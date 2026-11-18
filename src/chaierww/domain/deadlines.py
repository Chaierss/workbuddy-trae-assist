"""活动截止雷达。

只回答一个问题：**哪些活动的周期快结束了，但还有账号没做完**。

数据来源是 ``activity_snapshots`` —— 检测结果里每个账号每项活动的最近一次状态。
这里不触发任何上游请求，也不猜：

- 没有 ``period_end`` 的活动直接跳过（签到类没有截止日，不该出现在雷达里）；
- 没有检测快照的账号不参与统计；
- 快照超过 ``stale_hours`` 标 ``stale``，页面显示「待检测」，且健康中心不据此告警。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from chaierww.domain.activities import ACTIVITY_CATALOG, STATE_DONE
from chaierww.infra.db import transaction

#: 快照超过这么久就认为「不是当前的检测结论」。
STALE_HOURS = 24

_TITLES = {entry["key"]: entry["title"] for entry in ACTIVITY_CATALOG}


def _parse_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_day(value: Any) -> datetime | None:
    """``period_end`` 可能是 ISO 时间或纯日期，两种都按 UTC 当天处理。"""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = datetime.strptime(text[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def record_snapshots(conn: Any, detected: list[Any]) -> int:
    """把一次检测结果写入 ``activity_snapshots``（同号同活动覆盖为最新）。

    只存活动状态与周期；``detectable=False`` 的账号没有条目，不写入空状态，
    避免把「查不了」误记成「没做完」。
    """
    stamp = datetime.now(UTC).replace(microsecond=0).isoformat()
    rows = [
        (item.account_id, activity.key, activity.state, activity.period_end, stamp)
        for item in detected
        if item.detectable
        for activity in item.items
    ]
    if not rows:
        return 0
    with transaction(conn):
        conn.executemany(
            """
            INSERT INTO activity_snapshots (
                account_id, activity_key, state, period_end, detected_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (account_id, activity_key) DO UPDATE SET
                state = excluded.state,
                period_end = excluded.period_end,
                detected_at = excluded.detected_at
            """,
            rows,
        )
    return len(rows)


def deadlines(
    conn: Any,
    *,
    days: int = 3,
    stale_hours: int = STALE_HOURS,
    now: datetime | None = None,
) -> dict[str, Any]:
    """按活动聚合「N 天内截止且仍有账号未完成」的项。

    ``pendingAccounts`` 里带账号 id 与显示名，前端点击可切到待办页签；
    ``stale=true`` 表示最近一次检测已经过期，只提示重新检测，不报警。
    """
    moment = now or datetime.now(UTC)
    span = max(1, min(int(days), 14))
    horizon = moment + timedelta(days=span)
    rows = conn.execute(
        """
        SELECT s.activity_key, s.account_id, s.state, s.period_end, s.detected_at,
               a.display_name, a.scope
          FROM activity_snapshots s
          JOIN accounts a ON a.id = s.account_id AND a.deleted_at IS NULL
         WHERE s.period_end IS NOT NULL AND s.period_end != ''
        """
    ).fetchall()

    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        end = _parse_day(row["period_end"])
        if end is None:
            continue
        # 已过截止日的不再进雷达：活动已经结束，提示也无法补做。
        if end.date() < moment.date():
            continue
        if end > horizon:
            continue
        key = str(row["activity_key"])
        group = groups.setdefault(
            key,
            {
                "key": key,
                "title": _TITLES.get(key, key),
                "periodEnd": str(row["period_end"]),
                "daysLeft": max(0, (end.date() - moment.date()).days),
                "totalAccounts": 0,
                "pendingAccounts": [],
                "detectedAt": None,
            },
        )
        group["totalAccounts"] += 1
        detected_at = _parse_at(row["detected_at"])
        if detected_at is not None and (
            group["detectedAt"] is None or detected_at.isoformat() > group["detectedAt"]
        ):
            group["detectedAt"] = detected_at.isoformat()
        if str(row["state"]) != STATE_DONE:
            group["pendingAccounts"].append(
                {
                    "accountId": str(row["account_id"]),
                    "displayName": str(row["display_name"]),
                    "scope": str(row["scope"]),
                }
            )

    cutoff = moment - timedelta(hours=max(1, int(stale_hours)))
    items: list[dict[str, Any]] = []
    for group in groups.values():
        if not group["pendingAccounts"]:
            continue  # 全部完成，不占雷达
        detected_at = _parse_at(group["detectedAt"])
        group["stale"] = detected_at is None or detected_at < cutoff
        group["pending"] = len(group["pendingAccounts"])
        items.append(group)

    items.sort(key=lambda item: (int(item["daysLeft"]), -int(item["pending"])))
    return {
        "items": items,
        "days": span,
        "checkedAt": moment.replace(microsecond=0).isoformat(),
    }
