"""用量预测的纯逻辑测试：数据不够时必须如实返回 `None` / `insufficient`。"""

from __future__ import annotations

from datetime import date, timedelta

from chaierww.domain.gateway_forecast import build_forecast

TODAY = date(2026, 11, 2)


def _row(
    day: date,
    *,
    tokens: int = 0,
    requests: int = 0,
    accounts: dict | None = None,
    keys: dict | None = None,
    models: dict | None = None,
) -> dict:
    return {
        "date": day.isoformat(),
        "requests": requests,
        "ok": requests,
        "failed": 0,
        "promptTokens": tokens,
        "completionTokens": 0,
        "tokens": tokens,
        "accounts": accounts or {},
        "keys": keys or {},
        "models": models or {},
    }


def _series(active: dict[date, tuple[int, int]] | None = None) -> list[dict]:
    """14 天连续账本（含今天）；`active` 只描述有请求的完整日。"""
    active = active or {}
    rows: list[dict] = []
    for offset in range(13, -1, -1):
        day = TODAY - timedelta(days=offset)
        tokens, requests = active.get(day, (0, 0))
        rows.append(_row(day, tokens=tokens, requests=requests))
    return rows


def _forecast(
    series: list[dict],
    *,
    accounts: list[dict] | None = None,
    keys: list[dict] | None = None,
) -> dict:
    return build_forecast(series, accounts or [], keys or [], today=TODAY, window=7)


def test_empty_ledger_reports_insufficient_without_numbers():
    result = _forecast(_series())

    totals = result["totals"]
    assert totals["tokensPerDay"] is None
    assert totals["confidence"] == "insufficient"
    assert totals["topModel"] is None
    assert totals["activeDays"] == 0
    assert result["accounts"] == []
    assert result["keys"] == []


def test_two_active_days_is_medium_and_seven_is_high():
    two_days = _series({TODAY - timedelta(days=2): (900, 3), TODAY - timedelta(days=1): (300, 1)})
    result = _forecast(two_days)
    assert result["totals"]["activeDays"] == 2
    assert result["totals"]["confidence"] == "medium"
    # 均值按完整窗口 7 天稀释，不把两天总量当成一天。
    assert result["totals"]["tokensPerDay"] == round(1200 / 7, 1)

    seven_days = _series(
        {TODAY - timedelta(days=offset): (100, 1) for offset in range(1, 8)}
    )
    high = _forecast(seven_days)
    assert high["totals"]["activeDays"] == 7
    assert high["totals"]["confidence"] == "high"


def test_account_trend_boundaries():
    def account_series(recent: int, previous: int) -> list[dict]:
        rows = _series()
        # 直接按日期回填，避免依赖索引计算。
        for row in rows:
            day = date.fromisoformat(row["date"])
            offset = (TODAY - day).days
            if 1 <= offset <= 7:
                row["accounts"]["a"] = {"tokens": recent if offset <= 3 else previous}
        return rows

    account = {"id": "a", "name": "A", "state": "usable", "credits": None, "total": None}

    up = _forecast(account_series(200, 100), accounts=[account])["accounts"][0]
    down = _forecast(account_series(50, 100), accounts=[account])["accounts"][0]
    stable = _forecast(account_series(100, 100), accounts=[account])["accounts"][0]
    assert up["trend"] == "up"
    assert down["trend"] == "down"
    assert stable["trend"] == "stable"


def test_credit_burn_gives_exhaustion_date_and_ignores_topups():
    rows = _series({TODAY - timedelta(days=1): (10, 1)})
    account = {
        "id": "a",
        "name": "A",
        "state": "usable",
        "credits": 400.0,
        "total": 1000.0,
        "snapshots": [
            {"at": "2026-10-27T12:00:00", "credits": 1000.0},
            {"at": "2026-11-01T12:00:00", "credits": 400.0},
        ],
    }
    row = _forecast(rows, accounts=[account])["accounts"][0]
    assert row["creditsBurnPerDay"] == 120.0
    assert row["daysLeft"] == round(400 / 120, 1)
    # ceil(3.3) = 4 天 → 2026-11-06。
    assert row["exhaustDate"] == "2026-11-06"

    topped_up = dict(account)
    topped_up["snapshots"] = [
        {"at": "2026-10-27T12:00:00", "credits": 1000.0},
        {"at": "2026-11-01T12:00:00", "credits": 1200.0},
    ]
    topped = _forecast(rows, accounts=[topped_up])["accounts"][0]
    assert topped["creditsBurnPerDay"] is None
    assert topped["daysLeft"] is None
    assert topped["exhaustDate"] is None


def test_key_pace_watch_over_and_unlimited():
    rows = _series({TODAY - timedelta(days=1): (50, 1)})
    base = {
        "id": "k",
        "label": "Cursor",
        "mask": "sk-…abc",
        "active": True,
        "dayRequests": 10,
        "dailyRequestLimit": 0,
    }

    watch = _forecast(rows, keys=[{**base, "dayTokens": 80, "dailyTokenLimit": 100}])
    assert watch["keys"][0]["pace"] == "watch"
    assert watch["keys"][0]["todayPercent"] == 0.8

    over = _forecast(rows, keys=[{**base, "dayTokens": 100, "dailyTokenLimit": 100}])
    assert over["keys"][0]["pace"] == "over"

    unlimited = _forecast(rows, keys=[{**base, "dayTokens": 500, "dailyTokenLimit": 0}])
    assert unlimited["keys"][0]["todayPercent"] is None
    assert unlimited["keys"][0]["pace"] == "ok"


def test_accounts_are_sorted_by_days_left_with_unknown_last():
    rows = _series({TODAY - timedelta(days=1): (10, 1)})
    accounts = [
        {"id": "none", "name": "None", "state": "usable", "credits": None, "total": None},
        {
            "id": "soon",
            "name": "Soon",
            "state": "usable",
            "credits": 60.0,
            "total": 100.0,
            "snapshots": [
                {"at": "2026-10-27T12:00:00", "credits": 100.0},
                {"at": "2026-11-01T12:00:00", "credits": 60.0},
            ],
        },
        {
            "id": "later",
            "name": "Later",
            "state": "usable",
            "credits": 600.0,
            "total": 1000.0,
            "snapshots": [
                {"at": "2026-10-27T12:00:00", "credits": 700.0},
                {"at": "2026-11-01T12:00:00", "credits": 600.0},
            ],
        },
    ]
    result = _forecast(rows, accounts=accounts)
    assert [item["accountId"] for item in result["accounts"]] == ["soon", "later", "none"]
