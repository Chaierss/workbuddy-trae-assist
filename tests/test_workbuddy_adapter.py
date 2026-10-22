from __future__ import annotations

import json
import time

import pytest

from chaierww.adapters.base import NotLoggedIn, StateUnreadable
from chaierww.adapters.workbuddy import (
    STATE_FILENAME,
    WorkBuddyAdapter,
    WorkBuddyPaths,
    parse_identity,
)

UID_A = "c1f0cc8d-3824-42dd-bf6a-3fbcb2dd91a6"


def payload(uid: str, uin: str = "330128657221") -> bytes:
    """结构与真实登录态文件一致：加密字段用占位信封，工具不解密。"""
    return json.dumps(
        {
            "account": {
                "uid": uid,
                "uin": uin,
                "type": "personal",
                "nickname": {"$wbEncrypted": 1, "envelope": "x" * 180},
            },
            "accounts": [{"uid": uid, "uin": uin}],
            "allAccounts": [{"uid": uid, "uin": uin}],
            "auth": {
                "accessToken": {"$wbEncrypted": 1, "envelope": "y" * 2580},
                "refreshToken": {"$wbEncrypted": 1, "envelope": "z" * 1408},
                "expiresAt": 1794801730673,
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    state_dir = tmp_path / "auth"
    state_dir.mkdir()
    paths = WorkBuddyPaths(
        state_dir=state_dir,
        state_file=state_dir / STATE_FILENAME,
        app_candidates=(),
    )
    instance = WorkBuddyAdapter(paths=paths)
    # 测试机上的 WorkBuddy 可能正在运行，写入保护会正确拒绝。
    # 这里只验证文件读写逻辑本身，因此固定为"未运行"。
    monkeypatch.setattr(instance, "is_running", lambda: False)
    return instance


def test_parse_identity_uses_uid(adapter):
    external_id, display_name, detail = parse_identity(payload(UID_A))
    assert external_id == UID_A
    assert display_name == "用户 330128657221"
    assert detail["uin"] == "330128657221"


def test_parse_identity_rejects_empty_account():
    raw = json.dumps({"account": {}, "auth": {}}).encode("utf-8")
    with pytest.raises(NotLoggedIn):
        parse_identity(raw)


def test_parse_identity_rejects_broken_json():
    with pytest.raises(StateUnreadable):
        parse_identity(b"{not json")


def test_parse_identity_handles_bom():
    raw = b"\xef\xbb\xbf" + payload(UID_A)
    external_id, _, _ = parse_identity(raw)
    assert external_id == UID_A


def test_detect_reports_not_installed_when_nothing_exists(adapter):
    result = adapter.detect()
    assert result.installed is False
    assert result.logged_in is False


def test_import_returns_file_bytes_untouched(adapter):
    raw = payload(UID_A)
    adapter._paths.state_file.write_bytes(raw)

    imported = adapter.import_current()

    assert imported.payload == raw
    assert imported.external_id == UID_A


def test_import_reports_not_logged_in(adapter):
    adapter._paths.state_file.write_bytes(json.dumps({"account": {}}).encode("utf-8"))
    with pytest.raises(NotLoggedIn):
        adapter.import_current()


def test_apply_switch_writes_atomically(adapter):
    target = payload("uid-B")
    adapter.apply_switch(target)

    assert adapter._paths.state_file.read_bytes() == target
    leftovers = [p for p in adapter._paths.state_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_verify_switch_matches_identity(adapter):
    adapter.apply_switch(payload("uid-B"))
    assert adapter.verify_switch("uid-B") is True
    assert adapter.verify_switch("uid-A") is False


def test_verify_switch_returns_false_when_file_missing(adapter):
    assert adapter.verify_switch("uid-B") is False


def test_plan_switch_marks_history_files_as_keep(adapter):
    adapter._paths.state_file.write_bytes(payload(UID_A))
    (adapter._paths.state_dir / "workbuddy-desktop.2026-09-16T11-20-29-790Z.info").write_bytes(
        payload("uid-old")
    )

    plan = adapter.plan_switch(payload("uid-B"))

    actions = {item.path.name: item.action for item in plan.files}
    assert actions[STATE_FILENAME] == "override"
    assert actions["workbuddy-desktop.2026-09-16T11-20-29-790Z.info"] == "keep"


def test_plan_switch_warns_when_file_new(adapter):
    plan = adapter.plan_switch(payload("uid-B"))
    actions = {item.path.name: item.action for item in plan.files}
    assert actions[STATE_FILENAME] == "create"


def test_restore_from_brings_back_previous_files(adapter, tmp_path):
    adapter._paths.state_file.write_bytes(payload("uid-A"))
    backup = tmp_path / "snapshot"
    backup.mkdir()
    (backup / STATE_FILENAME).write_bytes(payload("uid-A"))

    adapter.apply_switch(payload("uid-B"))
    assert adapter.verify_switch("uid-B")

    adapter.restore_from(backup)
    assert adapter.verify_switch("uid-A")


def test_restore_removes_files_not_in_snapshot(adapter, tmp_path):
    adapter._paths.state_file.write_bytes(payload("uid-A"))
    backup = tmp_path / "snap"
    backup.mkdir()
    (backup / STATE_FILENAME).write_bytes(payload("uid-A"))

    adapter.apply_switch(payload("uid-B"))
    stray = adapter._paths.state_dir / "stray.info"
    stray.write_bytes(b"{}")

    adapter.restore_from(backup)

    assert adapter.verify_switch("uid-A")
    assert not stray.exists()


def test_apply_token_switch_writes_plain_token_to_auth(adapter):
    account = {
        "uid": "token-uid-1",
        "nickname": "扫码用户",
        "accessToken": "plain-access-token",
        "refreshToken": "plain-refresh-token",
        "tokenType": "Bearer",
        "domain": "www.workbuddy.cn",
        "expiresAt": None,
        "auth_raw": {},
        "profile_raw": {"email": "a@example.com"},
    }
    adapter.apply_token_switch(account)

    raw = adapter._paths.state_file.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    assert data["auth"]["accessToken"] == "plain-access-token"
    assert data["auth"]["refreshToken"] == "plain-refresh-token"
    assert data["account"]["uid"] == "token-uid-1"
    assert data["account"]["nickname"] == "扫码用户"
    # allAccounts 里包含目标账号
    assert any(a.get("uid") == "token-uid-1" for a in data["allAccounts"])
    # 没有 expiresAt 时**不能**写 expiresIn=0：那等于告诉客户端「这个 token 已过期」，
    # 客户端读到会立刻要求重新登录（导入号池后「还要登录」就是踩的这个）。
    assert "expiresIn" not in data["auth"]
    assert "refreshExpiresIn" not in data["auth"]


def test_apply_token_switch_parses_expiry_from_milliseconds(adapter):
    """上游给毫秒时间戳时，要算出真实剩余有效期而不是写 0。"""
    future = int(time.time() * 1000) + 3600 * 1000
    adapter.apply_token_switch(
        {
            "uid": "uid-ms",
            "nickname": "毫秒号",
            "accessToken": "tok",
            "refreshToken": "rt",
            "expiresAt": future,
            "refreshExpiresAt": future + 86400 * 1000,
        }
    )

    auth = json.loads(adapter._paths.state_file.read_bytes().decode("utf-8"))["auth"]
    assert auth["expiresAt"] == future
    assert 3500 <= auth["expiresIn"] <= 3600
    assert auth["refreshExpiresAt"] == future + 86400 * 1000
    assert auth["refreshExpiresIn"] > 86000


def test_apply_token_switch_parses_expiry_from_iso_string(adapter):
    """有的来源给秒、有的给 ISO 字符串；统一成毫秒，别因为类型不同就丢掉过期时间。"""
    adapter.apply_token_switch(
        {
            "uid": "uid-iso",
            "accessToken": "tok",
            "expiresAt": "2099-01-01T00:00:00+00:00",
        }
    )

    auth = json.loads(adapter._paths.state_file.read_bytes().decode("utf-8"))["auth"]
    assert auth["expiresAt"] == 4070908800000
    assert auth["expiresIn"] > 0


def test_apply_token_switch_rejects_missing_uid(adapter):
    from chaierww.adapters.base import StateUnreadable

    with pytest.raises(StateUnreadable):
        adapter.apply_token_switch({"accessToken": "tok"})


def test_apply_token_switch_preserves_existing_accounts(adapter):
    # 预置一个既有登录态（含另一个账号）
    existing = {
        "account": {"uid": "existing-uid", "nickname": "旧账号"},
        "auth": {"accessToken": "old-token"},
        "accounts": [{"uid": "existing-uid", "nickname": "旧账号"}],
        "allAccounts": [{"uid": "existing-uid", "nickname": "旧账号"}],
    }
    adapter._paths.state_file.write_bytes(json.dumps(existing).encode("utf-8"))

    account = {
        "uid": "new-uid",
        "nickname": "新账号",
        "accessToken": "new-token",
        "refreshToken": "new-refresh",
        "auth_raw": {},
        "profile_raw": {},
    }
    adapter.apply_token_switch(account)

    data = json.loads(adapter._paths.state_file.read_bytes().decode("utf-8"))
    uids = [a.get("uid") for a in data["allAccounts"]]
    assert "existing-uid" in uids
    assert "new-uid" in uids
    # 新账号成为当前 account
    assert data["account"]["uid"] == "new-uid"


def test_detect_treats_locked_file_as_logged_in(adapter, monkeypatch):
    """文件存在但读不到（客户端运行中独占锁）→ 应判「已登录」，不是「未登录」。"""
    adapter._paths.state_file.write_bytes(payload(UID_A))

    def _raise_oserror(_raw: bytes):
        raise OSError("Permission denied")

    # 模拟文件被客户端独占锁：read_bytes 抛 OSError。
    monkeypatch.setattr("chaierww.adapters.workbuddy.parse_identity", _raise_oserror)
    # is_running 已被 fixture 固定为 False，这里覆盖成 True（模拟客户端运行中）。
    monkeypatch.setattr(adapter, "is_running", lambda: True)

    result = adapter.detect()
    assert result.installed is True
    assert result.logged_in is True


