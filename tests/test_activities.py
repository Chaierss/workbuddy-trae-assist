"""活动检测与一键执行测试。

分三层：
1. **纯解析**：真实响应样例 → 活动项（不碰网络）。
2. **服务**：跳过/执行/失败的判定（用假 `_call` 顶掉 HTTP）。
3. **接口**：`GET /api/activities`、`POST /api/activities/run` 的形状。
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from chaierww.config import Settings
from chaierww.domain import activities as mod
from chaierww.domain import wb_extras as extras
from chaierww.domain.accounts.models import CredentialState
from chaierww.domain.accounts.repository import AccountNotFound
from chaierww.domain.activities import (
    RESULT_FAILED,
    RESULT_OK,
    RESULT_SKIPPED,
    STATE_DONE,
    STATE_IN_PROGRESS,
    STATE_NOT_STARTED,
    STATE_TODO,
    STATE_UNAVAILABLE,
    ActivitiesService,
    ActivityBusyError,
    parse_trae_checkin,
    parse_wb_checkin,
    parse_wb_streak,
    parse_wb_tasks,
    summarize,
)
from chaierww.main import create_app

# ---------- 样例（字段名照抄真实响应） ----------


def _wb_checkin(done: bool = True, active: bool = True) -> dict[str, Any]:
    return {
        "active": active,
        "today_checked_in": done,
        "streak_days": 13,
        "daily_credit": 100,
        "today_credit": 100 if done else 0,
        "week_checkin_days": 1,
        "is_streak_day": False,
        "streak_bonus_credit": 0,
        "theme_name": "Buddy加油站",
        "season": 10,
        "start_time": "2026-09-30 00:00:00",
        "end_time": "2026-10-31 23:59:59",
    }


def _wb_task(code: str, status: str, reward: int, title: str, current=1, target=1) -> dict:
    return {
        "task_code": code,
        "title": title,
        "accept_status": status,
        "reward_credit": reward,
        "progress": None if current is None else {"current": current, "target": target},
    }


def _wb_streak(days: int = 4, cards: int = 0, tier_status: str = "locked") -> dict[str, Any]:
    return {
        "streak": {
            "days": days,
            "month_total_days": 8,
            "next_tier": "7d",
            "next_tier_remaining": max(7 - days, 0),
        },
        "makeup_cards": {"balance": cards, "max": 4},
        "redemption_status": {
            "tier_7d_status": tier_status,
            "tier_14d_status": "locked",
            "tier_28d_status": "locked",
            "tiers": [
                {"tier": "7d", "days": 7, "credit": 0, "energy": 2, "cards": 1, "chances": 1},
                {"tier": "14d", "days": 14, "credit": 50, "energy": 3, "cards": 1, "chances": 1},
            ],
        },
    }


class _FakeAccount:
    """服务只读这四个字段，不必造完整的 Account 行。"""

    def __init__(
        self,
        account_id: str = "a1",
        scope: str = "workbuddy-cn",
        name: str = "账号甲",
        state: str = "unverified",
    ):
        self.id = account_id
        self.scope = scope
        self.display_name = name
        self.credential_state = CredentialState(state)


class _FakeRepo:
    def __init__(self, credential: dict[str, Any] | None):
        self._credential = credential
        self.states: dict[str, CredentialState] = {}
        self.runs: list[dict[str, Any]] = []

    def current_credential(self, account_id: str) -> dict[str, Any]:
        if self._credential is None:
            raise AccountNotFound(account_id)
        return self._credential

    def set_credential_state(self, account_id: str, state: CredentialState) -> None:
        self.states[account_id] = state

    def record_activity_runs(self, rows: list[dict[str, Any]]) -> None:
        self.runs.extend(rows)


def _wb_credential(token: str = "tok") -> dict[str, Any]:
    return {
        "format": "oauth-token",
        "accessToken": token,
        "uid": "uid-1",
        "domain": "www.workbuddy.cn",
    }


def _envelope_credential() -> dict[str, Any]:
    """客户端导入的凭据：auth.accessToken 不存在（DPAPI 信封）。"""
    blob = json.dumps({"auth": {"domain": "www.workbuddy.cn"}, "account": {"uid": "u"}})
    return {
        "format": "raw-file",
        "encoding": "base64",
        "data": base64.b64encode(blob.encode()).decode(),
    }


def _fake_calls(monkeypatch, *, status: tuple, claim: tuple | None = None) -> list[str]:
    """把 `_call` 换成走查表版本；返回被访问的 URL 列表用于断言。"""
    urls: list[str] = []

    def fake(client, method, url, headers, body=None):  # noqa: ANN001, ARG001
        urls.append(url)
        if url.endswith(mod.WB_CHECKIN_STATUS_PATH):
            return status
        if url.endswith(mod.WB_CHECKIN_PATH):
            return claim or (200, {"code": 0}, "")
        if url.endswith(mod.WB_TASKS_PATH):
            return 200, {"code": 0, "data": {"tasks": []}}, ""
        if url.endswith(mod.WB_STREAK_PATH):
            return 200, {"code": 0, "data": {}}, ""
        if url.endswith(mod.WB_LOTTERY_PATH):
            return 200, {"code": 0, "data": {"balance": 0}}, ""
        # 额外活动：默认「接口未开放」，用来验证它们只会被记为跳过。
        if url.endswith(extras.HEATMAP_PATH):
            return 200, {"code": 0, "data": {"cells": []}}, ""
        if url.endswith(extras.CLAIM_GIFT_PATH) or url.endswith(extras.CLAIM_COMPENSATION_PATH):
            return 404, {"code": 404, "msg": "not found"}, ""
        if url.endswith(extras.TRAVEL_PREFIX + "/config") or url.endswith(
            extras.TRAVEL_PREFIX + "/status"
        ):
            return 404, {"code": 404, "msg": "not found"}, ""
        if url.endswith(extras.SCHOOL_TASKS_PATH) or url.endswith(extras.SCHOOL_CONFIG_PATH):
            return 404, {"code": 404, "msg": "not found"}, ""
        if url.endswith(mod.TRAE_CHECKIN_STATUS_PATH):
            return status
        if url.endswith(mod.TRAE_CHECKIN_CLAIM_PATH):
            return claim or (200, {"code": 0, "message": "success"}, "")
        raise AssertionError(f"未预期的请求：{url}")

    monkeypatch.setattr(mod, "_call", fake)
    return urls


# ---------- WorkBuddy 签到解析 ----------


def test_wb_checkin_done_reports_granted_amount():
    item = parse_wb_checkin(_wb_checkin(done=True))
    assert item.state == STATE_DONE
    assert item.automatable is True
    assert item.group == mod.GROUP_DAILY
    # 「已领」不能写成「可得」—— 谎报收益
    assert item.reward_text == "今日 +100"
    assert "今日" in item.reward_text
    assert "连签 13 天" in item.note
    assert item.subtitle == "Buddy加油站 · 第 10 期"
    assert item.period_start == "2026-09-30 00:00:00"


def test_wb_checkin_pending_says_claimable_not_granted():
    item = parse_wb_checkin(_wb_checkin(done=False))
    assert item.state == STATE_TODO
    assert item.actionable is True
    assert item.reward_text == "可得 100"
    # 反例：把 daily_credit 当成已领，就会在这里出现「今日」
    assert "今日" not in item.reward_text


def test_wb_checkin_inactive_is_not_started_not_todo():
    item = parse_wb_checkin(_wb_checkin(done=False, active=False))
    assert item.state == STATE_NOT_STARTED
    assert item.actionable is False
    assert "没有进行中的签到活动" in item.note


# ---------- 成长任务 ----------


def test_wb_tasks_summarizes_unclaimed_and_marks_automatable():
    tasks = [
        _wb_task("chat_5", "claimed", 100, "和 AI 聊天 5 次", 5, 5),
        _wb_task("create_canvas", "claimed", 300, "体验设计创意模式"),
        # 可自动（有动作表覆盖）
        _wb_task("template_5", "not_accepted", 100, "使用 5 个模板", None, None),
        # 不可自动（expert 族，需真实操作）
        _wb_task("expert_chat", "not_accepted", 100, "和专家对话", None, None),
    ]
    item = parse_wb_tasks(tasks)
    assert item.state == STATE_TODO
    assert item.progress_text == "2/4 已领取"
    assert item.reward == 200
    assert "使用 5 个模板" in item.note
    # 有可自动的待办任务 → 允许一键执行
    assert item.automatable is True
    assert item.actionable is True
    assert item.manual_reason == ""


def test_wb_tasks_lists_each_manual_item_with_deep_link():
    """手动项要逐条列出来：标题 + 上游深链，客户端协议原样保留。"""
    tasks = [
        {
            "task_code": "expert_5",
            "accept_status": "not_accepted",
            "title": "召唤 5 次专家",
            "task_desc": "召唤 5 位不同的专家并发起对话。",
            "jump_url": "workbuddy://expert",
            "reward_credit": 100,
        },
        {
            "task_code": "wb_wechat_oa_subscribe_task",
            "accept_status": "not_accepted",
            "title": "关注官方公众号",
            "jump_url": "https://www.workbuddy.cn/events/follow-guide/",
        },
    ]
    item = parse_wb_tasks(tasks)
    assert item.automatable is False
    assert [entry["code"] for entry in item.manual_tasks] == [
        "expert_5",
        "wb_wechat_oa_subscribe_task",
    ]
    assert item.manual_tasks[0]["jumpUrl"] == "workbuddy://expert"
    assert item.manual_tasks[0]["desc"].startswith("召唤 5 位")
    assert item.manual_tasks[1]["jumpUrl"].startswith("https://")
    # 汇总链接仍取第一条有深链的手动项
    assert item.jump_url == "workbuddy://expert"
    # 已领取的任务不进手动清单
    assert parse_wb_tasks([{**tasks[0], "accept_status": "claimed"}]).manual_tasks == ()


def test_wb_tasks_all_pending_manual_is_not_automatable():
    # 全部待办都是 expert 族（无动作）→ 不可自动，须给理由
    tasks = [
        _wb_task("expert_chat", "not_accepted", 100, "和专家对话", None, None),
        _wb_task("skill_1", "not_accepted", 50, "使用技能", None, None),
    ]
    item = parse_wb_tasks(tasks)
    assert item.state == STATE_TODO
    assert item.automatable is False
    assert item.manual_reason
    assert item.actionable is False


def test_wb_tasks_all_claimed_is_done():
    item = parse_wb_tasks([_wb_task("chat_5", "claimed", 100, "聊天", 5, 5)])
    assert item.state == STATE_DONE
    assert item.reward is None
    assert item.note == ""


def test_wb_tasks_empty_is_unavailable_not_done():
    item = parse_wb_tasks([])
    # 上游没给任务 ≠ 任务都做完了
    assert item.state == STATE_UNAVAILABLE
    assert item.state != STATE_DONE
    assert "没有返回任何任务" in item.note


# ---------- 连签里程 ----------


def test_wb_streak_reports_progress_and_items():
    item = parse_wb_streak(_wb_streak(days=4, cards=0))
    assert item.state == STATE_IN_PROGRESS
    assert item.progress_text == "4/7 天"
    assert item.automatable is False
    assert "补签卡 0/4 张" in item.note
    assert "距 7d 档还差 3 天" in item.note


def test_wb_streak_with_makeup_card_is_todo_and_automatable():
    item = parse_wb_streak(_wb_streak(days=4, cards=1))
    assert item.state == STATE_TODO
    # 有补签卡 ⇒ 本工具能代做（补签/兑换/抽奖），不再是纯手动项
    assert item.automatable is True
    assert item.manual_reason == ""


def test_wb_streak_with_claimable_tier_is_todo():
    item = parse_wb_streak(_wb_streak(days=7, cards=0, tier_status="claimable"))
    assert item.state == STATE_TODO
    assert "可领档位：7d" in item.note


def test_wb_streak_counts_lottery_chances():
    item = parse_wb_streak(_wb_streak(days=4), chances=3)
    assert item.state == STATE_TODO
    assert "抽奖 3 次" in item.note


# ---------- Trae ----------


def test_trae_checkin_done_and_pending():
    done = parse_trae_checkin({"code": 0, "checked_in": True, "enable": True, "credits": 100})
    assert done.state == STATE_DONE
    assert done.reward == 100
    assert done.automatable is True

    pending = parse_trae_checkin(
        {"code": 0, "checked_in": False, "enable": True, "credits": 100, "extra_credits": 20}
    )
    assert pending.state == STATE_TODO
    assert pending.actionable is True
    assert "额外奖励 20" in pending.note


def test_trae_checkin_disabled_is_not_started():
    item = parse_trae_checkin({"code": 0, "checked_in": False, "enable": False, "credits": 0})
    assert item.state == STATE_NOT_STARTED
    assert item.actionable is False


# ---------- 汇总 ----------


def test_summarize_separates_actionable_from_manual():
    from chaierww.domain.activities import AccountActivities

    account = AccountActivities(
        account_id="a1",
        display_name="甲",
        platform="workbuddy",
        scope="workbuddy-cn",
        detectable=True,
        items=(
            parse_wb_checkin(_wb_checkin(done=False)),
            parse_wb_tasks([_wb_task("x", "claimed", 100, "任务")]),
            parse_wb_streak(_wb_streak(days=3, cards=1)),
        ),
    )
    summary = summarize([account])
    assert summary["accounts"] == 1
    assert summary["detectable"] == 1
    # 签到 + 连签（有补签卡可代做）都可以自动执行
    assert summary["actionable"] == 2
    assert summary["done"] == 1
    assert summary["manual"] == 0


# ---------- 服务：检测 ----------


def test_detect_marks_envelope_account_undetectable(monkeypatch):
    _fake_calls(monkeypatch, status=(200, {"code": 0, "data": _wb_checkin()}, ""))
    service = ActivitiesService(_FakeRepo(_envelope_credential()))
    result = service.detect([_FakeAccount()])
    # 凭据解不开就不该发请求，也不该说「没有活动」
    assert result[0].detectable is False
    assert result[0].items == ()
    assert "加密信封" in (result[0].error or "")
    service.close()


def test_detect_without_credential_is_not_an_exception():
    service = ActivitiesService(_FakeRepo(None))
    result = service.detect([_FakeAccount()])
    assert result[0].detectable is False
    assert result[0].error == "没有可用凭据"
    service.close()


def test_detect_failure_shows_up_as_item_not_disappearing(monkeypatch):
    _fake_calls(monkeypatch, status=(200, {"code": 10001, "msg": "今天已签到"}, ""))
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    result = service.detect([_FakeAccount()])
    assert result[0].detectable is True  # 任务/连签查到了
    checkin = next(item for item in result[0].items if item.key == "workbuddy.daily_checkin")
    # 签到接口失败 → 该项不可用，但仍在列表里（消失比报错更困惑）
    assert checkin.state == STATE_UNAVAILABLE
    assert checkin.note
    service.close()


def test_detect_caches_until_forced(monkeypatch):
    urls = _fake_calls(monkeypatch, status=(200, {"code": 0, "data": _wb_checkin()}, ""))
    service = ActivitiesService(_FakeRepo(_wb_credential()), ttl=600)
    service.detect([_FakeAccount()])
    first = len(urls)
    service.detect([_FakeAccount()])
    assert len(urls) == first  # 命中缓存，没有再打上游
    service.detect([_FakeAccount()], force=True)
    assert len(urls) == first * 2
    service.close()


# ---------- 服务：一键执行 ----------


def test_run_skips_already_checked_in_without_writing(monkeypatch):
    urls = _fake_calls(monkeypatch, status=(200, {"code": 0, "data": _wb_checkin(done=True)}, ""))
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    report = service.run([_FakeAccount()])
    item = report["results"][0]
    assert item["status"] == RESULT_SKIPPED
    assert item["message"] == "今日 +100"
    # 已签到 ⇒ 绝不能发写请求
    assert not any(url.endswith(mod.WB_CHECKIN_PATH) for url in urls)
    service.close()


def test_run_claims_when_pending(monkeypatch):
    _fake_calls(
        monkeypatch,
        status=(200, {"code": 0, "data": _wb_checkin(done=False)}, ""),
        claim=(200, {"code": 0, "data": {"today_credit": 100}}, ""),
    )
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    report = service.run([_FakeAccount()])
    item = report["results"][0]
    assert item["status"] == RESULT_OK
    assert item["reward"] == 100
    # 额外活动（礼包/补偿/补签/兑换/抽奖/旅行/夜猫子/开学季）都是「跳过」，
    # 只让签到这一项计为成功。
    summary = report["summary"]
    assert summary["total"] == len(report["results"])
    assert summary["ok"] == 1
    assert summary["failed"] == 0
    service.close()


def test_run_treats_already_checked_in_code_as_skipped(monkeypatch):
    _fake_calls(
        monkeypatch,
        status=(200, {"code": 0, "data": _wb_checkin(done=False)}, ""),
        claim=(400, {"code": 10001, "msg": "今天已签到，请明天再来"}, ""),
    )
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    item = service.run([_FakeAccount()])["results"][0]
    # 上游说「已签到」不是失败，也不是成功——是跳过
    assert item["status"] == RESULT_SKIPPED
    assert "已签到" in item["message"]
    service.close()


def test_run_reports_claim_failure(monkeypatch):
    _fake_calls(
        monkeypatch,
        status=(200, {"code": 0, "data": _wb_checkin(done=False)}, ""),
        claim=(500, {"code": 500, "msg": "服务繁忙"}, ""),
    )
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    item = service.run([_FakeAccount()])["results"][0]
    assert item["status"] == RESULT_FAILED
    assert "服务繁忙" in item["message"]
    service.close()


def test_run_skips_inactive_activity(monkeypatch):
    urls = _fake_calls(
        monkeypatch, status=(200, {"code": 0, "data": _wb_checkin(done=False, active=False)}, "")
    )
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    item = service.run([_FakeAccount()])["results"][0]
    assert item["status"] == RESULT_SKIPPED
    assert "没有进行中的签到活动" in item["message"]
    assert not any(url.endswith(mod.WB_CHECKIN_PATH) for url in urls)
    service.close()


def test_run_on_trae_claims_after_status(monkeypatch):
    _fake_calls(
        monkeypatch,
        status=(200, {"code": 0, "checked_in": False, "enable": True, "credits": 100}, ""),
    )
    service = ActivitiesService(
        _FakeRepo({"format": "oauth-token", "accessToken": "jwt", "deviceId": "d", "host": ""})
    )
    item = service.run([_FakeAccount(scope="trae-cn")])["results"][0]
    assert item["status"] == RESULT_OK
    assert item["platform"] == "trae"
    assert item["reward"] == 100
    service.close()


def test_trae_claim_retries_on_busy(monkeypatch):
    """9074 是上游冷却式限流：立刻再打没用，退避重试一次就能过。"""
    trail = [
        (200, {"code": 9074, "message": "当前参与用户太多"}, ""),
        (200, {"code": 0, "message": "success"}, ""),
    ]
    urls: list[str] = []

    def fake(client, method, url, headers, body=None):  # noqa: ANN001, ARG001
        urls.append(url)
        if url.endswith(mod.TRAE_CHECKIN_STATUS_PATH):
            return 200, {"code": 0, "checked_in": False, "enable": True, "credits": 100}, ""
        if url.endswith(mod.TRAE_CHECKIN_CLAIM_PATH):
            return trail.pop(0)
        raise AssertionError(f"未预期的请求：{url}")

    monkeypatch.setattr(mod, "_call", fake)
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)

    service = ActivitiesService(
        _FakeRepo({"format": "oauth-token", "accessToken": "jwt", "deviceId": "d", "host": ""})
    )
    item = service.run([_FakeAccount(scope="trae-cn")])["results"][0]
    assert item["status"] == RESULT_OK
    assert sum(1 for url in urls if url.endswith(mod.TRAE_CHECKIN_CLAIM_PATH)) == 2
    service.close()


def test_trae_claim_device_taken_is_reported_as_failure(monkeypatch):
    """9095 是「这台设备今天的名额被别的账号占了」：本账号确实没签成，不能粉饰成功。"""
    _fake_calls(
        monkeypatch,
        status=(200, {"code": 0, "checked_in": False, "enable": True, "credits": 100}, ""),
        claim=(200, {"code": 9095, "message": "当前设备今日已经签到，请明日再来哦～"}, ""),
    )
    service = ActivitiesService(
        _FakeRepo({"format": "oauth-token", "accessToken": "jwt", "deviceId": "d", "host": ""})
    )
    item = service.run([_FakeAccount(scope="trae-cn")])["results"][0]
    assert item["status"] == RESULT_FAILED
    assert "设备" in item["message"]
    service.close()


def test_run_invalidates_cache(monkeypatch):
    _fake_calls(monkeypatch, status=(200, {"code": 0, "data": _wb_checkin(done=True)}, ""))
    service = ActivitiesService(_FakeRepo(_wb_credential()), ttl=600)
    service.detect([_FakeAccount()])
    service.run([_FakeAccount()])
    # 执行过就必须让缓存失效，否则前端刷新看到的还是旧状态
    assert service._cache == {}
    service.close()


def test_run_refuses_concurrent_execution(monkeypatch):
    _fake_calls(monkeypatch, status=(200, {"code": 0, "data": _wb_checkin(done=True)}, ""))
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    assert service._run_lock.acquire(blocking=False)
    try:
        with pytest.raises(ActivityBusyError):
            service.run([_FakeAccount()])
    finally:
        service._run_lock.release()
    service.close()


# ---------- 接口 ----------


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(home=tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def _seed_account(client: TestClient) -> str:
    from chaierww.domain.switching import encode_payload

    created = client.app.state.repository.upsert(
        scope="workbuddy-cn",
        external_id="act-1",
        display_name="活动账号",
        credential=encode_payload(b'{"login": "fake"}'),
    )
    return created.account.id


def test_api_activities_returns_items_and_summary(client, monkeypatch):
    from chaierww.domain.activities import AccountActivities

    account_id = _seed_account(client)
    monkeypatch.setattr(
        client.app.state.activities,
        "detect",
        lambda accounts, force=False: [
            AccountActivities(
                account_id=account_id,
                display_name="活动账号",
                platform="workbuddy",
                scope="workbuddy-cn",
                detectable=True,
                items=(parse_wb_checkin(_wb_checkin(done=False)),),
            )
        ],
    )
    body = client.get("/api/activities").json()
    assert body["summary"]["actionable"] == 1
    activity = body["accounts"][0]["activities"][0]
    assert activity["state"] == STATE_TODO
    assert activity["automatable"] is True
    assert activity["key"] == "workbuddy.daily_checkin"


# ---------- 活动目录 ----------


def test_catalog_keys_match_parser_outputs():
    """目录 key 必须与解析器/额外活动输出的 key 一一对应，防止目录与检测漂移。"""
    parsed = {
        parse_wb_checkin(_wb_checkin(done=True)).key,
        parse_wb_tasks([{"task_code": "chat_5", "accept_status": "not_accepted"}]).key,
        parse_wb_streak({}).key,
        parse_trae_checkin({"checked_in": True}).key,
        # 由 wb_extras 检测、经 _extra_item 输出的三项
        "workbuddy.travel",
        "workbuddy.black_cat",
        "workbuddy.school",
    }
    catalog = {entry["key"] for entry in mod.ACTIVITY_CATALOG}
    assert catalog == parsed
    assert len(catalog) == len(mod.ACTIVITY_CATALOG)  # 目录内不重复


def test_catalog_entries_have_required_fields():
    for entry in mod.ACTIVITY_CATALOG:
        assert entry["key"] and entry["platform"] and entry["title"]
        assert entry["group"] in (mod.GROUP_DAILY, mod.GROUP_SEASONAL)
        assert entry["description"] and entry["rewardHint"]
        assert isinstance(entry["automatable"], bool)


def test_api_activities_catalog_is_static_no_network(client, monkeypatch):
    """目录接口零网络：把上游调用打桩成「一打就炸」，它也必须正常返回。"""

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("目录接口不该访问上游")

    monkeypatch.setattr(mod, "_call", boom)
    body = client.get("/api/activities/catalog").json()
    keys = [entry["key"] for entry in body["entries"]]
    assert keys == [entry["key"] for entry in mod.ACTIVITY_CATALOG]
    assert "workbuddy.daily_checkin" in keys


def test_api_activities_run_returns_report(client, monkeypatch):
    _seed_account(client)
    monkeypatch.setattr(
        client.app.state.activities,
        "run",
        lambda accounts, keys=None: {
            "results": [
                {
                    "accountId": accounts[0].id,
                    "displayName": accounts[0].display_name,
                    "platform": "workbuddy",
                    "key": "workbuddy.daily_checkin",
                    "title": "每日签到",
                    "status": RESULT_OK,
                    "message": "签到成功 +100",
                    "reward": 100.0,
                }
            ],
            "summary": {"total": 1, "ok": 1, "skipped": 0, "failed": 0},
        },
    )
    response = client.post("/api/activities/run", json={})
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["ok"] == 1
    assert body["results"][0]["status"] == RESULT_OK


def test_api_activities_run_scope_filter(client, monkeypatch):
    _seed_account(client)
    seen: list[list[str]] = []

    def fake_run(accounts, keys=None):  # noqa: ANN001
        seen.append([account.id for account in accounts])
        return {"results": [], "summary": {"total": 0, "ok": 0, "skipped": 0, "failed": 0}}

    monkeypatch.setattr(client.app.state.activities, "run", fake_run)
    client.post("/api/activities/run", json={"accountIds": ["不存在的 id"]})
    assert seen[-1] == []


# ---------- 服务：派猫猫旅行按阶段执行 ----------


def _travel_only_calls(monkeypatch, *, state: str) -> list[tuple[str, str]]:
    """只认旅行四个端点的传输层；返回 (method, path) 记录用于断言。"""
    seen: list[tuple[str, str]] = []

    def fake(client, method, url, headers, body=None):  # noqa: ANN001, ARG001
        # origin 由凭据的 domain 推出来（也可能被换成 codebuddy.cn），只认路径。
        _, _, tail = url.partition(extras.TRAVEL_PREFIX)
        path = extras.TRAVEL_PREFIX + tail
        seen.append((method, path))
        if path == extras.TRAVEL_PREFIX + "/config":
            return (
                200,
                {"code": 0, "data": {"enabled": True, "locations": [{"id": "l1", "name": "西湖"}]}},
                "",
            )
        if path == extras.TRAVEL_PREFIX + "/status":
            return 200, {"code": 0, "data": {"state": state, "record_id": 9}}, ""
        if path == extras.TRAVEL_PREFIX + "/depart":
            return 200, {"code": 0, "data": {}}, ""
        if path == extras.TRAVEL_PREFIX + "/claim":
            return 200, {"code": 0, "data": {"reward_credit": 30}}, ""
        raise AssertionError(f"未预期的请求：{method} {url}")

    monkeypatch.setattr(mod, "_call", fake)
    return seen


def _posted_paths(seen: list[tuple[str, str]]) -> list[str]:
    return [path for method, path in seen if method == "POST"]


def test_run_travel_stage_depart_does_not_touch_claim(monkeypatch):
    """计划任务里「派出」只该发一次写请求，不能顺手把领奖也做了。"""
    seen = _travel_only_calls(monkeypatch, state="idle")
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    body = service.run_travel_stage([_FakeAccount()], stage="depart")

    result = body["results"][0]
    assert result["status"] == RESULT_OK
    assert result["key"] == "workbuddy.travel"
    assert _posted_paths(seen) == [extras.TRAVEL_PREFIX + "/depart"]
    assert body["summary"]["ok"] == 1
    service.close()


def test_run_travel_stage_claim_does_not_touch_depart(monkeypatch):
    seen = _travel_only_calls(monkeypatch, state="arrived")
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    body = service.run_travel_stage([_FakeAccount()], stage="claim")

    result = body["results"][0]
    assert result["status"] == RESULT_OK
    assert result["reward"] == 30
    assert _posted_paths(seen) == [extras.TRAVEL_PREFIX + "/claim"]
    service.close()


def test_run_travel_stage_claim_mid_flight_is_skipped_with_reason(monkeypatch):
    _travel_only_calls(monkeypatch, state="traveling")
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    body = service.run_travel_stage([_FakeAccount()], stage="claim")

    result = body["results"][0]
    assert result["status"] == RESULT_SKIPPED
    assert "路上" in result["message"]
    service.close()


def test_run_travel_stage_ignores_non_workbuddy_accounts(monkeypatch):
    """派猫猫是 WorkBuddy 专属活动，Trae 账号不该被记成「失败」。"""

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("非 WorkBuddy 账号不该发请求")

    monkeypatch.setattr(mod, "_call", boom)
    service = ActivitiesService(
        _FakeRepo({"format": "oauth-token", "accessToken": "jwt", "deviceId": "d", "host": ""})
    )
    body = service.run_travel_stage([_FakeAccount(scope="trae-cn")], stage="depart")

    assert body["results"] == []
    assert body["summary"]["total"] == 0
    service.close()


def test_run_travel_stage_invalidates_cache(monkeypatch):
    _travel_only_calls(monkeypatch, state="idle")
    service = ActivitiesService(_FakeRepo(_wb_credential()), ttl=600)
    service._cache = {"stale": object()}
    service.run_travel_stage([_FakeAccount()], stage="depart")
    assert service._cache == {}
    service.close()


# ---------- 失败重试（只重试值得重试的号） ----------


def _sequence_calls(monkeypatch, statuses: list[tuple]) -> list[str]:
    """按顺序吐出 status 响应；claim 一律成功。返回被访问的 URL 列表。"""
    urls: list[str] = []
    queue = list(statuses)

    def fake(client, method, url, headers, body=None):  # noqa: ANN001, ARG001
        urls.append(url)
        if url.endswith(mod.WB_CHECKIN_STATUS_PATH):
            return queue.pop(0)
        if url.endswith(mod.WB_CHECKIN_PATH):
            return 200, {"code": 0, "data": {"today_credit": 5}}, ""
        raise AssertionError(f"未预期的请求：{url}")

    monkeypatch.setattr(mod, "_call", fake)
    monkeypatch.setattr(mod.time, "sleep", lambda _seconds: None)
    return urls


def test_run_retries_only_retryable_failures_and_merges_rows(monkeypatch):
    """首轮 5xx → 退避后只重试这个号；重试轮的结论顶替首轮，仍是一行。"""
    urls = _sequence_calls(
        monkeypatch,
        [
            (503, None, "HTTP 503"),
            (200, {"code": 0, "data": _wb_checkin(done=False, active=True)}, ""),
        ],
    )
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    body = service.run([_FakeAccount()], include_growth=False, retry_failed=True)

    assert body["summary"]["retried"] == 1
    assert body["summary"]["retryOk"] == 1
    assert body["summary"]["failed"] == 0
    assert len(body["results"]) == 1
    result = body["results"][0]
    assert result["status"] == RESULT_OK
    assert result["attempt"] == 2
    assert sum(1 for url in urls if url.endswith(mod.WB_CHECKIN_STATUS_PATH)) == 2
    service.close()


def test_run_does_not_retry_auth_failures(monkeypatch):
    """401 是「登录失效」，重试只会再失败一次 —— 首轮就该停。"""
    urls = _sequence_calls(monkeypatch, [(401, {"code": 401, "msg": "unauthorized"}, "")])
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    body = service.run([_FakeAccount()], include_growth=False, retry_failed=True)

    assert body["summary"]["retried"] == 0
    assert body["summary"]["retryOk"] == 0
    assert body["summary"]["failed"] == 1
    assert body["results"][0]["retryable"] is False
    assert body["results"][0]["attempt"] == 1
    assert len(urls) == 1
    service.close()


def test_run_retry_flag_off_keeps_single_attempt(monkeypatch):
    """默认不重试：同一份 5xx 响应只发一次请求。"""
    urls = _sequence_calls(monkeypatch, [(503, None, "HTTP 503")])
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    body = service.run([_FakeAccount()], include_growth=False)

    assert body["summary"]["retried"] == 0
    assert body["results"][0]["retryable"] is True
    assert len(urls) == 1
    service.close()


def test_401_marks_relogin_and_records_run(monkeypatch):
    """活动遇 401 = 登录失效：必须回写账号状态，并落一条执行流水。"""
    _sequence_calls(monkeypatch, [(401, {"code": 401, "msg": "unauthorized"}, "")])
    repo = _FakeRepo(_wb_credential())
    service = ActivitiesService(repo)
    body = service.run([_FakeAccount()], include_growth=False)

    assert body["results"][0]["status"] == RESULT_FAILED
    assert repo.states["a1"] is CredentialState.RELOGIN_REQUIRED
    assert len(repo.runs) == 1
    assert repo.runs[0]["status"] == RESULT_FAILED
    service.close()


def test_waf_10085_does_not_change_credential_state(monkeypatch):
    """WAF 指纹拦截不是令牌失效：一次网关抖动不能把好号标成需重登。"""
    _sequence_calls(monkeypatch, [(403, {"code": 10085, "msg": "blocked"}, "")])
    repo = _FakeRepo(_wb_credential())
    service = ActivitiesService(repo)
    service.run([_FakeAccount()], include_growth=False)

    assert repo.states == {}
    service.close()


def test_relogin_account_is_skipped_before_hitting_upstream(monkeypatch):
    """预检：已确认失效的号不发任何请求，直接列为待人工处理。"""
    urls = _sequence_calls(monkeypatch, [])
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    account = _FakeAccount(state="relogin_required")
    body = service.run([account], include_growth=False)

    assert body["results"][0]["status"] == RESULT_SKIPPED
    assert "重新扫码登录" in body["results"][0]["message"]
    assert urls == []
    service.close()
