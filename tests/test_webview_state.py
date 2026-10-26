"""网页版登录态快照：清单写入与代际校验。

快照本身只是「复制文件」，真正容易出错的是**判断该不该铺**：
uid 不符说明串号（必须拒绝），代际不符只说明 token 续过期（要铺 + 提示重登）。
"""

from __future__ import annotations

import pytest

import chaierww.domain.webview_state as wv

UID_A = "11111111-1111-1111-1111-111111111111"
UID_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    session = tmp_path / "session"
    (session / "Partitions").mkdir(parents=True)
    snapshots = tmp_path / "webview"
    monkeypatch.setattr(wv, "session_dir", lambda: session)
    monkeypatch.setattr(wv, "snapshot_root", lambda: snapshots)
    return {"session": session, "partitions": session / "Partitions", "snapshots": snapshots}


def _seed_partition(dirs, uid: str, purpose: str = "lexiang-auth") -> None:
    """造一个带登录态（白名单条目）的分区。"""
    folder = dirs["partitions"] / wv.partition_name(uid, purpose)
    (folder / "Network").mkdir(parents=True)
    (folder / "Network" / "Cookies").write_bytes(b"cookie-bytes")
    (folder / "Preferences").write_text("{}", encoding="utf-8")
    # 缓存条目不该被搬走。
    (folder / "Cache").mkdir()
    (folder / "Cache" / "junk").write_bytes(b"x" * 1024)


def test_snapshot_records_manifest_with_generation(dirs):
    _seed_partition(dirs, UID_A)
    assert wv.snapshot(UID_A, generation=3) == 1

    manifest = wv.read_manifest(UID_A)
    assert manifest is not None
    assert manifest["version"] == wv.MANIFEST_VERSION
    assert manifest["uid"] == UID_A
    assert manifest["credentialGeneration"] == 3
    assert manifest["purposes"] == ["lexiang-auth"]
    assert manifest["entries"] == 1
    # 缓存不进快照：清单里的字节数只算白名单条目。
    assert manifest["bytes"] < 1024


def _clear_partitions(dirs) -> None:
    """模拟「客户端分区被清空、只剩快照」。"""
    import shutil

    shutil.rmtree(dirs["partitions"])
    dirs["partitions"].mkdir()


def test_restore_checked_accepts_same_generation(dirs):
    _seed_partition(dirs, UID_A)
    wv.snapshot(UID_A, generation=1)
    _clear_partitions(dirs)

    result = wv.restore_checked(UID_A, generation=1)
    assert result.partitions == 1
    assert result.reason is None
    assert result.warning is False
    assert (dirs["partitions"] / wv.partition_name(UID_A, "lexiang-auth") / "Network").is_dir()


def test_restore_checked_refuses_mismatched_uid(dirs):
    _seed_partition(dirs, UID_A)
    wv.snapshot(UID_A, generation=1)
    _clear_partitions(dirs)
    # 把清单改成「属于另一个账号」——这是真的串号风险，必须拒绝。
    wv._write_manifest(dirs["snapshots"] / UID_A, {"uid": UID_B, "version": 1})

    result = wv.restore_checked(UID_A, generation=1)
    assert result.error is True
    assert result.partitions == 0
    assert "另一个账号" in (result.reason or "")
    assert not (dirs["partitions"] / wv.partition_name(UID_A, "lexiang-auth")).exists()


def test_restore_checked_warns_but_restores_on_generation_drift(dirs):
    _seed_partition(dirs, UID_A)
    wv.snapshot(UID_A, generation=1)

    result = wv.restore_checked(UID_A, generation=2)
    assert result.partitions == 1
    assert result.warning is True
    assert "不同代" in (result.reason or "")


def test_restore_checked_tolerates_legacy_snapshot_without_manifest(dirs):
    _seed_partition(dirs, UID_A)
    wv.snapshot(UID_A, generation=0)
    (dirs["snapshots"] / UID_A / wv.MANIFEST_NAME).unlink()

    result = wv.restore_checked(UID_A, generation=5)
    assert result.partitions == 1
    assert result.warning is True
    assert "旧版本" in (result.reason or "")


def test_restore_checked_without_snapshot_is_noop(dirs):
    result = wv.restore_checked(UID_A, generation=1)
    assert result.partitions == 0
    assert result.reason is None
    assert result.warning is False
    assert result.error is False


def test_unsafe_uid_never_touches_disk(dirs):
    assert wv.snapshot("..", generation=1) == 0
    assert wv.read_manifest("..") is None
    assert wv.has_snapshot("..") is False
    assert wv.restore_checked("..", generation=1).partitions == 0
    assert wv.drop_snapshot("..") is False
