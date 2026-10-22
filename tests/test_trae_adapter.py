from __future__ import annotations

import json

import pytest

from chaierww.adapters.base import NotLoggedIn, StateUnreadable
from chaierww.adapters.trae import (
    AUTH_PREFIX,
    DC_PREFIX,
    STORAGE_FILENAME,
    TraeAdapter,
    TraePaths,
    parse_identity,
)
from chaierww.domain.trae_crypto import tc_encrypt

UID_A = "1111222233334444"
UID_B = "9999999999999999"

# 设备 ID 固定不变（本机客户端），与账号 userId 解耦。
DEVICE_ID = "8888000011112222"


def _cloudide(uid: str) -> str:
    """造一个真实可解的 cloudide 信封，userId = uid。"""
    plain = {
        "token": "eyJhbGciOiJSUzI1NiJ9.header.payloadsig",
        "refreshToken": f"rt-{uid}",
        "expiredAt": "2026-11-02T01:08:09.789Z",
        "refreshExpiredAt": "2027-04-17T01:08:09.789Z",
        "tokenReleaseAt": "2026-10-19T01:08:09.582Z",
        "userId": uid,
        "host": "https://api.trae.cn",
        "userRegion": {"region": "CN", "_aiRegion": "CN"},
        "account": {"username": f"用户{uid[-8:]}", "scope": "marscode"},
    }
    return tc_encrypt(json.dumps(plain, ensure_ascii=False))


def storage(uid: str) -> bytes:
    """结构与真实 storage.json 一致：iCubeAuthInfo* 是 tc 信封（可解密）。"""
    data = {
        f"{DC_PREFIX}{DEVICE_ID}": tc_encrypt('{"privateKeyPEM":"pk","publicKeyPEM":"pub"}'),
        "iCubeAuthInfo://icube.cloudide": _cloudide(uid),
        "iCubeAuthInfo://usertag": tc_encrypt(json.dumps({uid: "cn"}, ensure_ascii=False)),
        # 用户 IDE 设置，切换时不应被触碰
        "theme": "dark",
        "windowsState": {"x": 0, "y": 0},
    }
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    state_dir = tmp_path / "globalStorage"
    state_dir.mkdir()
    paths = TraePaths(
        state_dir=state_dir,
        storage_file=state_dir / STORAGE_FILENAME,
        app_candidates=(),
    )
    instance = TraeAdapter(paths=paths)
    monkeypatch.setattr(instance, "is_running", lambda: False)
    return instance


def test_parse_identity_extracts_uid_from_cloudide():
    """账号标识应取自 cloudide 信封的 userId（真正账号），而非 icube-dc 的设备 ID。"""
    external_id, display_name, detail = parse_identity(storage(UID_A))
    assert external_id == UID_A
    # 展示名由 UID 后 8 位派生，跟着 UID_A 走，避免换测试值时再漏改。
    assert display_name == f"用户 {UID_A[-8:]}"
    assert detail["uid"] == UID_A


def test_parse_identity_rejects_not_logged_in():
    raw = json.dumps({"theme": "dark"}).encode("utf-8")
    with pytest.raises(NotLoggedIn):
        parse_identity(raw)


def test_parse_identity_rejects_broken_json():
    with pytest.raises(StateUnreadable):
        parse_identity(b"{not json")


def test_detect_reports_not_installed_when_nothing_exists(adapter):
    result = adapter.detect()
    assert result.installed is False
    assert result.logged_in is False


def test_import_keeps_only_auth_keys(adapter):
    adapter._paths.storage_file.write_bytes(storage(UID_A))

    imported = adapter.import_current()

    assert imported.external_id == UID_A
    # 凭据只含 iCubeAuthInfo key，不含 theme/windowsState 等 IDE 设置
    data = json.loads(imported.payload.decode("utf-8"))
    assert set(data) == {
        f"{DC_PREFIX}{DEVICE_ID}",
        "iCubeAuthInfo://icube.cloudide",
        "iCubeAuthInfo://usertag",
    }


def test_import_reports_not_logged_in(adapter):
    adapter._paths.storage_file.write_bytes(json.dumps({"theme": "dark"}).encode("utf-8"))
    with pytest.raises(NotLoggedIn):
        adapter.import_current()


def test_apply_switch_replaces_only_auth_keys(adapter):
    adapter._paths.storage_file.write_bytes(storage(UID_A))

    incoming = json.loads(storage(UID_B).decode("utf-8"))
    incoming = {k: v for k, v in incoming.items() if k.startswith(AUTH_PREFIX)}
    adapter.apply_switch(json.dumps(incoming).encode("utf-8"))

    result = json.loads(adapter._paths.storage_file.read_bytes().decode("utf-8"))
    # 账号换成了 B（cloudide 里的 userId）
    from chaierww.domain.trae_crypto import tc_decrypt

    assert json.loads(tc_decrypt(result["iCubeAuthInfo://icube.cloudide"]))["userId"] == UID_B
    # 用户 IDE 设置保留
    assert result["theme"] == "dark"
    assert result["windowsState"] == {"x": 0, "y": 0}
    # 不留临时文件
    leftovers = [p for p in adapter._paths.state_dir.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == []


def test_verify_switch_matches_identity(adapter):
    incoming = {k: v for k, v in json.loads(storage(UID_B).decode("utf-8")).items()
                if k.startswith(AUTH_PREFIX)}
    adapter.apply_switch(json.dumps(incoming).encode("utf-8"))
    assert adapter.verify_switch(UID_B) is True
    assert adapter.verify_switch(UID_A) is False


def test_verify_switch_returns_false_when_file_missing(adapter):
    assert adapter.verify_switch(UID_B) is False


def test_plan_switch_reports_storage_file(adapter):
    adapter._paths.storage_file.write_bytes(storage(UID_A))
    plan = adapter.plan_switch(storage(UID_B))
    actions = {item.path.name: item.action for item in plan.files}
    assert actions[STORAGE_FILENAME] == "override"


def test_restore_from_brings_back_previous_auth(adapter, tmp_path):
    adapter._paths.storage_file.write_bytes(storage(UID_A))
    backup = tmp_path / "snapshot"
    backup.mkdir()
    (backup / STORAGE_FILENAME).write_bytes(storage(UID_A))

    incoming = {k: v for k, v in json.loads(storage(UID_B).decode("utf-8")).items()
                if k.startswith(AUTH_PREFIX)}
    adapter.apply_switch(json.dumps(incoming).encode("utf-8"))
    assert adapter.verify_switch(UID_B)

    adapter.restore_from(backup)
    assert adapter.verify_switch(UID_A)


def test_plan_token_switch_is_supported(adapter):
    """Trae 明文 token 账号现可切换（tc_encrypt 反向构造信封）。"""
    adapter._paths.storage_file.write_bytes(storage(UID_A))
    plan = adapter.plan_token_switch({"uid": UID_B, "accessToken": "jwt-plain"})
    assert plan.files[0].action == "override"


def test_apply_token_switch_writes_cloudide_and_preserves_device(adapter):
    """token 切换应更新 cloudide 的 userId、保留 icube-dc 设备身份。"""
    from chaierww.domain.trae_crypto import tc_decrypt

    adapter._paths.storage_file.write_bytes(storage(UID_A))

    token = "eyJhbGciOiJSUzI1NiJ9." + "e30." + "sig"  # 无 exp/iat 的裸 JWT
    adapter.apply_token_switch(
        {"uid": UID_B, "accessToken": f"Cloud-IDE-JWT {token}", "refreshToken": "rt-B"}
    )

    result = json.loads(adapter._paths.storage_file.read_bytes().decode("utf-8"))
    # 设备身份 key 保留（deviceId 不变）
    assert f"{DC_PREFIX}{DEVICE_ID}" in result
    # cloudide 换成了目标账号
    cloudide = json.loads(tc_decrypt(result["iCubeAuthInfo://icube.cloudide"]))
    assert cloudide["userId"] == UID_B
    assert cloudide["token"] == token  # 前缀被去掉
    assert cloudide["refreshToken"] == "rt-B"
    # 用户 IDE 设置保留
    assert result["theme"] == "dark"
