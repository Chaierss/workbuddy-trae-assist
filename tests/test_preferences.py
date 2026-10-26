"""偏好文件：0.8.0 新增的引导状态与本地体验数据开关。"""

from __future__ import annotations

import json

from chaierww.domain import preferences as mod
from chaierww.domain.preferences import PreferencesStore


def test_defaults_include_onboarding_and_metrics_flags(tmp_path):
    payload = PreferencesStore(tmp_path).load().payload()
    assert payload["onboardingVersion"] == 0
    assert payload["onboardingCompleted"] == []
    assert payload["onboardingDismissedAt"] is None
    assert payload["firstSwitchAt"] is None
    assert payload["localMetricsEnabled"] is True


def test_legacy_file_without_new_keys_uses_defaults(tmp_path):
    """老版本写的 preferences.json 不该因为缺字段而报错或变成关闭体验数据。"""
    (tmp_path / mod.FILENAME).write_text(
        json.dumps({"migrateMemory": False, "autoGlobalParts": ["config"]}),
        encoding="utf-8",
    )
    prefs = PreferencesStore(tmp_path).load()
    assert prefs.migrate_memory is False
    assert prefs.onboarding_version == 0
    assert prefs.first_switch_at is None
    assert prefs.local_metrics_enabled is True


def test_onboarding_steps_are_whitelisted_and_deduped(tmp_path):
    (tmp_path / mod.FILENAME).write_text(
        json.dumps({"onboardingCompleted": ["install", "install", "bogus", "signin"]}),
        encoding="utf-8",
    )
    assert PreferencesStore(tmp_path).load().onboarding_completed == (
        "install",
        "signin",
    )


def test_onboarding_version_rejects_negative_and_bool(tmp_path):
    assert mod.clean_version(-1) == 0
    assert mod.clean_version(True) == 0
    assert mod.clean_version(2) == 2


def test_mark_first_switch_writes_once(tmp_path):
    store = PreferencesStore(tmp_path)
    assert store.mark_first_switch("2026-10-01T10:00:00+08:00") is True
    assert store.load().first_switch_at == "2026-10-01T10:00:00+08:00"
    # 第二次调用不覆盖第一个时间点 —— 它代表「首次」，不是「最近一次」。
    assert store.mark_first_switch("2026-10-02T10:00:00+08:00") is False
    assert store.load().first_switch_at == "2026-10-01T10:00:00+08:00"


def test_load_drops_invalid_first_switch_value(tmp_path):
    (tmp_path / mod.FILENAME).write_text(
        json.dumps({"firstSwitchAt": 12345, "onboardingDismissedAt": ""}),
        encoding="utf-8",
    )
    prefs = PreferencesStore(tmp_path).load()
    assert prefs.first_switch_at is None
    assert prefs.onboarding_dismissed_at is None
