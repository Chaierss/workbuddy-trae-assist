"""网页版登录态（按账号隔离的 Chromium 分区）的搬迁逻辑。

这些用例都不碰真实客户端目录：`partitions_dir` / `snapshot_root` 全部替换成 tmp_path。
"""

from __future__ import annotations

import json

import pytest

from chaierww.domain import webview_state

UID_A = "c1f0cc8d-3824-42dd-bf6a-3fbcb2dd91a6"
UID_B = "b27354e6-b015-4d85-bd86-3cf2e89c11ce"


@pytest.fixture
def parts(tmp_path, monkeypatch):
    """一个假的 Partitions 目录 + 假的快照目录。"""
    partitions = tmp_path / "Partitions"
    partitions.mkdir()
    snapshots = tmp_path / "webview"
    monkeypatch.setattr(webview_state, "partitions_dir", lambda: partitions)
    monkeypatch.setattr(webview_state, "snapshot_root", lambda: snapshots)
    return partitions, snapshots


def _make_partition(root, uid: str, purpose: str, *, cookie: str = "session-cookie") -> None:
    """造一个最小分区：登录态放在 Network/Cookies，另外塞一个缓存目录验证会被跳过。"""
    base = root / webview_state.partition_name(uid, purpose)
    (base / "Network").mkdir(parents=True, exist_ok=True)
    (base / "Network" / "Cookies").write_text(cookie, encoding="utf-8")
    local = base / "Local Storage" / "leveldb"
    local.mkdir(parents=True, exist_ok=True)
    (local / "000001.log").write_text("ls", encoding="utf-8")
    cache = base / "Cache"
    cache.mkdir(exist_ok=True)
    (cache / "big.bin").write_text("x" * 1024, encoding="utf-8")


def test_parse_partition_name_handles_uuid_and_rejects_others():
    assert webview_state.parse_partition_name(
        f"wb-webview-{UID_A}-lexiang-auth"
    ) == (UID_A, "lexiang-auth")
    # 不是本工具的命名，一律不碰
    assert webview_state.parse_partition_name("genie-webview") is None
    assert webview_state.parse_partition_name("wb-webview-too-short") is None
    # 构造与解析必须互逆，否则快照会铺到别的账号名下
    assert webview_state.partition_name(UID_A, "tdoc-auth") == f"wb-webview-{UID_A}-tdoc-auth"


def test_discover_reads_uid_and_purpose(parts):
    root, _ = parts
    _make_partition(root, UID_A, "lexiang-auth")
    _make_partition(root, UID_A, "tdoc-auth")
    _make_partition(root, UID_B, "channel-app")
    (root / "mcp-apps").mkdir()  # 与账号无关的分区应被忽略

    found = webview_state.discover()

    # discover 按 (uid, purpose) 排序，不按写入顺序
    assert [item.uid for item in found] == [UID_B, UID_A, UID_A]
    assert {item.purpose for item in found} == {"lexiang-auth", "tdoc-auth", "channel-app"}
    assert webview_state.uids_on_disk() == {UID_A, UID_B}


def test_snapshot_copies_credentials_but_skips_cache(parts):
    """搬的是登录态，不是缓存：缓存动辄几十 MB，且与登录无关。"""
    root, snapshots = parts
    _make_partition(root, UID_A, "lexiang-auth")

    saved = webview_state.snapshot(UID_A)

    assert saved == 1
    target = snapshots / UID_A / "lexiang-auth"
    assert (target / "Network" / "Cookies").read_text(encoding="utf-8") == "session-cookie"
    assert (target / "Local Storage" / "leveldb" / "000001.log").is_file()
    assert not (target / "Cache").exists()


def test_snapshot_is_overwrite_not_merge(parts):
    """第二次快照要整份重写，否则已登出的账号会在快照里残留登录态。"""
    root, snapshots = parts
    _make_partition(root, UID_A, "lexiang-auth", cookie="first")
    webview_state.snapshot(UID_A)

    stale = snapshots / UID_A / "lexiang-auth" / "Network" / "stale.txt"
    stale.write_text("left-over", encoding="utf-8")
    _make_partition(root, UID_A, "lexiang-auth", cookie="second")
    webview_state.snapshot(UID_A)

    target = snapshots / UID_A / "lexiang-auth"
    assert (target / "Network" / "Cookies").read_text(encoding="utf-8") == "second"
    assert not stale.exists()


def test_restore_puts_snapshot_back_under_same_partition_name(parts):
    root, _ = parts
    _make_partition(root, UID_A, "tdoc-auth")
    webview_state.snapshot(UID_A)
    # 模拟「切走之后客户端把分区删了」
    for item in webview_state.discover():
        if item.uid == UID_A:
            import shutil

            shutil.rmtree(item.path)
    assert UID_A not in webview_state.uids_on_disk()

    restored = webview_state.restore(UID_A)

    assert restored == 1
    back = root / webview_state.partition_name(UID_A, "tdoc-auth") / "Network" / "Cookies"
    assert back.read_text(encoding="utf-8") == "session-cookie"


def test_restore_returns_zero_when_no_snapshot(parts):
    assert webview_state.restore(UID_A) == 0
    assert webview_state.has_snapshot(UID_A) is False


def test_drop_only_removes_target_uid(parts):
    """清旧账号残留时不能顺手把别的号的分区一起删掉。"""
    root, _ = parts
    _make_partition(root, UID_A, "lexiang-auth")
    _make_partition(root, UID_B, "lexiang-auth")

    removed = webview_state.drop(UID_A)

    assert removed == 1
    assert webview_state.uids_on_disk() == {UID_B}


def test_snapshot_bytes_reports_size(parts):
    root, _ = parts
    _make_partition(root, UID_A, "lexiang-auth")
    webview_state.snapshot(UID_A)

    size = webview_state.snapshot_bytes(UID_A)

    assert size > 0
    # 缓存没被搬进去，所以快照不该包含那 1 KB
    assert size < 1024


def test_partition_bytes_counts_only_whitelisted(parts):
    root, _ = parts
    _make_partition(root, UID_A, "browser-preview")

    item = next(iter(webview_state.discover()))

    # Cookies("session-cookie" 13B) + leveldb log(2B)，Cache 的 1 KB 不计入
    assert webview_state.partition_bytes(item.path) == len("session-cookie") + 2


def test_real_uids_and_purposes_survive_json_roundtrip(parts):
    """uid 里有连字符，别在序列化/拼接时被切断。"""
    root, _ = parts
    _make_partition(root, UID_B, "tdoc-import")

    payload = json.dumps([item.name for item in webview_state.discover()])

    assert f"wb-webview-{UID_B}-tdoc-import" in payload
