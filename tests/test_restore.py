from __future__ import annotations

import hashlib
import json

from chaierww.domain.restore import (
    PENDING_NAME,
    RESULT_NAME,
    apply_pending_restore,
    cancel_pending,
    read_pending,
    stage_restore,
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _snapshot(home, name="snap-1", payload=b"from-backup", scope="global", break_hash=False):
    directory = home / "backups" / "global" / name
    directory.mkdir(parents=True)
    (directory / "preferences.json").write_bytes(payload)
    manifest = {
        "scope": scope,
        "files": [
            {
                "name": "preferences.json",
                "sha256": _digest(payload) if not break_hash else "0" * 64,
                "bytes": len(payload),
            }
        ],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return directory


def test_stage_then_apply_restores_files(tmp_path):
    (tmp_path / "preferences.json").write_bytes(b"current")
    _snapshot(tmp_path)

    pending = stage_restore(tmp_path, "snap-1")
    assert pending["snapshotId"] == "snap-1"
    assert (tmp_path / PENDING_NAME).is_file()
    # 登记阶段不能改动任何业务文件。
    assert (tmp_path / "preferences.json").read_bytes() == b"current"

    result = apply_pending_restore(tmp_path)
    assert result is not None and result["ok"] is True
    assert (tmp_path / "preferences.json").read_bytes() == b"from-backup"
    assert not (tmp_path / PENDING_NAME).exists()
    assert json.loads((tmp_path / RESULT_NAME).read_text(encoding="utf-8"))["ok"] is True

    safety = list((tmp_path / "backups" / "restore-safety").iterdir())
    assert safety and (safety[0] / "preferences.json").read_bytes() == b"current"


def test_hash_mismatch_keeps_original_and_reports_failure(tmp_path):
    (tmp_path / "preferences.json").write_bytes(b"current")
    directory = _snapshot(tmp_path)
    stage_restore(tmp_path, "snap-1")
    # 登记之后备份被篡改：启动时必须拒绝覆盖并保留当前数据。
    (directory / "preferences.json").write_bytes(b"tampered")

    result = apply_pending_restore(tmp_path)
    assert result is not None and result["ok"] is False
    assert "校验失败" in result["error"]
    assert (tmp_path / "preferences.json").read_bytes() == b"current"


def test_non_global_snapshot_is_rejected(tmp_path):
    _snapshot(tmp_path, scope="workbuddy-cn")
    try:
        stage_restore(tmp_path, "snap-1")
    except Exception as exc:  # noqa: BLE001 - 测试只关心拒绝
        assert "全局备份" in str(exc)
    else:  # pragma: no cover - 失败信息更直观
        raise AssertionError("非全局备份不应允许原地恢复")


def test_cancel_pending(tmp_path):
    _snapshot(tmp_path)
    stage_restore(tmp_path, "snap-1")
    assert read_pending(tmp_path) is not None
    cancel_pending(tmp_path)
    assert read_pending(tmp_path) is None
    assert apply_pending_restore(tmp_path) is None
