"""网关自动隔离与回归。

只处理一种情况：**账号在网关连续多次返回 401/403**。这类失败通常意味着登录态
或设备绑定出了问题，继续把它放进候选只会让每个请求都先失败一次。

边界写死：

- 只影响**网关候选**，不影响切号、备份、活动、数据；
- 网络错误（status=0）、429、5xx 不计数；
- 连续 ``isolationThreshold`` 次才隔离，默认 24 小时后自动回归；
- 成功一次即清零；人工可随时解除；
- 开关关闭时完全不写隔离，只保留计数清零。
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from chaierww.infra.db import transaction

DEFAULT_THRESHOLD = 3
DEFAULT_HOURS = 24


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


def _parse_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class IsolationService:
    """``account_isolation`` 的唯一读写入口。"""

    def __init__(
        self,
        conn: Any,
        preferences: Any,
        *,
        lock: threading.RLock | None = None,
        now: Any = None,
    ) -> None:
        self._conn = conn
        self._preferences = preferences
        self._lock = lock or threading.RLock()
        self._now = now or (lambda: datetime.now(UTC))
        #: 本次进程内发生过失败记账的账号：成功回调只对它们做一次清理，
        #: 避免每个网关请求都开一次事务。
        self._touched: set[str] = set()
        #: 网关每个请求都会问「当前排除谁」，5 秒缓存避免每次转发都查库。
        #: 同时记录逻辑时间：测试或时钟跳变时不能让旧缓存盖住新状态。
        self._cache: tuple[float, datetime | None, set[str]] = (0.0, None, set())

    # ---------- 配置 ----------

    def _settings(self) -> tuple[bool, int, int]:
        try:
            prefs = self._preferences.load()
        except Exception:  # noqa: BLE001 - 读不到偏好就按默认值处理
            return True, DEFAULT_THRESHOLD, DEFAULT_HOURS
        return (
            bool(getattr(prefs, "isolation_enabled", True)),
            max(1, int(getattr(prefs, "isolation_threshold", DEFAULT_THRESHOLD))),
            max(1, int(getattr(prefs, "isolation_hours", DEFAULT_HOURS))),
        )

    # ---------- 写入 ----------

    def record_failure(self, account_id: str, status: int) -> dict[str, Any] | None:
        """记一次网关认证失败；达到阈值返回新隔离的台账，否则返回 ``None``。"""
        if not account_id or status not in (401, 403):
            return None
        enabled, threshold, hours = self._settings()
        if not enabled:
            return None
        moment = self._now()
        stamp = _iso(moment)
        self._touched.add(account_id)
        self._cache = (0.0, None, set())
        with self._lock, transaction(self._conn):
            row = self._conn.execute(
                "SELECT * FROM account_isolation WHERE account_id = ?", (account_id,)
            ).fetchone()
            count = int(row["failure_count"] or 0) + 1 if row else 1
            previous_until = _parse_at(row["isolated_until"]) if row else None
            if previous_until is not None and previous_until <= moment:
                # 隔离期已过但还没来得及清理：从这次失败重新累计。
                count = 1
            first = (row["first_failure_at"] if row else None) or stamp
            isolated_until = row["isolated_until"] if row else None
            if count >= threshold:
                isolated_until = _iso(moment + timedelta(hours=hours))
            self._conn.execute(
                """
                INSERT INTO account_isolation (
                    account_id, reason, failure_count, first_failure_at,
                    last_failure_at, isolated_until, released_at, updated_at
                ) VALUES (?, 'auth', ?, ?, ?, ?, NULL, ?)
                ON CONFLICT (account_id) DO UPDATE SET
                    reason = excluded.reason,
                    failure_count = excluded.failure_count,
                    first_failure_at = excluded.first_failure_at,
                    last_failure_at = excluded.last_failure_at,
                    isolated_until = excluded.isolated_until,
                    released_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    account_id,
                    count,
                    first,
                    stamp,
                    isolated_until,
                    stamp,
                ),
            )
        if count < threshold:
            return None
        return {
            "accountId": account_id,
            "failureCount": count,
            "isolatedUntil": isolated_until,
            "reason": "auth",
        }

    def record_success(self, account_id: str) -> None:
        """成功一次即清零：连续失败的定义不能跨过一次成功。"""
        if not account_id or account_id not in self._touched:
            return
        self._touched.discard(account_id)
        self._cache = (0.0, None, set())
        with self._lock, transaction(self._conn):
            self._conn.execute(
                "DELETE FROM account_isolation WHERE account_id = ?", (account_id,)
            )

    def release(self, account_id: str) -> dict[str, Any]:
        """人工解除隔离：清零计数并标记释放，下一次失败重新累计。"""
        moment = self._now()
        stamp = _iso(moment)
        with self._lock, transaction(self._conn):
            row = self._conn.execute(
                "SELECT * FROM account_isolation WHERE account_id = ?", (account_id,)
            ).fetchone()
            if row is None:
                raise LookupError("该账号没有隔离记录")
            self._conn.execute(
                """
                UPDATE account_isolation
                   SET failure_count = 0, isolated_until = NULL,
                       released_at = ?, updated_at = ?
                 WHERE account_id = ?
                """,
                (stamp, stamp, account_id),
            )
        self._touched.discard(account_id)
        self._cache = (0.0, None, set())
        return {"accountId": account_id, "releasedAt": stamp}

    # ---------- 读取 ----------

    def active_ids(self) -> set[str]:
        """当前仍在隔离期的账号；结果缓存 5 秒，避免每次转发都查库。"""
        moment = self._now()
        second = moment.replace(microsecond=0)
        cached_at, cached_second, cached = self._cache
        if time.monotonic() - cached_at < 5 and cached_second == second:
            return set(cached)
        stamp = _iso(moment)
        expired: list[str] = []
        active: set[str] = set()
        with self._lock:
            rows = self._conn.execute(
                "SELECT account_id, isolated_until, released_at FROM account_isolation"
            ).fetchall()
            for row in rows:
                until = _parse_at(row["isolated_until"])
                if until is None or row["released_at"]:
                    continue
                if until > moment:
                    active.add(str(row["account_id"]))
                else:
                    expired.append(str(row["account_id"]))
            if expired:
                marks = ",".join("?" for _ in expired)
                self._conn.execute(
                    f"""
                    UPDATE account_isolation
                       SET failure_count = 0, isolated_until = NULL,
                           released_at = ?, updated_at = ?
                     WHERE account_id IN ({marks})
                    """,
                    (stamp, stamp, *expired),
                )
        self._cache = (time.monotonic(), second, active)
        return set(active)

    def items(self) -> list[dict[str, Any]]:
        moment = self._now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM account_isolation ORDER BY updated_at DESC"
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            until = _parse_at(row["isolated_until"])
            active = until is not None and until > moment and not row["released_at"]
            result.append(
                {
                    "accountId": str(row["account_id"]),
                    "reason": str(row["reason"] or "auth"),
                    "failureCount": int(row["failure_count"] or 0),
                    "firstFailureAt": row["first_failure_at"],
                    "lastFailureAt": row["last_failure_at"],
                    "isolatedUntil": row["isolated_until"],
                    "releasedAt": row["released_at"],
                    "active": active,
                }
            )
        return result

    def detail(self, account_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in self.items() if item["accountId"] == account_id), None
        )
