from __future__ import annotations

import functools
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from chaierww.domain.accounts.models import Account, CredentialState, UpsertResult
from chaierww.domain.scopes import require_implemented
from chaierww.infra.db import now_iso, transaction
from chaierww.infra.secrets import SecretBox

_F = TypeVar("_F", bound=Callable[..., Any])


class AccountNotFound(LookupError):
    pass


class AccountNotInTrash(RuntimeError):
    """账号存在，但不在回收站里，因此不允许永久删除。

    不复用 ``AccountNotFound``：那个语义是「这个 id 不存在」，映射 404；
    「存在但不该删」是状态冲突，应该映射 409。
    """


def _serialized(method: _F) -> _F:
    """同一时刻只允许一个操作持有连接。

    SQLite 单连接可在多线程使用（底层 serialized 模式），但事务是连接级的：
    两个线程的 BEGIN/COMMIT 交错会互相污染。这里用可重入锁把整个操作串起来，
    方法内部的自我调用不会死锁。
    """

    @functools.wraps(method)
    def wrapper(self: AccountRepository, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


class AccountRepository:
    """账号库的数据访问层。

    只负责数据读写与唯一性约束；"能不能删""该不该切"属于业务规则，
    放在 service 层，避免这里混入运行时状态判断。
    """

    def __init__(self, conn: sqlite3.Connection, box: SecretBox) -> None:
        self._conn = conn
        self._box = box
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        """与其它仓储共享的连接锁。

        SQLite 的事务是**连接级**的：``SyncRepository`` 若自带一把锁，仍可能与
        本仓储的事务交错。把同一把 RLock 传过去，跨仓储的读改写才真正串行。
        """
        return self._lock

    # ---------- 写入 ----------

    @_serialized
    def upsert(
        self,
        *,
        scope: str,
        external_id: str,
        display_name: str,
        credential: dict[str, object],
        credential_kind: str = "session",
        avatar_path: str | None = None,
        source: str = "local_import",
        expires_at: str | None = None,
    ) -> UpsertResult:
        require_implemented(scope)
        if not external_id or not external_id.strip():
            raise ValueError("账号唯一标识不能为空")
        if not display_name or not display_name.strip():
            raise ValueError("账号显示名不能为空")

        now = now_iso()
        sealed = self._box.seal(json.dumps(credential, ensure_ascii=False).encode("utf-8"))

        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE scope = ? AND external_id = ?",
                (scope, external_id),
            ).fetchone()

            if row is None:
                account_id = uuid.uuid4().hex
                created = True
                restored = False
                self._conn.execute(
                    """
                    INSERT INTO accounts (
                        id, scope, external_id, display_name, note, avatar_path,
                        source, credential_state, last_used_at, created_at, updated_at, deleted_at
                    ) VALUES (?, ?, ?, ?, '', ?, ?, ?, NULL, ?, ?, NULL)
                    """,
                    (
                        account_id,
                        scope,
                        external_id,
                        display_name.strip(),
                        avatar_path,
                        source,
                        CredentialState.UNVERIFIED.value,
                        now,
                        now,
                    ),
                )
            else:
                account_id = row["id"]
                created = False
                restored = row["deleted_at"] is not None
                self._conn.execute(
                    """
                    UPDATE accounts
                       SET display_name = ?,
                           avatar_path = COALESCE(?, avatar_path),
                           source = ?,
                           credential_state = ?,
                           credential_generation = credential_generation + 1,
                           updated_at = ?,
                           deleted_at = NULL
                     WHERE id = ?
                    """,
                    (
                        display_name.strip(),
                        avatar_path,
                        source,
                        CredentialState.UNVERIFIED.value,
                        now,
                        account_id,
                    ),
                )

            self._conn.execute(
                "UPDATE credentials SET is_current = 0 WHERE account_id = ?", (account_id,)
            )
            self._conn.execute(
                """
                INSERT INTO credentials (
                    id, account_id, kind, sealed, created_at, expires_at, is_current
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (uuid.uuid4().hex, account_id, credential_kind, sealed, now, expires_at),
            )
            # 新凭据 = 新起点：清掉旧的续期探测记录，否则熔断计数会让
            # 刚重新登录的号继续被当成「连续认证失败」跳过。
            self._conn.execute(
                "DELETE FROM credential_probes WHERE account_id = ?", (account_id,)
            )

        fresh = self.get(account_id)
        return UpsertResult(created=created, restored=restored, account=fresh)

    @_serialized
    def update_note(self, account_id: str, note: str) -> Account:
        with transaction(self._conn):
            cursor = self._conn.execute(
                "UPDATE accounts SET note = ?, updated_at = ? WHERE id = ?",
                (note, now_iso(), account_id),
            )
            if cursor.rowcount == 0:
                raise AccountNotFound(account_id)
        return self.get(account_id)

    @_serialized
    def replace_credential(
        self,
        account_id: str,
        *,
        credential: dict[str, object],
        kind: str,
        expires_at: str | None,
    ) -> None:
        """用一份续期后的凭据替换当前凭据（旧行保留，`is_current=0`）。

        只动 `credentials` 与 `accounts.updated_at`：显示名、备注、删除状态、
        凭据状态一律不在这里改 —— 状态由生命周期服务按结论单独写。
        """
        sealed = self._box.seal(json.dumps(credential, ensure_ascii=False).encode("utf-8"))
        now = now_iso()
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT 1 FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if row is None:
                raise AccountNotFound(account_id)
            self._conn.execute(
                "UPDATE credentials SET is_current = 0 WHERE account_id = ?", (account_id,)
            )
            self._conn.execute(
                """
                INSERT INTO credentials (
                    id, account_id, kind, sealed, created_at, expires_at, is_current
                ) VALUES (?, ?, ?, ?, ?, ?, 1)
                """,
                (uuid.uuid4().hex, account_id, kind, sealed, now, expires_at),
            )
            self._conn.execute(
                "UPDATE accounts SET updated_at = ?, "
                "credential_generation = credential_generation + 1 WHERE id = ?",
                (now, account_id),
            )

    @_serialized
    def probe_times(self, account_ids: list[str] | tuple[str, ...]) -> dict[str, str]:
        """批量读「上次凭据探测时间」，供盲续期节流判定。"""
        wanted = [str(item) for item in account_ids if str(item)]
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        rows = self._conn.execute(
            f"SELECT account_id, probed_at FROM credential_probes "
            f"WHERE account_id IN ({placeholders})",
            wanted,
        ).fetchall()
        return {str(row["account_id"]): str(row["probed_at"]) for row in rows}

    @_serialized
    def probe_failures(
        self, account_ids: list[str] | tuple[str, ...]
    ) -> dict[str, int]:
        """批量读「连续认证失败次数」，用于续期熔断。"""
        wanted = [str(item) for item in account_ids if str(item)]
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        rows = self._conn.execute(
            f"SELECT account_id, failures FROM credential_probes "
            f"WHERE account_id IN ({placeholders})",
            wanted,
        ).fetchall()
        return {str(row["account_id"]): int(row["failures"]) for row in rows}

    @_serialized
    def record_probe(self, account_id: str, status: str, *, failed: bool = False) -> None:
        """记一次续期探测（无论成败）。过期时间未知的号靠它做 12 小时节流。

        ``failed=True`` 时把连续认证失败次数 +1，成功则清零 —— 连续 3 次后
        调度停止自动续期（见 ``credential_lifecycle.AUTH_FAILURE_LIMIT``）。
        """
        increment = 1 if failed else 0
        with transaction(self._conn):
            self._conn.execute(
                """
                INSERT INTO credential_probes (account_id, probed_at, status, failures)
                VALUES (?, ?, ?, ?)
                ON CONFLICT (account_id) DO UPDATE SET
                    probed_at = excluded.probed_at,
                    status = excluded.status,
                    failures = CASE
                        WHEN ? = 1 THEN credential_probes.failures + 1
                        ELSE 0
                    END
                """,
                (account_id, now_iso(), status, increment, increment),
            )

    @_serialized
    def record_activity_runs(self, rows: list[dict[str, object]]) -> None:
        """追加活动执行流水；调用方负责只传已存在的 account_id。"""
        if not rows:
            return
        now = now_iso()
        with transaction(self._conn):
            self._conn.executemany(
                """
                INSERT INTO activity_runs (
                    id, account_id, activity_key, day, attempt, status, message, reward, at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        uuid.uuid4().hex,
                        str(row.get("accountId") or ""),
                        str(row.get("key") or ""),
                        str(row.get("day") or ""),
                        int(row.get("attempt") or 1),
                        str(row.get("status") or ""),
                        str(row.get("message") or ""),
                        row.get("reward"),
                        now,
                    )
                    for row in rows
                ],
            )

    @_serialized
    def activity_runs(self, since_day: str, limit: int = 500) -> list[dict[str, object]]:
        """按天读取活动执行流水（新的在前），供「最近执行」回看。"""
        rows = self._conn.execute(
            """
            SELECT account_id, activity_key, day, attempt, status, message, reward, at
              FROM activity_runs
             WHERE day >= ?
             ORDER BY at DESC
             LIMIT ?
            """,
            (since_day, max(1, int(limit))),
        ).fetchall()
        return [
            {
                "accountId": str(row["account_id"]),
                "key": str(row["activity_key"]),
                "day": str(row["day"]),
                "attempt": int(row["attempt"]),
                "status": str(row["status"]),
                "message": str(row["message"]),
                "reward": row["reward"],
                "at": str(row["at"]),
            }
            for row in rows
        ]

    @_serialized
    def activity_history(
        self, since_day: str, key: str | None = None
    ) -> list[dict[str, object]]:
        """按天聚合活动执行结果（供活动详情里的趋势图）。

        只返回「有执行记录」的 (day, key, status) 组合，缺失的天由前端补零；
        这样数据量恒定，不会因为天数增加而线性膨胀。
        """
        sql = """
            SELECT day, activity_key, status,
                   COUNT(*) AS count,
                   COALESCE(SUM(reward), 0) AS reward
              FROM activity_runs
             WHERE day >= ?
        """
        params: list[object] = [since_day]
        if key:
            sql += " AND activity_key = ?"
            params.append(key)
        sql += " GROUP BY day, activity_key, status ORDER BY day ASC"
        rows = self._conn.execute(sql, params).fetchall()
        return [
            {
                "day": str(row["day"]),
                "key": str(row["activity_key"]),
                "status": str(row["status"]),
                "count": int(row["count"]),
                "reward": float(row["reward"] or 0),
            }
            for row in rows
        ]

    @_serialized
    def credential_expiries(
        self, account_ids: list[str] | tuple[str, ...]
    ) -> dict[str, str | None]:
        """批量读当前凭据的有效期，供列表一次取全（避免逐号查库）。"""
        wanted = [str(item) for item in account_ids if str(item)]
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        rows = self._conn.execute(
            f"""
            SELECT account_id, expires_at
              FROM credentials
             WHERE account_id IN ({placeholders}) AND is_current = 1
            """,
            wanted,
        ).fetchall()
        return {str(row["account_id"]): row["expires_at"] for row in rows}

    @_serialized
    def current_credentials(
        self, account_ids: list[str] | tuple[str, ...]
    ) -> dict[str, tuple[str, dict[str, object], str | None]]:
        """批量取当前凭据：`id -> (kind, 明文载荷, expires_at)`。

        **只给内部判定用**（可续期性 / 过期时间），返回值绝不能进接口响应或日志。
        """
        wanted = [str(item) for item in account_ids if str(item)]
        if not wanted:
            return {}
        placeholders = ",".join("?" for _ in wanted)
        rows = self._conn.execute(
            f"""
            SELECT account_id, kind, sealed, expires_at
              FROM credentials
             WHERE account_id IN ({placeholders}) AND is_current = 1
            """,
            wanted,
        ).fetchall()
        found: dict[str, tuple[str, dict[str, object], str | None]] = {}
        for row in rows:
            try:
                data = json.loads(self._box.open(row["sealed"]).decode("utf-8"))
            except (ValueError, OSError):
                continue
            if isinstance(data, dict):
                found[str(row["account_id"])] = (str(row["kind"]), data, row["expires_at"])
        return found

    @_serialized
    def credential_generation(self, account_id: str) -> int:
        """该账号当前登录态代际；账号不存在抛 ``AccountNotFound``。"""
        row = self._conn.execute(
            "SELECT credential_generation FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if row is None:
            raise AccountNotFound(account_id)
        return int(row[0])

    @_serialized
    def bump_credential_generation(self, account_id: str) -> int:
        """登录态发生实际变化时把代际 +1，返回新值。

        只在「确实写入了新凭据」时调用（回写内容相同会提前 return，不虚增代际）。
        """
        with transaction(self._conn):
            cursor = self._conn.execute(
                "UPDATE accounts SET credential_generation = credential_generation + 1, "
                "updated_at = ? WHERE id = ?",
                (now_iso(), account_id),
            )
            if cursor.rowcount == 0:
                raise AccountNotFound(account_id)
            row = self._conn.execute(
                "SELECT credential_generation FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return int(row[0])

    @_serialized
    def set_credential_state(self, account_id: str, state: CredentialState) -> None:
        with transaction(self._conn):
            cursor = self._conn.execute(
                "UPDATE accounts SET credential_state = ?, updated_at = ? WHERE id = ?",
                (state.value, now_iso(), account_id),
            )
            if cursor.rowcount == 0:
                raise AccountNotFound(account_id)

    @_serialized
    def touch_last_used(self, account_id: str, when: str | None = None) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE accounts SET last_used_at = ?, updated_at = ? WHERE id = ?",
                (when or now_iso(), now_iso(), account_id),
            )

    @_serialized
    def soft_delete(self, account_id: str) -> None:
        with transaction(self._conn):
            cursor = self._conn.execute(
                "UPDATE accounts SET deleted_at = ?, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NULL",
                (now_iso(), now_iso(), account_id),
            )
            if cursor.rowcount == 0:
                raise AccountNotFound(f"{account_id}（不存在或已在回收站）")

    @_serialized
    def restore(self, account_id: str) -> Account:
        with transaction(self._conn):
            cursor = self._conn.execute(
                "UPDATE accounts SET deleted_at = NULL, updated_at = ? "
                "WHERE id = ? AND deleted_at IS NOT NULL",
                (now_iso(), account_id),
            )
            if cursor.rowcount == 0:
                raise AccountNotFound(f"{account_id}（不存在或不在回收站）")
        return self.get(account_id)

    @_serialized
    def purge_expired(self, days: int) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=days)).replace(microsecond=0).isoformat()
        with transaction(self._conn):
            cursor = self._conn.execute(
                "DELETE FROM accounts WHERE deleted_at IS NOT NULL AND deleted_at < ?",
                (cutoff,),
            )
        return cursor.rowcount

    @_serialized
    def purge(self, account_id: str) -> Account:
        """永久删除回收站里的一个账号，返回删前的行（供调用方做关联清理）。

        - 凭据由 ``credentials.account_id ... ON DELETE CASCADE`` 自动清掉，不显式删。
        - DELETE 的条件里再带一次 ``deleted_at IS NOT NULL``：防「读到在回收站、
          删除前恰好被恢复」的竞态，宁可 0 行也不误删。
        - 不做 bulk 版：批量场景需要逐条的失败原因，由调用方循环调用本方法。
        """
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT * FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if row is None:
                raise AccountNotFound(account_id)
            if row["deleted_at"] is None:
                raise AccountNotInTrash(f"{account_id}（不在回收站，不能永久删除）")
            cursor = self._conn.execute(
                "DELETE FROM accounts WHERE id = ? AND deleted_at IS NOT NULL",
                (account_id,),
            )
            if cursor.rowcount == 0:
                raise AccountNotInTrash(f"{account_id}（已被恢复，不能永久删除）")
        return Account.from_row(row)

    # ---------- 读取 ----------

    @_serialized
    def get(self, account_id: str) -> Account:
        row = self._conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            raise AccountNotFound(account_id)
        return Account.from_row(row)

    @_serialized
    def get_by_external(self, scope: str, external_id: str) -> Account | None:
        row = self._conn.execute(
            "SELECT * FROM accounts WHERE scope = ? AND external_id = ?",
            (scope, external_id),
        ).fetchone()
        return Account.from_row(row) if row else None

    @_serialized
    def list_accounts(
        self,
        scope: str,
        *,
        include_deleted: bool = False,
        only_deleted: bool = False,
        query: str | None = None,
    ) -> list[Account]:
        require_implemented(scope)
        sql = "SELECT * FROM accounts WHERE scope = ?"
        params: list[object] = [scope]
        if only_deleted:
            sql += " AND deleted_at IS NOT NULL"
        elif not include_deleted:
            sql += " AND deleted_at IS NULL"
        if query:
            sql += " AND (display_name LIKE ? OR note LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like])
        sql += " ORDER BY (last_used_at IS NULL), last_used_at DESC, created_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [Account.from_row(row) for row in rows]

    @_serialized
    def list_all(
        self,
        scopes: list[str] | tuple[str, ...],
        *,
        include_deleted: bool = False,
        only_deleted: bool = False,
        query: str | None = None,
    ) -> list[Account]:
        """跨作用域读取：合并号池用。

        与 list_accounts 的区别只在「按多个 scope 取」；排序规则保持一致，
        平台归属由返回行各自的 scope 字段表达，这里不做任何合并改写。
        """
        if not scopes:
            return []
        for scope in scopes:
            require_implemented(scope)
        placeholders = ",".join("?" for _ in scopes)
        sql = f"SELECT * FROM accounts WHERE scope IN ({placeholders})"
        params: list[object] = list(scopes)
        if only_deleted:
            sql += " AND deleted_at IS NOT NULL"
        elif not include_deleted:
            sql += " AND deleted_at IS NULL"
        if query:
            sql += " AND (display_name LIKE ? OR note LIKE ?)"
            like = f"%{query}%"
            params.extend([like, like])
        sql += " ORDER BY (last_used_at IS NULL), last_used_at DESC, created_at DESC"
        rows = self._conn.execute(sql, params).fetchall()
        return [Account.from_row(row) for row in rows]

    @_serialized
    def count_by_state(self, scope: str) -> dict[CredentialState, int]:
        rows = self._conn.execute(
            """
            SELECT credential_state, COUNT(*) AS n
              FROM accounts
             WHERE scope = ? AND deleted_at IS NULL
             GROUP BY credential_state
            """,
            (scope,),
        ).fetchall()
        counts = {state: 0 for state in CredentialState}
        for row in rows:
            counts[CredentialState(row["credential_state"])] = row["n"]
        return counts

    @_serialized
    def current_credential(self, account_id: str) -> dict[str, object]:
        """解密并返回当前凭据。

        仅供切换引擎调用；调用方不得把结果写入日志或落盘明文。
        """
        return self.credential_record(account_id)[1]

    @_serialized
    def credential_record(self, account_id: str) -> tuple[str, dict[str, object]]:
        """当前凭据的（类型, 明文载荷）。

        类型要跟着导出走：`oauth_token` 与 `session` 的导入路径不同，
        只导出载荷会让恢复出来的账号切不过去。
        """
        row = self._conn.execute(
            "SELECT kind, sealed FROM credentials WHERE account_id = ? AND is_current = 1 "
            "ORDER BY created_at DESC LIMIT 1",
            (account_id,),
        ).fetchone()
        if row is None:
            raise AccountNotFound(f"{account_id}（无可用凭据）")
        plaintext = self._box.open(row["sealed"])
        data = json.loads(plaintext.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("凭据结构异常")
        return str(row["kind"]), data

    @_serialized
    def has_credential(self, account_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM credentials WHERE account_id = ? AND is_current = 1 LIMIT 1",
            (account_id,),
        ).fetchone()
        return row is not None
