"""WorkBuddy 客户端信封（``$wbEncrypted``）解密与续期回写单元测试。

信封是 5.9.2 起客户端引入的：``auth.accessToken`` / ``auth.refreshToken`` 不再是明文，
而是 AES-256-GCM 密文，密钥由客户端 ``loggerGet`` 给的 ``atRestSecretKey`` 派生。
这里用测试密钥替身跑完整链路，不依赖本机是否装了 WorkBuddy。
"""

from __future__ import annotations

import base64
import hashlib
import json

import httpx
import pytest

from chaierww.domain import wb_crypto
from chaierww.domain.credential_lifecycle import refreshable, token_expiry
from chaierww.domain.credential_refresh import (
    STATUS_REFRESHED,
    STATUS_UNSUPPORTED,
    WorkBuddyRefresher,
)
from chaierww.domain.platform_api import CredentialError, workbuddy_credentials
from chaierww.domain.wb_crypto import (
    WbCryptoError,
    decrypt_workbuddy_auth,
    decrypt_workbuddy_credential,
    reencrypt_workbuddy_auth,
)

KEY = hashlib.sha256(b"test-at-rest-secret").digest()
KEY_ID = wb_crypto._protector_key_id(KEY)
EXPIRES_MS = 1_800_000_000_000


@pytest.fixture(autouse=True)
def _fake_protector_key(monkeypatch):
    """不启动 WorkBuddy 子进程，直接给一把测试密钥。"""
    monkeypatch.setattr(wb_crypto, "get_protector_key", lambda app_path=None: KEY)


def _field(value: str) -> dict:
    envelope = wb_crypto._encrypt_envelope(value.encode("utf-8"), KEY, KEY_ID, framing="field")
    return wb_crypto._wrap_envelope(envelope)


def _info(
    *, access: str = "acc-old", refresh: str = "ref-old", expires: int = EXPIRES_MS
) -> bytes:
    data = {
        "account": {"uid": "u-1", "type": "personal"},
        "auth": {
            "domain": "www.workbuddy.cn",
            "accessToken": _field(access),
            "refreshToken": _field(refresh),
            "expiresAt": expires,
        },
    }
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def _credential(raw: bytes) -> dict:
    return {
        "format": "raw-file",
        "encoding": "base64",
        "data": base64.b64encode(raw).decode("ascii"),
    }


def test_decrypt_auth_reads_encrypted_tokens():
    auth = decrypt_workbuddy_auth(_info())

    assert auth["accessToken"] == "acc-old"
    assert auth["refreshToken"] == "ref-old"
    assert auth["uid"] == "u-1"
    assert auth["domain"] == "www.workbuddy.cn"
    assert auth["expiresAt"] == EXPIRES_MS


def test_decrypt_auth_accepts_plaintext_legacy_file():
    """旧版客户端写明文 token，同一个函数要照收，不能只认信封。"""
    raw = json.dumps(
        {
            "account": {"uid": "u-legacy"},
            "auth": {"accessToken": "plain-acc", "refreshToken": "plain-ref"},
        }
    ).encode("utf-8")

    auth = decrypt_workbuddy_auth(raw)

    assert auth["accessToken"] == "plain-acc"
    assert auth["refreshToken"] == "plain-ref"
    assert auth["uid"] == "u-legacy"


def test_plaintext_file_does_not_need_client(monkeypatch):
    """纯明文登录态不该去碰客户端：没有 WorkBuddy 的机器也要能读旧文件。"""
    raw = json.dumps(
        {"account": {"uid": "u-legacy"}, "auth": {"accessToken": "plain-acc"}}
    ).encode("utf-8")

    def boom(app_path=None):
        raise AssertionError("明文登录态不应调用 get_protector_key")

    monkeypatch.setattr(wb_crypto, "get_protector_key", boom)

    assert decrypt_workbuddy_auth(raw)["accessToken"] == "plain-acc"


def test_decrypt_auth_rejects_foreign_envelope():
    """换机器 / 客户端重装后密钥变了，必须明确报错而不是给出空 token。"""
    other = hashlib.sha256(b"another-machine").digest()
    raw = json.dumps(
        {
            "account": {"uid": "u-1"},
            "auth": {
                "accessToken": wb_crypto._wrap_envelope(
                    wb_crypto._encrypt_envelope(b"acc", other, KEY_ID, framing="field")
                )
            },
        }
    ).encode("utf-8")

    with pytest.raises(WbCryptoError) as exc:
        decrypt_workbuddy_auth(raw)
    assert "accessToken" in str(exc.value)


def test_decrypt_credential_returns_oauth_shape():
    decrypted = decrypt_workbuddy_credential(_credential(_info()))

    assert decrypted["format"] == "oauth-token"
    assert decrypted["accessToken"] == "acc-old"
    assert decrypted["refreshToken"] == "ref-old"
    assert decrypted["uid"] == "u-1"


def test_decrypt_credential_rejects_broken_envelope():
    credential = {"format": "raw-file", "encoding": "base64", "data": "not-base64!"}

    with pytest.raises(WbCryptoError):
        decrypt_workbuddy_credential(credential)


def test_reencrypt_roundtrip_keeps_unrelated_fields():
    """回写只能动 token 与过期时间，昵称、uid、其它段一律原样。"""
    new_raw = reencrypt_workbuddy_auth(
        _info(), access_token="acc-new", refresh_token="ref-new", expires_at=1_900_000_000_000
    )

    data = json.loads(new_raw.decode("utf-8"))
    assert data["account"] == {"uid": "u-1", "type": "personal"}
    assert data["auth"]["expiresAt"] == 1_900_000_000_000
    auth = decrypt_workbuddy_auth(new_raw)
    assert auth["accessToken"] == "acc-new"
    assert auth["refreshToken"] == "ref-new"


def test_reencrypt_without_auth_section_raises():
    raw = json.dumps({"account": {"uid": "u-1"}}).encode("utf-8")

    with pytest.raises(WbCryptoError):
        reencrypt_workbuddy_auth(raw, access_token="acc")


def test_workbuddy_credentials_decrypts_envelope():
    token, uid, domain = workbuddy_credentials(_credential(_info()))

    assert token == "acc-old"
    assert uid == "u-1"
    assert domain == "www.workbuddy.cn"


def test_workbuddy_credentials_reports_unreadable_envelope():
    broken = {"auth": {"accessToken": {"$wbEncrypted": 1, "envelope": "xx"}}}
    credential = {
        "format": "raw-file",
        "encoding": "base64",
        "data": base64.b64encode(json.dumps(broken).encode()).decode(),
    }

    with pytest.raises(CredentialError) as exc:
        workbuddy_credentials(credential)
    assert "accessToken" in str(exc.value)


def test_lifecycle_reads_expiry_and_refreshability_from_envelope():
    """信封里既有 refreshToken 也有 expiresAt，号池才能说「可续期、什么时候到期」。"""
    credential = _credential(_info())

    assert refreshable("session", credential) is True
    expiry = token_expiry("session", credential)
    assert expiry is not None
    assert int(expiry.timestamp() * 1000) == EXPIRES_MS


class _FakeClient:
    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.calls: list[dict] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):  # noqa: A002
        self.calls.append({"url": url, "headers": headers, "json": json})
        return self.response


def test_refresh_raw_file_reencrypts_and_keeps_format(monkeypatch):
    """续期成功后必须写回 ``raw-file`` 信封：切号链路依赖这个形态。"""
    response = httpx.Response(
        200,
        json={
            "data": {
                "data": {
                    "accessToken": "acc-new",
                    "refreshToken": "ref-new",
                    "expiresIn": 3600,
                }
            }
        },
    )
    client = _FakeClient(response)
    monkeypatch.setattr(
        "chaierww.domain.credential_refresh.httpx.Client", lambda **kwargs: client
    )

    outcome = WorkBuddyRefresher().refresh(_credential(_info()))

    assert outcome.status == STATUS_REFRESHED
    assert outcome.credential is not None
    assert outcome.credential["format"] == "raw-file"
    after = decrypt_workbuddy_credential(outcome.credential)
    assert after["accessToken"] == "acc-new"
    assert after["refreshToken"] == "ref-new"
    assert after["uid"] == "u-1"

    call = client.calls[0]
    assert call["url"].endswith("/v2/plugin/auth/token/refresh")
    assert call["headers"]["X-Refresh-Token"] == "ref-old"
    assert call["headers"]["X-Auth-Refresh-Source"] == "plugin"
    assert call["json"] == {}


def test_refresh_raw_file_without_key_is_unsupported(monkeypatch):
    """本机没有可用密钥时，诚实报「无法解密」，不能假装续期成功。"""

    def boom(app_path=None):
        raise WbCryptoError("未找到 WorkBuddy 客户端，无法解密信封")

    monkeypatch.setattr(wb_crypto, "get_protector_key", boom)

    outcome = WorkBuddyRefresher().refresh(_credential(_info()))

    assert outcome.status == STATUS_UNSUPPORTED
    assert "无法解密" in outcome.detail
