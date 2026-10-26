"""``sync_state`` / ``sync_items`` / ``sync_jobs`` 的读写。

与 ``AccountRepository`` 共用同一个 SQLite 连接与同一把锁：SQLite 的事务是
连接级的，两个仓储各自加锁仍会交错。因此这里不自己建锁，由构造参数注入。
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
import uuid
from collections.abc import Callable
from typing import Any, TypeVar

from chaierww.infra.db import now_iso, transaction

_F = TypeVar("_F", bound=Callable[..., Any])

#: 同步状态取值。``blocked`` 表示在等外部条件（客户端未退出 / 凭据失效），不是失败。
SYNC_IDLE = "idle"
SYNC_CAPTURING = "capturing"
SYNC_APPLIED = "applied"
SYNC_BLOCKED = "blocked"
SYNC_FAILED = "failed"


def _serialized(method: _F) -> _F:
    @functools.wraps(method)
    def wrapper(self: SyncRepository, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


class SyncRepository:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    # ---------- sync_state ----------

    @_serialized
    def ensure_state(self, account_id: str, scope: str, external_id: str) -> None:
        with transaction(self._conn):
            self._conn.execute(
                """
                INSERT INTO sync_state (account_id, scope, external_id)
                VALUES (?, ?, ?)
                ON CONFLICT (account_id) DO NOTHING
                """,
                (account_id, scope, external_id),
            )

    @_serialized
    def get_state(self, account_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM sync_state WHERE account_id = ?", (account_id,)
        ).fetchone()
        return dict(row) if row else None

    @_serialized
    def update_state(self, account_id: str, **fields: Any) -> None:
        if not fields:
            return
        allowed = {
            "enabled",
            "state",
            "credential_generation",
            "last_captured_at",
            "last_applied_at",
            "items_total",
            "items_pending",
            "last_error",
        }
        sets = [f"{key} = ?" for key in fields if key in allowed]
        if not sets:
            return
        params = [fields[key] for key in fields if key in allowed]
        params.append(account_id)
        with transaction(self._conn):
            self._conn.execute(
                f"UPDATE sync_state SET {', '.join(sets)} WHERE account_id = ?", params
            )

    @_serialized
    def state_rows(self, scope: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM sync_state WHERE scope = ? ORDER BY last_captured_at DESC",
            (scope,),
        ).fetchall()
        return [dict(row) for row in rows]

    @_serialized
    def refresh_counts(self, account_id: str) -> tuple[int, int]:
        """重算 ``items_total`` / ``items_pending``：pending = 未落地且未删除。"""
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(
                       CASE WHEN local_cid IS NULL AND deleted_at IS NULL THEN 1 ELSE 0 END
                   ) AS pending
              FROM sync_items WHERE account_id = ?
            """,
            (account_id,),
        ).fetchone()
        total = int(row["total"] or 0)
        pending = int(row["pending"] or 0)
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE sync_state SET items_total = ?, items_pending = ? WHERE account_id = ?",
                (total, pending, account_id),
            )
        return total, pending

    # ---------- sync_items ----------

    @_serialized
    def get_item(self, account_id: str, stable_key: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM sync_items WHERE account_id = ? AND stable_key = ?",
            (account_id, stable_key),
        ).fetchone()
        return dict(row) if row else None

    @_serialized
    def items(self, account_id: str, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM sync_items WHERE account_id = ?"
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        sql += " ORDER BY captured_at DESC"
        return [dict(row) for row in self._conn.execute(sql, (account_id,)).fetchall()]

    @_serialized
    def upsert_item(self, account_id: str, item: dict[str, Any]) -> bool:
        """写入一条镜像。返回 ``True`` 表示内容有变化。"""
        existing = self._conn.execute(
            "SELECT content_sha256 FROM sync_items WHERE account_id = ? AND stable_key = ?",
            (account_id, item["stable_key"]),
        ).fetchone()
        changed = existing is None or existing["content_sha256"] != item["content_sha256"]
        with transaction(self._conn):
            self._upsert_row(account_id, item)
        return changed

    @_serialized
    def upsert_many(self, account_id: str, items: list[dict[str, Any]]) -> int:
        """批量写入（一个事务），返回内容有变化的条数。

        采集时一条源会话要同时分发给同一作用域里的所有账号（各自一份待应用记录），
        逐条开事务会把 6 个账号 × N 条会话变成几千次提交，因此这里必须批量。
        """
        if not items:
            return 0
        existing = {
            row["stable_key"]: row["content_sha256"]
            for row in self._conn.execute(
                "SELECT stable_key, content_sha256 FROM sync_items WHERE account_id = ?",
                (account_id,),
            ).fetchall()
        }
        changed = sum(
            1 for item in items if existing.get(item["stable_key"]) != item["content_sha256"]
        )
        with transaction(self._conn):
            for item in items:
                self._upsert_row(account_id, item)
        return changed

    def _upsert_row(self, account_id: str, item: dict[str, Any]) -> None:
        """单条 upsert（调用方负责事务）。

        ``local_cid`` 用 COALESCE：目标账号已经落地过这条血统时，后续镜像更新
        不能把它的本地 cid 抹掉（抹掉会被当成「从未落地」而重复创建会话）。
        """
        existing = self._conn.execute(
            "SELECT 1 FROM sync_items WHERE account_id = ? AND stable_key = ?",
            (account_id, item["stable_key"]),
        ).fetchone()
        if existing is None:
            self._conn.execute(
                """
                INSERT INTO sync_items (
                    account_id, stable_key, local_cid, workspace, title,
                    content_sha256, content_bytes, content_lines, meta_json,
                    source_updated_at, captured_at, applied_at, deleted_at, conflict_of
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    account_id,
                    item["stable_key"],
                    item.get("local_cid"),
                    item.get("workspace", ""),
                    item.get("title", ""),
                    item["content_sha256"],
                    item.get("content_bytes", 0),
                    item.get("content_lines", 0),
                    item.get("meta_json", ""),
                    item.get("source_updated_at"),
                    item.get("captured_at") or now_iso(),
                    item.get("applied_at"),
                    item.get("deleted_at"),
                    item.get("conflict_of"),
                ),
            )
            return
        self._conn.execute(
            """
            UPDATE sync_items
               SET local_cid = COALESCE(?, local_cid),
                   workspace = ?,
                   title = ?,
                   content_sha256 = ?,
                   content_bytes = ?,
                   content_lines = ?,
                   meta_json = ?,
                   source_updated_at = ?,
                   captured_at = ?,
                   deleted_at = ?
             WHERE account_id = ? AND stable_key = ?
            """,
            (
                item.get("local_cid"),
                item.get("workspace", ""),
                item.get("title", ""),
                item["content_sha256"],
                item.get("content_bytes", 0),
                item.get("content_lines", 0),
                item.get("meta_json", ""),
                item.get("source_updated_at"),
                item.get("captured_at") or now_iso(),
                item.get("deleted_at"),
                account_id,
                item["stable_key"],
            ),
        )

    @_serialized
    def mark_applied(
        self,
        account_id: str,
        stable_key: str,
        local_cid: str,
        *,
        applied_at: str | None = None,
    ) -> None:
        with transaction(self._conn):
            self._conn.execute(
                """
                UPDATE sync_items
                   SET local_cid = ?, applied_at = ?, conflict_of = NULL
                 WHERE account_id = ? AND stable_key = ?
                """,
                (local_cid, applied_at or now_iso(), account_id, stable_key),
            )

    @_serialized
    def mark_tombstone(self, account_id: str, stable_key: str, deleted_at: str | None) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE sync_items SET deleted_at = ? WHERE account_id = ? AND stable_key = ?",
                (deleted_at, account_id, stable_key),
            )

    @_serialized
    def mark_conflict(
        self, account_id: str, stable_key: str, conflict_sha: str, conflict_of: str
    ) -> None:
        with transaction(self._conn):
            self._conn.execute(
                """
                UPDATE sync_items
                   SET conflict_sha256 = ?, conflict_of = ?
                 WHERE account_id = ? AND stable_key = ?
                """,
                (conflict_sha, conflict_of, account_id, stable_key),
            )

    @_serialized
    def clear_conflict(self, account_id: str, stable_key: str) -> None:
        with transaction(self._conn):
            self._conn.execute(
                """
                UPDATE sync_items
                   SET conflict_sha256 = NULL, conflict_of = NULL
                 WHERE account_id = ? AND stable_key = ?
                """,
                (account_id, stable_key),
            )

    # ---------- sync_jobs ----------

    @_serialized
    def start_job(self, scope: str, account_id: str, direction: str) -> str:
        job_id = uuid.uuid4().hex
        with transaction(self._conn):
            self._conn.execute(
                """
                INSERT INTO sync_jobs (id, scope, account_id, direction, state, started_at)
                VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (job_id, scope, account_id, direction, now_iso()),
            )
        return job_id

    @_serialized
    def finish_job(
        self,
        job_id: str,
        state: str,
        *,
        items_total: int = 0,
        items_changed: int = 0,
        failed_step: str | None = None,
        error: str = "",
    ) -> None:
        with transaction(self._conn):
            self._conn.execute(
                """
                UPDATE sync_jobs
                   SET state = ?, items_total = ?, items_changed = ?,
                       failed_step = ?, error = ?, finished_at = ?
                 WHERE id = ?
                """,
                (
                    state,
                    items_total,
                    items_changed,
                    failed_step,
                    error,
                    now_iso(),
                    job_id,
                ),
            )

    @_serialized
    def forget_account(self, account_id: str) -> int:
        """永久删除账号后清掉它的同步痕迹。

        ``sync_state`` / ``sync_items`` 有 ``ON DELETE CASCADE``，账号行一删就没了；
        ``sync_jobs`` 是历史流水、没有外键，必须在这里显式删，否则审计表会一直留着
        指向不存在账号的记录。
        """
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM sync_jobs WHERE account_id = ?", (account_id,)
            ).fetchone()
            removed = int(row["n"] or 0)
            self._conn.execute("DELETE FROM sync_jobs WHERE account_id = ?", (account_id,))
            self._conn.execute("DELETE FROM sync_items WHERE account_id = ?", (account_id,))
            self._conn.execute("DELETE FROM sync_state WHERE account_id = ?", (account_id,))
        return removed

    @_serialized
    def recent_jobs(self, account_id: str, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM sync_jobs WHERE account_id = ? ORDER BY started_at DESC LIMIT ?",
            (account_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 辅助 ----------

    @_serialized
    def referenced_blobs(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT content_sha256 FROM sync_items WHERE content_sha256 <> ''"
        ).fetchall()
        return {row[0] for row in rows}

    @staticmethod
    def meta(item: dict[str, Any]) -> dict[str, Any]:
        """安全解析 ``meta_json``；损坏时返回空 dict，不抛。"""
        raw = item.get("meta_json") or ""
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return {}
        return data if isinstance(data, dict) else {}
