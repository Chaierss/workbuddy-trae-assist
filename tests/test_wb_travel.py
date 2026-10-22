"""派猫猫旅行阶段拆分测试。

原来的 `run_travel` 把「派出」和「回领」挤在一个函数里：谁先命中就 return，
于是 `idle` 那天只派不领、`arrived` 那天只领不派，一天只能做一件。拆开以后
`travel_depart` / `travel_claim` 各自可被独立计划任务调用，这里逐个状态验证
它们**只做自己那件事**，以及组合版 `run_travel` 仍然保持「领优先」的次序。
"""

from __future__ import annotations

from typing import Any

import pytest

from chaierww.domain import wb_extras as extras

ORIGIN = "https://www.workbuddy.cn"
HEADERS = {"Authorization": "Bearer tok"}

CONFIG_URL = f"{ORIGIN}{extras.TRAVEL_PREFIX}/config"
STATUS_URL = f"{ORIGIN}{extras.TRAVEL_PREFIX}/status"
DEPART_URL = f"{ORIGIN}{extras.TRAVEL_PREFIX}/depart"
CLAIM_URL = f"{ORIGIN}{extras.TRAVEL_PREFIX}/claim"


def _travel_backend(
    *,
    state: str,
    daily_limit_reached: bool = False,
    record_id: int | None = 42,
    locations: list[dict[str, Any]] | None = None,
    enabled: bool = True,
    status_ok: bool = True,
    depart_response: tuple[int, Any] | None = None,
    claim_response: tuple[int, Any] | None = None,
) -> tuple[Any, list[tuple[str, str, Any]]]:
    """造一个只认旅行四个端点的传输层，返回 (call, 请求记录)。

    请求记录是 `(method, url, body)`，用来断言「该打的打了、不该打的一次没打」。
    """
    spots = locations if locations is not None else [{"id": "loc_1", "name": "西湖"}]
    calls: list[tuple[str, str, Any]] = []

    def call(method: str, url: str, headers: dict[str, str], body: Any | None) -> tuple[int, Any]:
        calls.append((method, url, body))
        if url == CONFIG_URL:
            return 200, {"code": 0, "data": {"enabled": enabled, "locations": spots}}
        if url == STATUS_URL:
            if not status_ok:
                return 404, {"code": 404, "msg": "not found"}
            data: dict[str, Any] = {
                "state": state,
                "daily_limit_reached": daily_limit_reached,
            }
            if record_id is not None:
                data["record_id"] = record_id
            return 200, {"code": 0, "data": data}
        if url == DEPART_URL:
            return depart_response or (200, {"code": 0, "data": {}})
        if url == CLAIM_URL:
            return claim_response or (200, {"code": 0, "data": {"reward_credit": 50}})
        raise AssertionError(f"未预期的请求：{method} {url}")

    return call, calls


def _posted(calls: list[tuple[str, str, Any]], url: str) -> list[Any]:
    return [body for method, target, body in calls if method == "POST" and target == url]


# ---------- travel_depart：只派出 ----------


def test_depart_from_idle_posts_first_location():
    call, calls = _travel_backend(state="idle")
    step = extras.travel_depart(call, ORIGIN, HEADERS)

    assert step.status == "ok"
    assert "西湖" in step.message
    assert _posted(calls, DEPART_URL) == [{"location_id": "loc_1"}]
    # 派出动作绝不该顺手去领
    assert _posted(calls, CLAIM_URL) == []


def test_depart_when_arrived_tells_user_to_claim_first():
    """猫回来了但还没领：不能直接再派一次，那样等于把奖励丢掉。"""
    call, calls = _travel_backend(state="arrived")
    step = extras.travel_depart(call, ORIGIN, HEADERS)

    assert step.status == "skipped"
    assert "先领再派" in step.message
    assert _posted(calls, DEPART_URL) == []


def test_depart_when_traveling_skips_without_posting():
    call, calls = _travel_backend(state="traveling")
    step = extras.travel_depart(call, ORIGIN, HEADERS)

    assert step.status == "skipped"
    assert "路上" in step.message
    assert _posted(calls, DEPART_URL) == []


def test_depart_respects_daily_limit():
    call, calls = _travel_backend(state="idle", daily_limit_reached=True)
    step = extras.travel_depart(call, ORIGIN, HEADERS)

    assert step.status == "skipped"
    assert "今日已派完" in step.message
    assert _posted(calls, DEPART_URL) == []


def test_depart_without_locations_skips_instead_of_failing():
    call, calls = _travel_backend(state="idle", locations=[])
    step = extras.travel_depart(call, ORIGIN, HEADERS)

    assert step.status == "skipped"
    assert "地点" in step.message
    assert _posted(calls, DEPART_URL) == []


def test_depart_translates_upstream_busy_into_skipped():
    """上游已经派过（`already traveling`）属于正常态，不该记成失败。"""
    call, _ = _travel_backend(
        state="idle",
        depart_response=(400, {"code": 400, "msg": "already traveling"}),
    )
    step = extras.travel_depart(call, ORIGIN, HEADERS)

    assert step.status == "skipped"
    assert "路上" in step.message


def test_depart_unexpected_upstream_error_is_failed():
    call, _ = _travel_backend(
        state="idle",
        depart_response=(500, {"code": 500, "msg": "boom"}),
    )
    step = extras.travel_depart(call, ORIGIN, HEADERS)

    assert step.status == "failed"
    assert "boom" in step.message


# ---------- travel_claim：只回领 ----------


def test_claim_when_arrived_posts_record_id():
    call, calls = _travel_backend(state="arrived")
    step = extras.travel_claim(call, ORIGIN, HEADERS)

    assert step.status == "ok"
    assert step.reward == 50
    assert _posted(calls, CLAIM_URL) == [{"record_id": 42}]
    # 领奖动作绝不该顺手再派一次
    assert _posted(calls, DEPART_URL) == []


def test_claim_without_record_id_posts_empty_body():
    call, calls = _travel_backend(state="arrived", record_id=None)
    step = extras.travel_claim(call, ORIGIN, HEADERS)

    assert step.status == "ok"
    assert _posted(calls, CLAIM_URL) == [{}]


def test_claim_when_traveling_says_cat_still_on_the_way():
    call, calls = _travel_backend(state="traveling")
    step = extras.travel_claim(call, ORIGIN, HEADERS)

    assert step.status == "skipped"
    assert "路上" in step.message
    assert _posted(calls, CLAIM_URL) == []


def test_claim_when_idle_says_nothing_to_collect():
    call, calls = _travel_backend(state="idle")
    step = extras.travel_claim(call, ORIGIN, HEADERS)

    assert step.status == "skipped"
    assert "还没出门" in step.message
    assert _posted(calls, CLAIM_URL) == []


# ---------- 开场检查：活动没开放 / 接口不可用 ----------


def test_depart_skips_when_activity_disabled():
    call, calls = _travel_backend(state="idle", enabled=False)
    step = extras.travel_depart(call, ORIGIN, HEADERS)

    assert step.status == "skipped"
    assert "未开放" in step.message
    assert _posted(calls, DEPART_URL) == []


def test_claim_skips_when_status_endpoint_is_down():
    """config 说开着、status 打不开：只能说查不到，不能当「已领」。"""
    call, calls = _travel_backend(state="arrived", status_ok=False)
    step = extras.travel_claim(call, ORIGIN, HEADERS)

    assert step.status == "skipped"
    assert "接口不可用" in step.message
    assert _posted(calls, CLAIM_URL) == []


# ---------- 阶段分发 ----------


@pytest.mark.parametrize(
    ("stage", "state", "expected_post"),
    [
        ("depart", "idle", DEPART_URL),
        ("claim", "arrived", CLAIM_URL),
    ],
)
def test_run_travel_stage_only_hits_its_own_endpoint(stage, state, expected_post):
    call, calls = _travel_backend(state=state)
    step = extras.run_travel_stage(call, ORIGIN, HEADERS, stage)

    assert step.status == "ok"
    posted = [url for method, url, _ in calls if method == "POST"]
    assert posted == [expected_post]


@pytest.mark.parametrize("stage", ["all", "", "unknown"])
def test_run_travel_stage_falls_back_to_combined(stage):
    """未知阶段按组合版处理——绝不静默什么都不做。"""
    call, calls = _travel_backend(state="idle")
    step = extras.run_travel_stage(call, ORIGIN, HEADERS, stage)

    # 组合版：先领（跳过，猫还没出门）再派（成功）
    assert step.status == "ok"
    assert _posted(calls, DEPART_URL) == [{"location_id": "loc_1"}]


def test_travel_stages_declares_every_supported_stage():
    assert extras.TRAVEL_STAGES == ("depart", "claim", "all")


# ---------- 组合版保持「领优先」 ----------


def test_run_travel_claims_before_departing():
    call, calls = _travel_backend(state="arrived")
    step = extras.run_travel(call, ORIGIN, HEADERS)

    assert step.status == "ok"
    assert step.reward == 50
    # 领到就别再派：同一次执行里做两件会撞每日上限
    assert _posted(calls, DEPART_URL) == []
    assert _posted(calls, CLAIM_URL) == [{"record_id": 42}]


def test_run_travel_departs_when_claim_has_nothing():
    call, calls = _travel_backend(state="idle")
    step = extras.run_travel(call, ORIGIN, HEADERS)

    assert step.status == "ok"
    assert "西湖" in step.message
    # 两个端点都打了，但只派出成功
    assert _posted(calls, CLAIM_URL) == []
    assert _posted(calls, DEPART_URL) == [{"location_id": "loc_1"}]


def test_run_travel_keeps_claim_reason_when_both_skip():
    """两件都做不了时，用回领的说明（「还在路上」比「今日已派完」更贴近现状）。"""
    call, _ = _travel_backend(state="traveling")
    step = extras.run_travel(call, ORIGIN, HEADERS)

    assert step.status == "skipped"
    assert "路上" in step.message
