"""账号稳定度：把「这个号最近靠不靠得住」压成一组窗口指标。

四个来源都是**本地已落盘的数据**，刷新页面不触发任何上游请求：

- 切换成功率 → ``switch_jobs``（终态任务，排除预演）；
- 登录失效次数 → ``credential_probe_log`` 里 ``status='login_invalid'`` 的追加行；
- 续期失败次数 → ``credential_probe_log`` 里 ``transient`` / ``relogin_required``；
- 网关失败次数 → 网关日志（``ok=false`` 且 ``status!=0``，网络错误不算失败）。

数据不足一律返回 ``None``（未知 ≠ 0）。稳定度只用于展示与排序，
**不参与**切换、网关候选之外的任何决策。
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any

#: 允许的统计窗口。
WINDOWS = (7, 30)

#: 续期失败在巡检日志里的状态值。
_REFRESH_FAILED_STATES = ("transient", "relogin_required")
#: 登录探测追加到同一张历史表里的状态值（只写失效，避免污染凭据时间线）。
LOGIN_INVALID_STATUS = "login_invalid"


def _parse_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat()


class StabilityService:
    """按账号聚合切换 / 登录 / 续期 / 网关四个窗口指标。"""

    def __init__(
        self,
        repository: Any,
        gateway: Any,
        conn: Any,
        *,
        scopes: tuple[str, ...] = ("workbuddy-cn", "trae-cn"),
        lock: threading.RLock | None = None,
        now: Any = None,
    ) -> None:
        self._repo = repository
        self._gateway = gateway
        self._conn = conn
        self._scopes = scopes
        self._lock = lock or threading.RLock()
        self._now = now or (lambda: datetime.now(UTC))

    def report(self, days: int = 7) -> dict[str, Any]:
        span = days if days in WINDOWS else 7
        moment = self._now()
        since = _iso(moment - timedelta(days=span))
        accounts = self._repo.list_all(self._scopes) if self._scopes else []
        ids = [account.id for account in accounts]
        switches = self._switches(ids, since)
        probes = self._probe_log(ids, since)
        gateway = self._gateway_log(ids, since)

        items: list[dict[str, Any]] = []
        for account in accounts:
            switch = switches.get(account.id, {"ok": 0, "total": 0, "last": None})
            probe = probes.get(
                account.id,
                {"invalid": 0, "refreshFailed": 0, "hasProbe": False, "last": None},
            )
            gate = gateway.get(account.id, {"failed": 0, "seen": False, "last": None})
            samples = switch["total"] + probe["invalid"] + probe["refreshFailed"] + gate["failed"]
            if not samples and not probe["hasProbe"]:
                # 完全没有样本：返回全 null，界面显示「样本不足」，不编 0。
                items.append(self._blank(account))
                continue
            rate = round(switch["ok"] / switch["total"], 3) if switch["total"] else None
            reasons = [
                record
                for record in (switch["last"], probe["last"], gate["last"])
                if record is not None
            ]
            latest = max(reasons, key=lambda item: item["at"]) if reasons else None
            items.append(
                {
                    **self._base(account),
                    "switchTotal": switch["total"],
                    "switchOk": switch["ok"],
                    "switchRate": rate,
                    "loginInvalid": probe["invalid"] if probe["hasProbe"] else None,
                    "refreshFailed": probe["refreshFailed"] if probe["hasProbe"] else None,
                    "gatewayFailed": gate["failed"] if gate["seen"] else None,
                    "lastReason": latest["reason"] if latest else "",
                    "lastAt": latest["at"] if latest else None,
                }
            )
        return {
            "days": span,
            "items": items,
            "checkedAt": _iso(moment),
        }

    def _base(self, account: Any) -> dict[str, Any]:
        return {
            "accountId": account.id,
            "displayName": account.display_name,
            "scope": account.scope,
            "platform": getattr(account, "platform", ""),
        }

    def _blank(self, account: Any) -> dict[str, Any]:
        return {
            **self._base(account),
            "switchTotal": 0,
            "switchOk": 0,
            "switchRate": None,
            "loginInvalid": None,
            "refreshFailed": None,
            "gatewayFailed": None,
            "lastReason": "",
            "lastAt": None,
        }

    def _switches(self, ids: list[str], since: str) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT target_account_id, state, started_at, error
                  FROM switch_jobs
                 WHERE dry_run = 0 AND finished_at IS NOT NULL
                   AND started_at >= ? AND target_account_id IN ({marks})
                """,
                (since, *ids),
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            account_id = str(row["target_account_id"])
            entry = result.setdefault(account_id, {"ok": 0, "total": 0, "last": None})
            entry["total"] += 1
            if str(row["state"]) == "succeeded":
                entry["ok"] += 1
                continue
            reason = str(row["error"] or row["state"] or "切换失败")
            entry["last"] = {"at": str(row["started_at"]), "reason": f"切换失败：{reason}"}
        return result

    def _probe_log(self, ids: list[str], since: str) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        with self._lock:
            rows = self._conn.execute(
                f"""
                SELECT account_id, at, status, detail
                  FROM credential_probe_log
                 WHERE at >= ? AND account_id IN ({marks})
                """,
                (since, *ids),
            ).fetchall()
        result: dict[str, dict[str, Any]] = {}
        for row in rows:
            account_id = str(row["account_id"])
            entry = result.setdefault(
                account_id,
                {"invalid": 0, "refreshFailed": 0, "hasProbe": False, "last": None},
            )
            entry["hasProbe"] = True
            status = str(row["status"] or "")
            if status == LOGIN_INVALID_STATUS:
                entry["invalid"] += 1
                entry["last"] = {
                    "at": str(row["at"]),
                    "reason": str(row["detail"] or "登录态失效"),
                }
            elif status in _REFRESH_FAILED_STATES:
                entry["refreshFailed"] += 1
                entry["last"] = {
                    "at": str(row["at"]),
                    "reason": str(row["detail"] or f"续期失败（{status}）"),
                }
        return result

    def _gateway_log(self, ids: list[str], since: str) -> dict[str, dict[str, Any]]:
        wanted = set(ids)
        result: dict[str, dict[str, Any]] = {}
        try:
            entries = self._gateway.logs(limit=2000)
        except Exception:  # noqa: BLE001 - 日志读不到只是未知，不影响其它指标
            return {}
        for entry in entries:
            account_id = str(entry.get("accountId") or "")
            if account_id not in wanted:
                continue
            at = _parse_at(entry.get("at"))
            if at is None or _iso(at) < since:
                continue
            slot = result.setdefault(account_id, {"failed": 0, "seen": True, "last": None})
            slot["seen"] = True
            status = entry.get("status")
            if entry.get("ok"):
                continue
            # status=0 是网络错误：不是账号的问题，不计入失败。
            if status in (None, 0, "0"):
                continue
            slot["failed"] += 1
            slot["last"] = {
                "at": _iso(at),
                "reason": str(entry.get("reason") or entry.get("message") or f"网关 {status}"),
            }
        return result
