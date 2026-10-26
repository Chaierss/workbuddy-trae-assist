"""本地体验数据：双白名单、轮转、漏斗口径与清空。"""

from __future__ import annotations

import json

from chaierww.domain import metrics as mod


def test_unknown_event_name_is_rejected(tmp_path):
    store = mod.MetricsStore(tmp_path)
    assert store.record("login_email", {"step": "signin"}) is False
    assert store.export_payload()["events"] == []


def test_properties_are_whitelisted(tmp_path):
    store = mod.MetricsStore(tmp_path)
    store.record(
        "account_imported",
        {
            "platform": "workbuddy",
            "method": "scan",
            "email": "a@b.com",  # 不在白名单
            "note": "x" * 100,  # 超长
            "count": 3,
            "first": True,
        },
    )
    props = store.export_payload()["events"][0]["props"]
    # count 不在白名单里，同样丢弃 —— 属性集合是封闭的。
    assert props == {"platform": "workbuddy", "method": "scan", "first": True}


def test_funnel_counts_and_activation(tmp_path):
    store = mod.MetricsStore(tmp_path)
    store.record("app_open", {"first": True})
    store.record("client_detected", {"platform": "workbuddy"})
    store.record("switch_completed", {"platform": "workbuddy"})

    funnel = store.funnel(14)
    assert funnel["counts"]["app_open"] == 1
    assert funnel["counts"]["switch_completed"] == 1
    assert funnel["activated"] is True
    assert funnel["firstSwitchSeconds"] is not None
    assert funnel["ttfvMedianSeconds"] is not None


def test_funnel_without_switch_is_not_activated(tmp_path):
    store = mod.MetricsStore(tmp_path)
    store.record("app_open", {"first": True})
    funnel = store.funnel(14)
    assert funnel["activated"] is None
    assert funnel["firstSwitchAt"] is None


def test_old_events_fall_outside_window(tmp_path):
    store = mod.MetricsStore(tmp_path)
    store._dir.mkdir(parents=True, exist_ok=True)
    store._file.write_text(
        json.dumps(
            {"ts": "2020-01-01T00:00:00+08:00", "name": "app_open", "props": {}}
        )
        + "\n",
        encoding="utf-8",
    )
    funnel = store.funnel(3)
    assert funnel["counts"]["app_open"] == 0


def test_corrupt_lines_are_skipped(tmp_path):
    store = mod.MetricsStore(tmp_path)
    store._dir.mkdir(parents=True, exist_ok=True)
    store._file.write_text(
        "not json\n"
        + json.dumps({"ts": "2026-10-01T00:00:00+08:00", "name": "app_open", "props": {}})
        + "\n",
        encoding="utf-8",
    )
    assert store.funnel(90)["counts"]["app_open"] == 1


def test_file_rotates_and_keeps_two_backups(tmp_path, monkeypatch):
    monkeypatch.setattr(mod, "MAX_BYTES", 1)
    store = mod.MetricsStore(tmp_path)
    for _ in range(4):
        assert store.record("app_open", {}) is True
    assert (tmp_path / mod.DIRNAME / "events.1.ndjson").exists()
    assert (tmp_path / mod.DIRNAME / "events.2.ndjson").exists()
    # 当前文件 + 两份历史 = 保留最近 3 条，更早的被丢弃。
    assert store.funnel(14)["counts"]["app_open"] == 3


def test_clear_removes_events(tmp_path):
    store = mod.MetricsStore(tmp_path)
    store.record("app_open", {})
    assert store.clear() >= 1
    assert store.funnel(14)["counts"]["app_open"] == 0


def test_export_payload_is_self_describing(tmp_path):
    store = mod.MetricsStore(tmp_path)
    store.record("onboarding_step", {"step": "install", "status": "done"})
    payload = store.export_payload()
    assert payload["format"] == "chaierww.metrics"
    assert payload["version"] == 1
    assert payload["events"][0]["name"] == "onboarding_step"
