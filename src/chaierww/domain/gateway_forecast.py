"""用量预测的纯逻辑：只读日账本与积分快照，**不发请求、不做 token→积分换算**。

三条硬规则（界面文案必须与之一致）：

1. 均值只用**完整的自然日**（不含今天），且窗口内至少 2 天有请求才给数字；
2. 积分消耗速度只用真实快照的差值算，余额上升（充值）时返回 `None`；
3. 数据不够时字段就是 `None` / `insufficient`，绝不编造一个「看起来合理」的数。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from math import ceil
from typing import Any

#: 默认趋势窗口（天）。
DEFAULT_WINDOW = 7
#: 近 3 天 vs 前 4 天，变动在 ±15% 内视为平稳。
TREND_BAND = 0.15
#: 「快满了」的阈值：当日限额用掉 80% 就进 watch。
WATCH_PERCENT = 0.8


def _to_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _to_moment(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tokens_of(bucket: Any, key: str) -> int:
    if not isinstance(bucket, dict):
        return 0
    slot = bucket.get(key)
    if not isinstance(slot, dict):
        return 0
    return int(slot.get("tokens") or 0)


def _confidence(active_days: int) -> str:
    if active_days >= 5:
        return "high"
    if active_days >= 2:
        return "medium"
    return "insufficient"


def _trend(recent: float, previous: float) -> str:
    if previous <= 0:
        return "up" if recent > 0 else "stable"
    ratio = (recent - previous) / previous
    if ratio >= TREND_BAND:
        return "up"
    if ratio <= -TREND_BAND:
        return "down"
    return "stable"


def _credit_burn(snapshots: Any, *, earliest: date) -> float | None:
    """窗口内最早与最近快照的日消耗速度；余额上升或不足两点时返回 `None`。"""
    points: list[tuple[datetime, float]] = []
    if isinstance(snapshots, list):
        for item in snapshots:
            if not isinstance(item, dict):
                continue
            at = _to_moment(item.get("at"))
            credits = _number(item.get("credits"))
            if at is None or credits is None:
                continue
            if at.date() < earliest:
                continue
            points.append((at, credits))
    if len(points) < 2:
        return None
    points.sort(key=lambda point: point[0])
    first_at, first_credits = points[0]
    last_at, last_credits = points[-1]
    span_days = (last_at - first_at).total_seconds() / 86400
    if span_days <= 0:
        return None
    drop = first_credits - last_credits
    if drop <= 0:
        return None
    return round(drop / span_days, 2)


def _account_row(
    descriptor: dict[str, Any],
    completed: list[dict[str, Any]],
    *,
    window_start: date,
    today: date,
) -> dict[str, Any]:
    account_id = str(descriptor.get("id") or "")
    recent_bucket = completed[-3:]
    previous_bucket = completed[:-3]
    window_tokens = sum(_tokens_of(row.get("accounts"), account_id) for row in completed)
    # 近 3 天与前 4 天要按「天」比，不能直接比总量：4 天总量天然比 3 天大，
    # 平稳使用会被误判成下降。
    recent_tokens = (
        sum(_tokens_of(row.get("accounts"), account_id) for row in recent_bucket)
        / len(recent_bucket)
        if recent_bucket
        else 0.0
    )
    previous_tokens = (
        sum(_tokens_of(row.get("accounts"), account_id) for row in previous_bucket)
        / len(previous_bucket)
        if previous_bucket
        else 0.0
    )
    credits = _number(descriptor.get("credits"))
    total = _number(descriptor.get("total"))
    burn = _credit_burn(descriptor.get("snapshots"), earliest=window_start)
    days_left = (
        round(credits / burn, 1)
        if credits is not None and burn is not None and burn > 0 and credits >= 0
        else None
    )
    exhaust_date = (
        (today + timedelta(days=ceil(days_left))).isoformat() if days_left is not None else None
    )
    return {
        "accountId": account_id,
        "name": str(descriptor.get("name") or "—"),
        "state": str(descriptor.get("state") or "usable"),
        "credits": credits,
        "total": total,
        "tokensWindow": window_tokens,
        "tokensPerDay": round(window_tokens / len(completed), 1) if completed else None,
        "trend": _trend(recent_tokens, previous_tokens),
        "creditsBurnPerDay": burn,
        "daysLeft": days_left,
        "exhaustDate": exhaust_date,
    }


def _key_row(
    payload: dict[str, Any],
    completed: list[dict[str, Any]],
) -> dict[str, Any]:
    key_id = str(payload.get("id") or "")
    today_tokens = int(payload.get("dayTokens") or 0)
    today_requests = int(payload.get("dayRequests") or 0)
    token_limit = int(payload.get("dailyTokenLimit") or 0)
    request_limit = int(payload.get("dailyRequestLimit") or 0)
    window_tokens = sum(_tokens_of(row.get("keys"), key_id) for row in completed)
    avg_tokens = round(window_tokens / len(completed), 1) if completed else None

    token_percent = round(today_tokens / token_limit, 4) if token_limit > 0 else None
    request_percent = round(today_requests / request_limit, 4) if request_limit > 0 else None
    over = (token_limit > 0 and today_tokens >= token_limit) or (
        request_limit > 0 and today_requests >= request_limit
    )
    watch = (not over) and (
        (token_percent is not None and token_percent >= WATCH_PERCENT)
        or (request_percent is not None and request_percent >= WATCH_PERCENT)
        or (avg_tokens is not None and token_limit > 0 and avg_tokens > token_limit)
    )
    return {
        "keyId": key_id,
        "label": str(payload.get("label") or "—"),
        "mask": str(payload.get("mask") or ""),
        "active": bool(payload.get("active")),
        "todayTokens": today_tokens,
        "todayRequests": today_requests,
        "tokenLimit": token_limit,
        "requestLimit": request_limit,
        "todayPercent": token_percent,
        "requestPercent": request_percent,
        "avgTokensPerDay": avg_tokens,
        "pace": "over" if over else "watch" if watch else "ok",
    }


def build_forecast(
    series: list[dict[str, Any]],
    accounts: list[dict[str, Any]],
    keys: list[dict[str, Any]],
    *,
    today: date,
    window: int = DEFAULT_WINDOW,
) -> dict[str, Any]:
    """把日账本 + 账号快照 + Key 记录算成预测视图。

    `series` 是 `GatewayStore.daily(days)` 的结果（含今天）；
    只有 `date < today` 的**完整日**参与均值与趋势。
    """
    window = max(3, min(int(window or DEFAULT_WINDOW), 90))
    today_key = today.isoformat()
    completed = [
        row
        for row in series
        if isinstance(row, dict) and str(row.get("date") or "") < today_key
    ][-window:]
    window_start = today - timedelta(days=window - 1)

    active_days = sum(1 for row in completed if int(row.get("requests") or 0) > 0)
    tokens_window = sum(int(row.get("tokens") or 0) for row in completed)
    requests_window = sum(int(row.get("requests") or 0) for row in completed)
    # 只有 1 天有请求时不给「日均」：单日样本说明不了趋势，宁可显示数据不足。
    tokens_per_day = (
        round(tokens_window / len(completed), 1) if completed and active_days >= 2 else None
    )

    model_tokens: dict[str, int] = {}
    for row in completed:
        models = row.get("models")
        if not isinstance(models, dict):
            continue
        for name, slot in models.items():
            if isinstance(slot, dict):
                model_tokens[str(name)] = model_tokens.get(str(name), 0) + int(
                    slot.get("tokens") or 0
                )
    top_model = max(model_tokens, key=lambda name: model_tokens[name]) if model_tokens else None

    account_rows = [
        _account_row(item, completed, window_start=window_start, today=today)
        for item in accounts
        if isinstance(item, dict)
    ]
    account_rows.sort(
        key=lambda row: (
            row["daysLeft"] is None,
            row["daysLeft"] if row["daysLeft"] is not None else 0.0,
        )
    )
    key_rows = [_key_row(item, completed) for item in keys if isinstance(item, dict)]
    active_keys = sum(
        1
        for row in key_rows
        if any(_tokens_of(item.get("keys"), str(row["keyId"])) > 0 for item in completed)
    )

    return {
        "window": window,
        "series": series,
        "accounts": account_rows,
        "keys": key_rows,
        "totals": {
            "tokensWindow": tokens_window,
            "tokensPerDay": tokens_per_day,
            "requestsWindow": requests_window,
            "activeDays": active_days,
            "activeKeys": active_keys,
            "topModel": top_model,
            "confidence": _confidence(active_days),
        },
    }
