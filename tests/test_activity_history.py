from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from chaierww.config import Settings
from chaierww.domain.activities import (
    STATE_DONE,
    STATE_TODO,
    AccountActivities,
    ActivityItem,
    summarize_campaigns,
)
from chaierww.main import create_app


def _account(items, *, platform="workbuddy", scope="workbuddy-cn", detectable=True):
    return AccountActivities(
        account_id="acc-1",
        display_name="账号甲",
        platform=platform,
        scope=scope,
        detectable=detectable,
        items=tuple(items),
    )


def test_unknown_activity_becomes_fallback_card():
    campaigns = summarize_campaigns(
        [
            _account(
                [
                    ActivityItem(
                        key="workbuddy.new_thing",
                        platform="workbuddy",
                        group="daily",
                        title="新上线活动",
                        state=STATE_TODO,
                        automatable=True,
                    )
                ]
            )
        ]
    )
    fallback = [item for item in campaigns if item["status"] == "uncatalogued"]
    assert len(fallback) == 1
    assert fallback[0]["key"] == "workbuddy.new_thing"
    assert fallback[0]["title"] == "新上线活动"
    assert fallback[0]["todo"] == 1
    assert fallback[0]["accountsTotal"] == 1
    # 未收录活动没有官方入口，不能伪造。
    assert fallback[0]["officialUrl"] == ""


def test_uncatalogued_stays_last():
    campaigns = summarize_campaigns(
        [
            _account(
                [
                    ActivityItem(
                        key="workbuddy.new_thing",
                        platform="workbuddy",
                        group="daily",
                        title="新上线活动",
                        state=STATE_DONE,
                    )
                ]
            )
        ]
    )
    assert campaigns[-1]["status"] == "uncatalogued"


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(home=tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def test_history_endpoint_aggregates_by_day(client):
    repository = client.app.state.repository
    first = repository.upsert(
        scope="workbuddy-cn",
        external_id="ext-1",
        display_name="账号甲",
        credential={"t": 1},
    ).account.id
    second = repository.upsert(
        scope="workbuddy-cn",
        external_id="ext-2",
        display_name="账号乙",
        credential={"t": 2},
    ).account.id
    today = datetime.now(UTC).date()
    yesterday = today - timedelta(days=1)
    repository.record_activity_runs(
        [
            {
                "accountId": first,
                "key": "workbuddy.daily_checkin",
                "day": today.isoformat(),
                "status": "ok",
                "reward": 3,
            },
            {
                "accountId": second,
                "key": "workbuddy.daily_checkin",
                "day": today.isoformat(),
                "status": "failed",
            },
            {
                "accountId": first,
                "key": "workbuddy.daily_checkin",
                "day": yesterday.isoformat(),
                "status": "skipped",
            },
        ]
    )

    body = client.get(
        "/api/activities/history?days=14&key=workbuddy.daily_checkin"
    ).json()
    assert body["days"] == 14
    series = body["series"]
    assert len(series) == 1
    assert series[0]["key"] == "workbuddy.daily_checkin"
    today_row = next(item for item in series[0]["days"] if item["day"] == today.isoformat())
    assert today_row == {
        "day": today.isoformat(),
        "ok": 1,
        "skipped": 0,
        "failed": 1,
        "reward": 3.0,
    }
    assert series[0]["ok"] == 1
    assert series[0]["skipped"] == 1
    assert series[0]["failed"] == 1


def test_history_rejects_unknown_key(client):
    response = client.get("/api/activities/history?key=no.such.activity")
    assert response.status_code == 400


def test_history_all_catalog_keys_without_filter(client):
    body = client.get("/api/activities/history?days=7").json()
    keys = {item["key"] for item in body["series"]}
    assert "workbuddy.daily_checkin" in keys
    assert "trae.daily_checkin" in keys
