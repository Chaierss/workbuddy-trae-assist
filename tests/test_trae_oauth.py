"""Trae 扫码登录（tc 信封解密 + OAuth 构造）单元测试。"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from chaierww.domain.trae_crypto import (
    TC_HEADER,
    TraeCryptoError,
    _aes_pepper,
    _derive_keys,
    device_identity_from_storage,
    tc_decrypt,
)
from chaierww.domain.trae_oauth import (
    ACCESS_TOKEN_KEYS,
    CALLBACK_PORT,
    CLIENT_ID,
    REFRESH_TOKEN_KEYS,
    TraeOAuth,
    TraeOAuthError,
    _find_token_value,
    _parse_jwt_uid,
    _parse_query,
    _pkce_pair,
    build_authorize_url,
)

# ---------- tc 信封加密（测试用反向实现，与解密对称） ----------


def _tc_encrypt(plain: str) -> str:
    """用与解密对称的算法造一条合法 tc 信封（仅测试用）。"""
    body = plain.encode("utf-8")
    tag = hashlib.sha512(body).digest()
    padded = tag + body
    # PKCS7 补齐到 16 的倍数
    pad_len = 16 - (len(padded) % 16)
    padded += bytes([pad_len]) * pad_len

    random = b"R" * 32
    key, iv = _derive_keys(random, _aes_pepper())
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    cipher = encryptor.update(padded) + encryptor.finalize()

    raw = TC_HEADER + random + cipher
    return base64.b64encode(raw).decode("ascii")


def test_tc_decrypt_roundtrip():
    public_key = "-----BEGIN PUBLIC KEY-----\nabc\n-----END PUBLIC KEY-----"
    plain = json.dumps({"publicKeyPEM": public_key})
    assert tc_decrypt(_tc_encrypt(plain)) == plain


def test_tc_decrypt_rejects_bad_magic():
    bad = base64.b64encode(b"\x00" * 64).decode("ascii")
    with pytest.raises(TraeCryptoError):
        tc_decrypt(bad)


def test_tc_decrypt_rejects_short_envelope():
    short = base64.b64encode(TC_HEADER + b"x" * 10).decode("ascii")
    with pytest.raises(TraeCryptoError):
        tc_decrypt(short)


def test_tc_decrypt_rejects_tampered_body():
    envelope = _tc_encrypt('{"publicKeyPEM":"k"}')
    raw = bytearray(base64.b64decode(envelope))
    raw[-1] ^= 0xFF
    tampered = base64.b64encode(bytes(raw)).decode("ascii")
    with pytest.raises(TraeCryptoError):
        tc_decrypt(tampered)


def test_device_identity_from_storage():
    public_key = "-----BEGIN PUBLIC KEY-----\nMFkw\n-----END PUBLIC KEY-----"
    storage = {
        "iCubeAuthInfo://icube-dc:1111222233334444": _tc_encrypt(
            json.dumps({"publicKeyPEM": public_key, "privateKeyPEM": "SECRET"})
        ),
        "telemetry.machineId": "mach-123",
    }
    ident = device_identity_from_storage(storage)
    assert ident.device_id == "1111222233334444"
    assert ident.machine_id == "mach-123"
    assert ident.public_key_pem == public_key


def test_device_identity_missing_dc_key():
    with pytest.raises(TraeCryptoError):
        device_identity_from_storage({"telemetry.machineId": "mach"})


def test_pepper_head_matches_known_value():
    # 对照旧项目 icube.rs 测试：AES 模式 pepper 前 6 字节 = 4d d4 c2 e6 b8 31。
    assert _aes_pepper()[:6] == bytes([0x4D, 0xD4, 0xC2, 0xE6, 0xB8, 0x31])


# ---------- PKCE / URL ----------


def test_pkce_pair_lengths():
    verifier, challenge = _pkce_pair()
    assert len(verifier) == 64
    # base64url-nopad 的 SHA256 = 43 字符
    assert len(challenge) == 43
    assert "=" not in challenge


def test_build_authorize_url_has_22_params():
    verifier, challenge = _pkce_pair()
    url = build_authorize_url(
        device_id="d1", machine_id="m1", trace_id="t" * 32, code_challenge=challenge
    )
    assert url.startswith("https://www.trae.cn/authorization?")
    params = url.split("?", 1)[1].split("&")
    assert len(params) == 22
    assert f"client_id={CLIENT_ID}" in url
    assert "code_challenge_method=S256" in url
    assert f"login_trace_id={'t' * 32}" in url
    # 回调地址整体 URL 编码（: 与 / 都编码）。
    assert f"auth_callback_url=http%3A%2F%2F127.0.0.1%3A{CALLBACK_PORT}%2Fauthorize" in url


def test_build_authorize_url_urlencodes_callback():
    verifier, challenge = _pkce_pair()
    url = build_authorize_url(
        device_id="d", machine_id="m", trace_id="t" * 32, code_challenge=challenge
    )
    # auth_callback_url 里的 http:// 与 : 应被 URL 编码
    assert "auth_callback_url=http%3A%2F%2F127.0.0.1" in url


# ---------- 回调解析 ----------


def test_parse_query_splits_on_first_equals():
    params = _parse_query("refreshToken=a=b=c&code=xyz")
    assert params["refreshToken"] == "a=b=c"
    assert params["code"] == "xyz"


def test_parse_query_decodes_percent():
    params = _parse_query("host=https%3A%2F%2Fapi.trae.cn&userRegion=cn")
    assert params["host"] == "https://api.trae.cn"
    assert params["userRegion"] == "cn"


def test_parse_jwt_uid():
    payload = base64.urlsafe_b64encode(
        json.dumps({"data": {"id": "1111222233334444"}}).encode()
    ).rstrip(b"=").decode()
    jwt = f"h.{payload}.sig"
    assert _parse_jwt_uid(jwt) == "1111222233334444"


def test_parse_jwt_uid_missing():
    assert _parse_jwt_uid("h.notbase64.sig") is None
    payload = base64.urlsafe_b64encode(json.dumps({"foo": 1}).encode()).rstrip(b"=").decode()
    assert _parse_jwt_uid(f"h.{payload}.sig") is None


# ---------- 换 token 响应解析 ----------


def test_find_token_value_reads_new_result_envelope():
    """2026-10-30 新版响应：``Result`` 容器 + 首字母大写的 ``Token``。"""
    body = {"Result": {"Token": "jwt-1", "RefreshToken": "rt-1"}}
    assert _find_token_value(body, ACCESS_TOKEN_KEYS) == "jwt-1"
    assert _find_token_value(body, REFRESH_TOKEN_KEYS) == "rt-1"


def test_find_token_value_is_case_insensitive_in_nested_data():
    body = {"data": {"tokens": [{"accessToken": "jwt-2"}]}}
    assert _find_token_value(body, ("AccessToken", "access_token", "token")) == "jwt-2"


def test_exchange_accepts_new_result_envelope(monkeypatch):
    """主变体返回 ``Result.Token`` 时应直接成功，而不是落到旧端点兜底。"""
    oauth = TraeOAuth()
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"Result": {"Token": "jwt-new", "RefreshToken": "rt-new"}}

    class FakeClient:
        def post(self, url, json=None, headers=None):  # noqa: A002
            calls.append(url)
            return FakeResponse()

    monkeypatch.setattr(oauth, "_client", FakeClient())
    payload = {"ClientID": CLIENT_ID, "AuthCode": "ac-1", "CodeVerifier": "v-1"}
    jwt, refresh = oauth._try_exchange(
        "ExchangeToken/DeviceInfo", "http://x/exchange", payload, "dev-1", True
    )
    assert (jwt, refresh) == ("jwt-new", "rt-new")
    assert len(calls) == 1


def test_exchange_reports_upstream_field_error(monkeypatch):
    """新版把业务错误放 ``Data.__Message.error``：要原样报出来，别再兜底重试。"""
    oauth = TraeOAuth()

    class FakeResponse:
        status_code = 200

        def json(self):
            return {"Data": {"__Message": {"error": "invalid field: AuthCode"}}}

    class FakeClient:
        def post(self, url, json=None, headers=None):  # noqa: A002
            return FakeResponse()

    monkeypatch.setattr(oauth, "_client", FakeClient())
    with pytest.raises(TraeOAuthError, match="invalid field: AuthCode"):
        oauth._try_exchange("ExchangeToken/DeviceInfo", "http://x/exchange", {}, "dev-1", True)
