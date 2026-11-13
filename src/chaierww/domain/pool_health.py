"""号池供给：健康号数量、24 小时内到期数、冷却数、库存天数与转发成功率。

全部**只读本地数据**：

- 账号 / 登录态 / 凭据到期 / 巡检熔断 → ``accounts`` 与 ``credential_probes``；
- 冷却 → 网关内存里的 ``GatewayRelay.cooldown``；
- 库存 → 网关日账本里的积分快照（每天一条，最多 14 天）；
- 成功率 → 网关日志（保留条数有限，所以口径写清楚是「保留日志内」）。

数据不足一律返回 ``None``（未知 ≠ 0），宁可显示「暂无数据」，也不编一个 0。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from chaierww.domain.credential_lifecycle import AUTH_FAILURE_LIMIT
from chaierww.domain.login_health import LOGIN_BLOCKED, LOGIN_INVALID

#: 库存天数按最近这么久的消耗推算。
CREDITS_WINDOW_DAYS = 7
#: 健康账号少于这个数就告警。
DEFAULT_MIN_HEALTHY = 2
#: 库存支撑天数低于这个值就告警。
DEFAULT_MIN_CREDITS_DAYS = 3.0


@dataclass(frozen=True, slots=True)
class SupplyThresholds:
    min_healthy_accounts: int = DEFAULT_MIN_HEALTHY
    min_credits_days: float = DEFAULT_MIN_CREDITS_DAYS


def credits_days(
    snapshots: dict[str, list[dict[str, Any]]],
    *,
    now: datetime | None = None,
    window_days: int = CREDITS_WINDOW_DAYS,
) -> float | None:
    """按近 ``window_days`` 天的净消耗推算号池还能撑几天；数据不足返回 ``None``。

    「净消耗」只加正值：签到 / 领奖会让余额变多，那是补给，不是消耗。
    时间跨度用每个号窗口内首末快照的实际间隔，避免把「刚接入三天」的数据
    当成七天消耗来低估库存。
    """
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(days=window_days)
    remaining = 0.0
    has_remaining = False
    consumed = 0.0
    span_seconds = 0.0

    for items in snapshots.values():
        points: list[tuple[datetime, float]] = []
        for item in items:
            credits = item.get("credits")
            if not isinstance(credits, (int, float)) or isinstance(credits, bool):
                continue
            at = _parse_at(item.get("at"))
            if at is not None:
                points.append((at, float(credits)))
        if not points:
            continue
        points.sort()
        remaining += points[-1][1]
        has_remaining = True
        window = [point for point in points if point[0] >= cutoff]
        if len(window) < 2:
            continue
        drop = window[0][1] - window[-1][1]
        if drop > 0:
            consumed += drop
        span_seconds += (window[-1][0] - window[0][0]).total_seconds()

    if not has_remaining:
        return None
    if consumed <= 0 or span_seconds <= 0:
        # 没有净消耗（或只有一天数据）时无法外推，不拿 0 当答案。
        return None
    per_day = consumed / max(span_seconds / 86400.0, 1.0)
    if per_day <= 0:
        return None
    return round(remaining / per_day, 1)


def _parse_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class PoolHealthService:
    """把号池的「现在够不够用」压成一屏数字 + 告警列表。"""

    def __init__(
        self,
        repository: Any,
        gateway: Any,
        *,
        relay: Any | None = None,
        preferences: Any | None = None,
        scopes: tuple[str, ...] = ("workbuddy-cn", "trae-cn"),
    ) -> None:
        self._repo = repository
        self._gateway = gateway
        self._relay = relay
        self._preferences = preferences
        self._scopes = scopes

    def thresholds(self) -> SupplyThresholds:
        if self._preferences is None:
            return SupplyThresholds()
        try:
            prefs = self._preferences.load()
        except Exception:  # noqa: BLE001 - 读偏好失败就用默认阈值
            return SupplyThresholds()
        return SupplyThresholds(
            min_healthy_accounts=int(getattr(prefs, "min_healthy_accounts", DEFAULT_MIN_HEALTHY)),
            min_credits_days=float(getattr(prefs, "min_credits_days", DEFAULT_MIN_CREDITS_DAYS)),
        )

    def health(self) -> dict[str, Any]:
        accounts = self._repo.list_all(self._scopes) if self._scopes else []
        ids = [account.id for account in accounts]
        logins = self._login_states(ids)
        expiries = self._expiries(ids)
        failures = self._failures(ids)
        excluded = self._excluded()
        now = datetime.now(UTC)
        soon = now + timedelta(hours=24)

        healthy = 0
        expiring = 0
        cooling = 0
        for account in accounts:
            state = logins.get(account.id, "unknown")
            if state in (LOGIN_INVALID, LOGIN_BLOCKED):
                continue
            if failures.get(account.id, 0) >= AUTH_FAILURE_LIMIT:
                continue
            moment = _parse_at(expiries.get(account.id))
            if moment is not None and moment <= now:
                continue
            if account.id in excluded:
                continue
            if self._cooling(account.id):
                cooling += 1
            else:
                healthy += 1
            if (moment is not None and moment <= soon) or state == "expiring":
                expiring += 1
        thresholds = self.thresholds()
        days = credits_days(self._snapshots(ids))
        rate = self._success_rate()
        alerts: list[dict[str, Any]] = []
        if healthy < thresholds.min_healthy_accounts:
            alerts.append(
                {
                    "code": "min_healthy_accounts",
                    "severity": "high" if healthy == 0 else "warn",
                    "message": (
                        f"健康账号只剩 {healthy} 个（阈值 {thresholds.min_healthy_accounts}），"
                        "网关可能无号可换，请补充或重新登录账号"
                    ),
                }
            )
        if days is not None and days < thresholds.min_credits_days:
            alerts.append(
                {
                    "code": "min_credits_days",
                    "severity": "high" if days <= 0 else "warn",
                    "message": (
                        f"按近 {CREDITS_WINDOW_DAYS} 天消耗推算，号池库存约 {days:g} 天"
                        f"（阈值 {thresholds.min_credits_days:g} 天），建议提前补号"
                    ),
                }
            )
        return {
            "healthyAccounts": healthy,
            "accounts": len(accounts),
            "expiring24h": expiring,
            "cooling": cooling,
            "creditsDays": days,
            "successRate24h": rate,
            "thresholds": {
                "minHealthyAccounts": thresholds.min_healthy_accounts,
                "minCreditsDays": thresholds.min_credits_days,
            },
            "alerts": alerts,
            "checkedAt": now.replace(microsecond=0).isoformat(),
        }

    # ---------- 数据源 ----------

    def _login_states(self, ids: list[str]) -> dict[str, str]:
        try:
            records = self._repo.login_probes(ids)
        except Exception:  # noqa: BLE001
            return {}
        return {key: value.get("state", "unknown") for key, value in records.items()}

    def _expiries(self, ids: list[str]) -> dict[str, str | None]:
        try:
            return self._repo.credential_expiries(ids)
        except Exception:  # noqa: BLE001
            return {}

    def _failures(self, ids: list[str]) -> dict[str, int]:
        try:
            return self._repo.probe_failures(ids)
        except Exception:  # noqa: BLE001
            return {}

    def _excluded(self) -> set[str]:
        try:
            return set(self._gateway.excluded_accounts())
        except Exception:  # noqa: BLE001
            return set()

    def _cooling(self, account_id: str) -> bool:
        if self._relay is None:
            return False
        try:
            return float(self._relay.cooldown(account_id)) > 0
        except Exception:  # noqa: BLE001
            return False

    def _snapshots(self, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
        reader = getattr(self._gateway, "credit_snapshots", None)
        if reader is None:
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        for account_id in ids:
            try:
                result[account_id] = reader(account_id)
            except Exception:  # noqa: BLE001
                continue
        return result

    def _success_rate(self) -> float | None:
        """最近 24 小时网关转发成功率；保留日志里没有任何请求时为 ``None``。"""
        try:
            entries = self._gateway.logs(limit=1000)
        except Exception:  # noqa: BLE001
            return None
        cutoff = datetime.now(UTC) - timedelta(hours=24)
        total = 0
        ok = 0
        for entry in entries:
            if entry.get("kind") == "probe":
                continue
            at = _parse_at(entry.get("at"))
            if at is None or at < cutoff:
                continue
            total += 1
            if entry.get("ok"):
                ok += 1
        if total == 0:
            return None
        return round(ok / total, 3)
