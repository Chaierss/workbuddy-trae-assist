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
