from __future__ import annotations

import json

from chaierww.infra.fsutil import (
    quarantine_corrupt,
    read_json_or_default,
    scan_corrupt_files,
    write_json_atomic,
    write_text_atomic,
)


def test_atomic_write_leaves_no_temp_files(tmp_path):
    target = tmp_path / "config.json"
    write_json_atomic(target, {"a": 1})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 1}
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_write_overwrites(tmp_path):
    target = tmp_path / "config.json"
    write_text_atomic(target, "one")
    write_text_atomic(target, "two")
    assert target.read_text(encoding="utf-8") == "two"


def test_corrupt_json_is_quarantined(tmp_path):
    target = tmp_path / "preferences.json"
    target.write_text("{not json", encoding="utf-8")
    value = read_json_or_default(target, {"default": True})
    assert value == {"default": True}
    assert not target.exists()
    corrupt = list(tmp_path.glob("preferences.json.corrupt-*"))
    assert len(corrupt) == 1
    # 原路径可以继续写，后续保存不会被历史残片影响。
    write_json_atomic(target, {"ok": True})
    assert read_json_or_default(target, None) == {"ok": True}
    assert scan_corrupt_files(tmp_path) == corrupt


def test_missing_file_returns_default(tmp_path):
    assert read_json_or_default(tmp_path / "missing.json", []) == []


def test_quarantine_failure_is_tolerated(tmp_path):
    target = tmp_path / "missing.json"
    assert quarantine_corrupt(target) is None
