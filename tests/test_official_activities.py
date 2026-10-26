"""官方活动展示与按活动执行测试。

覆盖三块：
1. 官方活动目录字段与官方入口白名单（`validate_official_catalog`）。
2. 活动级聚合 `summarize_campaigns` 的状态与统计口径。
3. `run(keys=...)` 只触发指定活动，以及接口层的参数校验。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chaierww.config import Settings
from chaierww.domain import activities as mod
from chaierww.domain import wb_extras as extras
from chaierww.domain.accounts.models import CredentialState
from chaierww.domain.accounts.repository import AccountNotFound
from chaierww.domain.activities import (
    CAMPAIGN_ACTIVE,
    CAMPAIGN_DONE,
    CAMPAIGN_NO_ACCOUNTS,
    CAMPAIGN_NOT_DETECTED,
    CAMPAIGN_PARTIAL,
    STATE_DONE,
    STATE_TODO,
    AccountActivities,
    ActivitiesService,
    ActivityItem,
    summarize_campaigns,
)
from chaierww.main import create_app

# ---------- 目录 ----------


def test_official_catalog_passes_validation():
    """目录字段、主题/图标枚举、官方入口白名单全部合法。"""
    mod.validate_official_catalog()
    assert len(mod.ACTIVITY_CATALOG) == 7
    assert {entry["key"] for entry in mod.ACTIVITY_CATALOG} == mod.ACTIVITY_KEYS


def test_catalog_entries_carry_official_fields():
    for entry in mod.ACTIVITY_CATALOG:
        assert entry["theme"] in mod.CAMPAIGN_THEMES
        assert entry["icon"] in mod.CAMPAIGN_ICONS
        assert entry["tags"] and entry["rules"] and entry["rewards"]
        assert entry["periodNote"] and entry["officialLabel"]
        assert isinstance(entry["featured"], bool)
        assert entry["sortOrder"] > 0
        assert entry["officialUrl"].startswith("https://")


def test_official_url_whitelist_rejects_foreign_host():
    """把手写错的官方地址塞进目录，校验必须拦下来。"""
    broken = dict(mod.ACTIVITY_CATALOG[0])
    broken["officialUrl"] = "https://evil.example.com/phish"
    with pytest.raises(ValueError, match="域名不在白名单"):
        original = mod.ACTIVITY_CATALOG
        mod.ACTIVITY_CATALOG = (broken,)  # type: ignore[assignment]
        try:
            mod.validate_official_catalog()
        finally:
            mod.ACTIVITY_CATALOG = original  # type: ignore[assignment]


# ---------- 活动级聚合 ----------


def _item(
    *,
    key: str = "workbuddy.daily_checkin",
    platform: str = "workbuddy",
    state: str = STATE_TODO,
    automatable: bool = True,
    period_start: str | None = None,
    period_end: str | None = None,
) -> ActivityItem:
    return ActivityItem(
        key=key,
        platform=platform,
        group=mod.GROUP_DAILY,
        title="每日签到",
        state=state,
        automatable=automatable,
        period_start=period_start,
        period_end=period_end,
    )


def _account(
    account_id: str,
    *,
    detectable: bool = True,
    items: tuple[ActivityItem, ...] = (),
    platform: str = "workbuddy",
) -> AccountActivities:
    scope = "workbuddy-cn" if platform == "workbuddy" else "trae-cn"
    return AccountActivities(
        account_id=account_id,
        display_name=f"账号-{account_id}",
        platform=platform,
        scope=scope,
        detectable=detectable,
        items=items,
    )


def _campaign(campaigns: list[dict], key: str = "workbuddy.daily_checkin") -> dict:
    return next(item for item in campaigns if item["key"] == key)


def test_summarize_campaigns_counts_status_buckets():
    accounts = [
        _account("a1", items=(_item(state=STATE_DONE),)),
        _account("a2", items=(_item(state=STATE_TODO, automatable=True),)),
        _account("a3", items=(_item(state=STATE_TODO, automatable=False),)),
    ]
    campaign = _campaign(summarize_campaigns(accounts))

    assert campaign["accountsTotal"] == 3
    assert campaign["detectable"] == 3
    assert campaign["done"] == 1
    assert campaign["todo"] == 1
    assert campaign["manual"] == 1
    assert campaign["actionable"] == 1
    assert campaign["allDone"] is False
    assert campaign["status"] == CAMPAIGN_PARTIAL
    # 每个账号只落一个桶。
    assert (
        campaign["done"]
        + campaign["todo"]
        + campaign["inProgress"]
        + campaign["manual"]
        + campaign["unavailable"]
        + campaign["notDetected"]
        == campaign["accountsTotal"]
    )


def test_summarize_campaigns_no_accounts_and_not_detected():
    no_accounts = _campaign(summarize_campaigns([]))
    assert no_accounts["status"] == CAMPAIGN_NO_ACCOUNTS
    assert no_accounts["accountsTotal"] == 0

    hidden = _campaign(summarize_campaigns([_account("a1", detectable=False)]))
    assert hidden["status"] == CAMPAIGN_NOT_DETECTED
    assert hidden["notDetected"] == 1
    assert hidden["detectable"] == 0


def test_summarize_campaigns_done_and_period_range():
    accounts = [
        _account(
            "a1",
            items=(_item(state=STATE_DONE, period_start="2026-09-01 00:00:00"),),
        ),
        _account(
            "a2",
            items=(
                _item(
                    state=STATE_DONE,
                    period_start="2026-09-30 00:00:00",
                    period_end="2026-10-31 23:59:59",
                ),
            ),
        ),
    ]
    campaign = _campaign(summarize_campaigns(accounts))
    assert campaign["status"] == CAMPAIGN_DONE
    assert campaign["allDone"] is True
    assert campaign["periodStart"] == "2026-09-01 00:00:00"
    assert campaign["periodEnd"] == "2026-10-31 23:59:59"


def test_summarize_campaigns_active_and_platform_isolation():
    accounts = [
        _account("w1", items=(_item(state=STATE_TODO),)),
        _account("t1", platform="trae", items=(_item(platform="trae", key="trae.daily_checkin"),)),
    ]
    campaigns = summarize_campaigns(accounts)
    workbuddy = _campaign(campaigns)
    trae = _campaign(campaigns, "trae.daily_checkin")

    assert workbuddy["status"] == CAMPAIGN_ACTIVE
    assert workbuddy["accountsTotal"] == 1
    assert trae["status"] == CAMPAIGN_ACTIVE
    assert trae["accountsTotal"] == 1


def test_campaign_payload_has_only_known_fields():
    campaign = _campaign(summarize_campaigns([]))
    expected = {
        "key",
        "platform",
        "group",
        "title",
        "subtitle",
        "description",
        "rewardHint",
        "automatable",
        "theme",
        "icon",
        "tags",
        "rules",
        "rewards",
        "periodNote",
        "officialUrl",
        "officialLabel",
        "featured",
        "sortOrder",
        "accountsTotal",
        "detectable",
        "done",
        "todo",
        "inProgress",
        "manual",
        "unavailable",
        "notDetected",
        "actionable",
        "allDone",
        "status",
        "periodStart",
        "periodEnd",
    }
    assert set(campaign) == expected


# ---------- 按活动执行 ----------


class _FakeAccount:
    def __init__(self, account_id: str = "a1", scope: str = "workbuddy-cn") -> None:
        self.id = account_id
        self.scope = scope
        self.display_name = "账号甲"
        self.credential_state = CredentialState.UNVERIFIED


class _FakeRepo:
    def __init__(self, credential: dict) -> None:
        self._credential = credential
        self.runs: list[dict] = []

    def current_credential(self, account_id: str) -> dict:
        if not self._credential:
            raise AccountNotFound(account_id)
        return self._credential

    def set_credential_state(self, account_id: str, state: CredentialState) -> None:
        return None

    def record_activity_runs(self, rows: list[dict]) -> None:
        self.runs.extend(rows)


def _wb_credential() -> dict:
    return {
        "format": "oauth-token",
        "accessToken": "tok",
        "uid": "uid-1",
        "domain": "www.workbuddy.cn",
    }


def _travel_only_calls(monkeypatch) -> list[tuple[str, str]]:
    """只认旅行端点；任何额外请求（签到、成长任务等）直接判失败。"""
    seen: list[tuple[str, str]] = []

    def fake(client, method, url, headers, body=None):  # noqa: ANN001, ARG001
        _, _, tail = url.partition(extras.TRAVEL_PREFIX)
        path = extras.TRAVEL_PREFIX + tail
        seen.append((method, path))
        if path == extras.TRAVEL_PREFIX + "/config":
            locations = [{"id": "l1", "name": "西湖"}]
            return (200, {"code": 0, "data": {"enabled": True, "locations": locations}}, "")
        if path == extras.TRAVEL_PREFIX + "/status":
            return 200, {"code": 0, "data": {"state": "idle", "record_id": 9}}, ""
        if path == extras.TRAVEL_PREFIX + "/depart":
            return 200, {"code": 0, "data": {}}, ""
        raise AssertionError(f"只执行旅行活动时不该请求：{method} {url}")

    monkeypatch.setattr(mod, "_call", fake)
    return seen


def test_run_with_keys_only_touches_requested_campaign(monkeypatch):
    seen = _travel_only_calls(monkeypatch)
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    body = service.run([_FakeAccount()], keys={"workbuddy.travel"})

    assert [item["key"] for item in body["results"]] == ["workbuddy.travel"]
    # 签到状态、成长任务、连签、抽奖一次都不该打。
    checkin_paths = (
        mod.WB_CHECKIN_STATUS_PATH,
        mod.WB_TASKS_PATH,
        mod.WB_STREAK_PATH,
        mod.WB_LOTTERY_PATH,
    )
    assert not any(path in checkin_paths for _, path in seen)
    service.close()


def test_run_keys_none_keeps_full_semantics(monkeypatch):
    """不传 keys 时行为与旧版一致：签到 + 额外活动都会跑。"""
    seen: list[str] = []

    def fake(client, method, url, headers, body=None):  # noqa: ANN001, ARG001
        seen.append(url)
        if url.endswith(mod.WB_CHECKIN_STATUS_PATH):
            return 200, {"code": 0, "data": {"active": True, "today_checked_in": True}}, ""
        if url.endswith(mod.WB_TASKS_PATH):
            return 200, {"code": 0, "data": {"tasks": []}}, ""
        if url.endswith(mod.WB_STREAK_PATH):
            return 200, {"code": 0, "data": {}}, ""
        if url.endswith(mod.WB_LOTTERY_PATH):
            return 200, {"code": 0, "data": {"balance": 0}}, ""
        # 其余额外活动统一按「接口未开放」处理。
        return 404, {"code": 404, "msg": "not found"}, ""

    monkeypatch.setattr(mod, "_call", fake)
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    service.run([_FakeAccount()])

    assert any(url.endswith(mod.WB_CHECKIN_STATUS_PATH) for url in seen)
    assert any(url.endswith(mod.WB_STREAK_PATH) for url in seen)
    service.close()


def test_run_unknown_account_platform_is_skipped_without_calls(monkeypatch):
    """keys 全是 trae.* 时，WorkBuddy 账号不该发任何请求。"""

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("平台不匹配时不该访问上游")

    monkeypatch.setattr(mod, "_call", boom)
    service = ActivitiesService(_FakeRepo(_wb_credential()))
    body = service.run([_FakeAccount()], keys={"trae.daily_checkin"})
    assert body["results"] == []
    service.close()


# ---------- 接口 ----------


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(home=tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def test_api_activities_catalog_includes_version_and_disclaimer(client):
    body = client.get("/api/activities/catalog").json()
    assert body["version"] == mod.OFFICIAL_CATALOG_VERSION
    assert body["updatedAt"] == mod.OFFICIAL_CATALOG_UPDATED_AT
    assert body["disclaimer"] == mod.OFFICIAL_DISCLAIMER
    assert len(body["entries"]) == 7


def test_api_activities_includes_campaigns(client, monkeypatch):
    monkeypatch.setattr(
        client.app.state.activities,
        "detect",
        lambda accounts, force=False: [
            _account(
                "a1",
                items=(_item(state=STATE_TODO, automatable=True),),
            )
        ],
    )
    body = client.get("/api/activities").json()
    assert body["campaigns"]
    assert {item["key"] for item in body["campaigns"]} == set(mod.ACTIVITY_KEYS)
    assert "detectedAt" in body
    assert body["summary"]["actionable"] == 1
    assert _campaign(body["campaigns"], "workbuddy.daily_checkin")["actionable"] == 1


def test_api_activities_run_rejects_bad_keys(client):
    empty = client.post("/api/activities/run", json={"keys": []})
    assert empty.status_code == 400

    unknown = client.post("/api/activities/run", json={"keys": ["workbuddy.unknown"]})
    assert unknown.status_code == 400
    assert "workbuddy.unknown" in unknown.json()["detail"]


def test_api_activities_run_passes_keys_to_service(client, monkeypatch):
    seen: list[set[str] | None] = []

    def fake_run(accounts, keys=None):  # noqa: ANN001
        seen.append(keys)
        return {"results": [], "summary": {"total": 0, "ok": 0, "skipped": 0, "failed": 0}}

    monkeypatch.setattr(client.app.state.activities, "run", fake_run)
    response = client.post(
        "/api/activities/run", json={"keys": ["workbuddy.daily_checkin"]}
    )
    assert response.status_code == 200
    assert seen == [{"workbuddy.daily_checkin"}]
