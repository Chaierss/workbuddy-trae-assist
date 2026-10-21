"""用量解析与合并视图测试。"""

from __future__ import annotations

from datetime import date

import pytest

from chaierww.domain import usage as mod
from chaierww.domain.usage import (
    AccountUsage,
    UsageService,
    aggregate_rows,
    normalize_row,
    parse_wb_usage_page,
    summarize,
)


def _row(rid: str, credit: float, model: str, time_str: str) -> dict:
    return {"requestId": rid, "credit": credit, "model": model, "requestTime": time_str}


# ---------- 行归一化 ----------


def test_normalize_row_parses_time_and_credit():
    row = normalize_row(_row("abc", 33.25, "hy4-preview", "2026-10-17 12:07:00"))
    assert row is not None
    assert row.credit == 33.25
    assert row.model == "hy4-preview"
    assert row.date == "2026-10-17"


def test_normalize_row_rejects_negative_credit():
    assert normalize_row(_row("x", -1, "m", "2026-10-17 12:00:00")) is None


def test_normalize_row_rejects_missing_time():
    assert normalize_row({"requestId": "x", "credit": 1.0}) is None


def test_normalize_row_handles_millis_timestamp():
    row = normalize_row({"requestId": "x", "credit": 1.0, "requestTime": 1700000000000})
    assert row is not None
    assert row.date == "2023-11-15"


# ---------- 分页解析 ----------


def test_parse_wb_usage_page_ok():
    response = {
        "code": 0,
        "data": {
            "total": 2,
            "data": [
                _row("a", 1.0, "m", "2026-10-17 12:00:00"),
                _row("b", 2.0, "m", "2026-10-18 12:00:00"),
            ],
        },
    }
    rows, total = parse_wb_usage_page(response)
    assert total == 2
    assert len(rows) == 2


def test_parse_wb_usage_page_bad_code_raises():
    with pytest.raises(ValueError):
        parse_wb_usage_page({"code": 10001, "msg": "已失效"})


def test_parse_wb_usage_page_bad_shape_raises():
    with pytest.raises(ValueError):
        parse_wb_usage_page({"code": 0, "data": {"total": 0, "data": "不是数组"}})


# ---------- 聚合 ----------


def test_aggregate_rows_buckets_today_week_month():
    today = date(2026, 10, 19)  # 周一
    rows = [
        normalize_row(_row("1", 10, "m", "2026-10-19 09:00:00")),
        normalize_row(_row("2", 20, "m", "2026-10-19 10:00:00")),
        normalize_row(_row("3", 30, "n", "2026-10-01 09:00:00")),
    ]
    agg = aggregate_rows([r for r in rows if r], today)
    assert agg["today_requests"] == 2
    assert agg["today_credit"] == 30
    assert agg["week_requests"] == 2  # 10-19 是周一，本周只有今天
    assert agg["month_requests"] == 3
    assert agg["requests"] == 3
    assert agg["credit_consumed"] == 60
    assert len(agg["models"]) == 2
    assert len(agg["daily"]) == 2


def test_aggregate_rows_empty():
    agg = aggregate_rows([], date(2026, 10, 19))
    assert agg["requests"] == 0
    assert agg["models"] == ()
    assert agg["daily"] == ()


# ---------- 汇总（积分不可互换） ----------


def test_summarize_totals_and_marks_non_interchangeable():
    accounts = [
        AccountUsage(
            account_id="a",
            display_name="甲",
            platform="workbuddy",
            scope="workbuddy-cn",
            detectable=True,
            requests=10,
            credit_consumed=100.0,
        ),
        AccountUsage(
            account_id="b",
            display_name="乙",
            platform="trae",
            scope="trae-cn",
            detectable=True,
            credits_consumed=500.0,
            credits_total=1200.0,
        ),
    ]
    summary = summarize(accounts)
    assert summary["accounts"] == 2
    assert summary["detectable"] == 2
    assert summary["totalRequests"] == 10
    assert summary["workbuddyCreditConsumed"] == 100.0
    assert summary["traeCreditsConsumed"] == 500.0


def test_summarize_excludes_undetectable_from_totals():
    accounts = [
        AccountUsage(
            account_id="a",
            display_name="甲",
            platform="workbuddy",
            scope="workbuddy-cn",
            detectable=False,
            error="信封",
        ),
    ]
    summary = summarize(accounts)
    assert summary["detectable"] == 0
    assert summary["totalRequests"] == 0
    assert summary["totalCreditConsumed"] == 0


# ---------- 服务 ----------


class _FakeAccount:
    def __init__(self, account_id="a1", scope="workbuddy-cn", name="甲", external_id="uid-1"):
        self.id = account_id
        self.scope = scope
        self.display_name = name
        self.external_id = external_id


class _FakeRepo:
    def __init__(self, credential=None):
        self._credential = credential

    def current_credential(self, account_id):
        if self._credential is None:
            from chaierww.domain.accounts.repository import AccountNotFound

            raise AccountNotFound(account_id)
        return self._credential


def _wb_credential():
    return {
        "format": "oauth-token",
        "accessToken": "tok",
        "uid": "uid-1",
        "domain": "www.workbuddy.cn",
    }


def test_usage_service_wb_flow(monkeypatch):
    rows = [normalize_row(_row("1", 10, "m", "2026-10-19 09:00:00"))]
    monkeypatch.setattr(
        mod, "fetch_wb_usage", lambda client, credential: ([r for r in rows if r], False)
    )
    monkeypatch.setattr(mod, "attach_conversations", lambda rows, uid: (rows, (), 0))
    service = UsageService(_FakeRepo(_wb_credential()))
    result = service.query_many([_FakeAccount()])
    assert result[0].detectable is True
    assert result[0].requests == 1
    assert result[0].credit_consumed == 10
    service.close()


def test_usage_service_trae_flow(monkeypatch):
    monkeypatch.setattr(mod, "fetch_trae_consumption", lambda client, credential: (500.0, 1200.0))
    service = UsageService(
        _FakeRepo({"format": "oauth-token", "accessToken": "jwt", "deviceId": "d", "host": ""})
    )
    result = service.query_many([_FakeAccount(scope="trae-cn")])
    assert result[0].detectable is True
    assert result[0].credits_consumed == 500.0
    assert result[0].credits_total == 1200.0
    assert result[0].requests == 0  # Trae 无请求明细
    service.close()


def test_usage_service_envelope_undetectable():
    import base64
    import json

    blob = json.dumps({"auth": {"domain": "www.workbuddy.cn"}})
    credential = {
        "format": "raw-file",
        "encoding": "base64",
        "data": base64.b64encode(blob.encode()).decode(),
    }
    service = UsageService(_FakeRepo(credential))
    result = service.query_many([_FakeAccount()])
    assert result[0].detectable is False
    assert "信封" in (result[0].error or "")
    service.close()


def test_usage_service_caches(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(client, credential):
        calls["n"] += 1
        return [], False

    monkeypatch.setattr(mod, "fetch_wb_usage", fake_fetch)
    monkeypatch.setattr(mod, "attach_conversations", lambda rows, uid: (rows, (), 0))
    service = UsageService(_FakeRepo(_wb_credential()), ttl=600)
    service.query_many([_FakeAccount()])
    service.query_many([_FakeAccount()])
    assert calls["n"] == 1
    service.query_many([_FakeAccount()], force=True)
    assert calls["n"] == 2
    service.close()


# ---------- 明细 ↔ 本机会话 ----------


def test_attach_conversations_groups_and_links(monkeypatch):
    from pathlib import Path

    from chaierww.domain.usage_turns import TurnRange

    rows = [
        normalize_row(_row("1", 10, "m", "2026-10-19 09:00:00")),
        normalize_row(_row("2", 5, "m", "2026-10-19 10:00:00")),
        normalize_row(_row("3", 7, "n", "2026-10-18 10:00:00")),
        normalize_row(_row("4", 1, "n", "2026-10-18 11:00:00")),
    ]
    monkeypatch.setattr(
        mod,
        "conversations_for_user",
        lambda uid: {"cid-1": ("标题一", "/w1"), "cid-2": ("标题二", "/w2")},
    )
    monkeypatch.setattr(
        mod,
        "lookup_turns",
        lambda ids, allowed=None: {
            "1": TurnRange("cid-1", Path("a.jsonl"), 0, 0),
            "2": TurnRange("cid-1", Path("a.jsonl"), 0, 0),
            "3": TurnRange("cid-2", Path("b.jsonl"), 0, 0),
        },
    )
    linked, conversations, count = mod.attach_conversations([r for r in rows if r], "uid-1")

    assert count == 3
    assert len(linked) == 4
    assert linked[0].conversation_id == "cid-1"
    assert linked[3].conversation_id is None  # 关联不到就留空，不猜
    assert [c.conversation_id for c in conversations] == ["cid-1", "cid-2"]
    assert conversations[0].requests == 2
    assert conversations[0].credit == 15
    assert conversations[0].title == "标题一"
    assert conversations[0].last_time == "2026-10-19 10:00:00"


def test_attach_conversations_without_local_conversations(monkeypatch):
    rows = [normalize_row(_row("1", 10, "m", "2026-10-19 09:00:00"))]
    monkeypatch.setattr(mod, "conversations_for_user", lambda uid: {})
    monkeypatch.setattr(mod, "lookup_turns", lambda ids, allowed=None: {})
    linked, conversations, count = mod.attach_conversations([r for r in rows if r], "uid-1")
    assert count == 0
    assert conversations == ()
    assert linked[0].conversation_id is None
