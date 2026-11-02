"""Trae 续期（ExchangeToken + DeviceProof）协议单元测试。

对齐官方客户端 v0.1.45 的 ``exchangeTokenByRefreshToken``：
``POST /trae/api/v3/oauth/ExchangeToken``，体里的 ``DeviceProof`` 用签发该
refreshToken 的设备私钥做 ECDSA-SHA256 签名；少了它（或换了设备）服务端会
回 ``20403 Token device not match``。
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from chaierww.domain.credential_refresh import (
    STATUS_REFRESHED,
    STATUS_RELOGIN_REQUIRED,
    TraeRefresher,
)
from chaierww.domain.trae_crypto import tc_decrypt, tc_encrypt
from chaierww.domain.trae_oauth import CLIENT_ID, EXCHANGE_TOKEN_PATH


def _keypair():
    key = ec.generate_private_key(ec.SECP256R1())
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private, public, key.public_key()


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


def _oauth_credential(private: str, public: str) -> dict:
    return {
        "format": "oauth-token",
        "uid": "u-1",
        "accessToken": "Cloud-IDE-JWT jwt-old",
        "refreshToken": "rt-old",
        "host": "https://api.trae.cn",
        "deviceId": "dev-1",
        "devicePublicKey": public,
        "devicePrivateKey": private,
        "deviceMachineId": "mach-1",
    }


def test_exchange_request_matches_client_protocol(monkeypatch):
    private, public, verify_key = _keypair()
    credential = _oauth_credential(private, public)
    response = httpx.Response(
        200,
        json={
            "Result": {
                "Token": "jwt-new",
                "RefreshToken": "rt-new",
                "TokenExpireAt": 1_800_000_000,
                "TokenExpireDuration": 3_600_000,
            }
        },
    )
    client = _FakeClient(response)
    monkeypatch.setattr(
        "chaierww.domain.credential_refresh.httpx.Client", lambda **kwargs: client
    )

    outcome = TraeRefresher().refresh(credential)
    assert outcome.status == STATUS_REFRESHED
    assert outcome.credential is not None
    assert outcome.credential["accessToken"] == "jwt-new"
    assert outcome.credential["refreshToken"] == "rt-new"
    # 非令牌字段原样保留（设备私钥要跟着走，否则下次续期必失败）。
    assert outcome.credential["devicePrivateKey"] == private

    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["url"].endswith(EXCHANGE_TOKEN_PATH)
    assert call["headers"]["x-cloudide-token"] == "jwt-old"
    body = call["json"]
    assert body["ClientID"] == CLIENT_ID
    assert body["ClientSecret"] == ""
    assert body["RefreshToken"] == "rt-old"
    assert body["DeviceInfo"]["DeviceID"] == "dev-1"
    assert body["DeviceInfo"]["DevicePublicKey"] == public.strip()
    assert body["DeviceInfo"]["DeviceType"] == "PC"
    assert set(body["DeviceProof"]) == {"Signature", "Timestamp", "Nonce"}

    proof = body["DeviceProof"]
    message = "\n".join(
        [
            "POST",
            EXCHANGE_TOKEN_PATH,
            CLIENT_ID,
            "rt-old",
            str(proof["Timestamp"]),
            proof["Nonce"],
        ]
    )
    verify_key.verify(
        base64.b64decode(proof["Signature"]),
        message.encode(),
        ec.ECDSA(hashes.SHA256()),
    )


def test_device_not_match_is_relogin_required(monkeypatch):
    private, public, _key = _keypair()
    response = httpx.Response(
        401,
        json={
            "ResponseMetadata": {
                "Error": {"Code": "20403", "Message": "Token device not match"}
            }
        },
    )
    client = _FakeClient(response)
    monkeypatch.setattr(
        "chaierww.domain.credential_refresh.httpx.Client", lambda **kwargs: client
    )
    outcome = TraeRefresher().refresh(_oauth_credential(private, public))
    assert outcome.status == STATUS_RELOGIN_REQUIRED
    assert "device not match" in outcome.detail


def test_missing_device_key_asks_for_relogin(monkeypatch):
    """旧版导出没有设备私钥：如实要求重登，不能拿别的设备硬试。"""
    credential = {
        "format": "oauth-token",
        "uid": "u-1",
        "accessToken": "jwt-old",
        "refreshToken": "rt-old",
        "host": "https://api.trae.cn",
    }
    monkeypatch.setattr(
        "chaierww.domain.credential_refresh._local_device_keypair", lambda: None
    )
    outcome = TraeRefresher().refresh(credential)
    assert outcome.status == STATUS_RELOGIN_REQUIRED
    assert "设备私钥" in outcome.detail


def test_raw_file_refresh_writes_back_envelope(monkeypatch):
    """客户端信封凭据续期后仍是 raw-file，且 cloudide 里的 token 被更新。"""
    private, public, _key = _keypair()
    cloudide = {
        "token": "jwt-old",
        "refreshToken": "rt-old",
        "expiredAt": "2026-01-01T00:00:00.000Z",
        "userId": "u-1",
        "host": "https://api.trae.cn",
    }
    auth = {
        "iCubeAuthInfo://icube-dc:dev-1": tc_encrypt(
            json.dumps({"publicKeyPEM": public, "privateKeyPEM": private})
        ),
        "iCubeAuthInfo://icube.cloudide": tc_encrypt(json.dumps(cloudide)),
    }
    credential = {
        "format": "raw-file",
        "encoding": "base64",
        "data": base64.b64encode(json.dumps(auth).encode()).decode(),
    }
    response = httpx.Response(
        200,
        json={
            "Result": {
                "Token": "jwt-new",
                "RefreshToken": "rt-new",
                "TokenExpireAt": int(datetime.now(UTC).timestamp()) + 3600,
                "TokenExpireDuration": 3_600_000,
            }
        },
    )
    client = _FakeClient(response)
    monkeypatch.setattr(
        "chaierww.domain.credential_refresh.httpx.Client", lambda **kwargs: client
    )

    outcome = TraeRefresher().refresh(credential)
    assert outcome.status == STATUS_REFRESHED
    merged = outcome.credential
    assert merged is not None and merged["format"] == "raw-file"
    assert merged["data"] != credential["data"]

    data = json.loads(base64.b64decode(merged["data"]))
    info = json.loads(tc_decrypt(data["iCubeAuthInfo://icube.cloudide"]))
    assert info["token"] == "jwt-new"
    assert info["refreshToken"] == "rt-new"
    assert info["expiredAt"].endswith("Z")


def test_request_failure_is_transient(monkeypatch):
    private, public, _key = _keypair()

    def boom(**kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("chaierww.domain.credential_refresh.httpx.Client", boom)
    outcome = TraeRefresher().refresh(_oauth_credential(private, public))
    assert outcome.status == "transient"


def test_invalid_refresh_token_code_is_not_unsupported():
    """错误码集合来自客户端 ``RefreshTokenInvalid``；不能被当成端点不存在。"""
    from chaierww.domain.credential_refresh import TRAE_INVALID_REFRESH_CODES

    assert {"20324", "20101", "20403"} <= TRAE_INVALID_REFRESH_CODES