"""WorkBuddy 客户端信封解密。

5.9.2 起客户端把 ``accessToken`` / ``refreshToken`` 等字段写成 ``$wbEncrypted``
信封，本模块负责把信封解开，拿到明文 token，让现有 ``oauth-token`` 续期链路
（``WorkBuddyRefresher``）能直接复用。

## 解密链（2026-11 逆向确认）

1. 调 WorkBuddy 自带的原生模块 ``electron_browser_workbuddy_storage.loggerGet()``
   拿到 ``atRestSecretKey``（32 字节 base64 字符串）；
2. ``protector_key = SHA-256(atRestSecretKey.encode("utf-8"))``；
3. token 信封用 ``protector_key`` 做 AES-256-GCM，AAD 按
   ``buildAuthenticatedContextAad(keyId, framing="field")`` 构造；
4. 解密得到明文 JWT（accessToken / refreshToken）。

``loggerGet`` 是 WorkBuddy 自定义 Electron 内置 binding，只能在 WorkBuddy 的
Electron 运行时里调到，所以本模块用 ``ELECTRON_RUN_AS_NODE=1`` 起一个
WorkBuddy 子进程跑一行脚本取 key。取到的 protector key 进程内缓存，不重复起进程。

## 安全约束

- atRestSecretKey 与 protector key 只存在内存，不落盘、不打日志；
- 解密出的明文 token 只在调用方栈上传给请求头 / 续期器，不写入业务数据文件；
- 任何分支都不打印 token 或 key（只打状态与长度）。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import struct
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger("chaierww")

#: AAD 前缀与常量，复刻客户端 ``AtRestCrypto.buildAuthenticatedContextAad``。
_WB_AAD = b"WB-AAD\x00"
_FRAMING_TYPE = {"file": "WBEF1", "field": "WBEV1", "record": "WBER1", "stream": "WBES1"}
_FRAMING_NUM = {"file": 1, "field": 2, "record": 3, "stream": 4}

#: 调 loggerGet 的小脚本：只输出 JSON 里的 atRestSecretKey 字段，不打印其它。
_LOGGER_SCRIPT = r"""
const wb = process._linkedBinding('electron_browser_workbuddy_storage');
const r = JSON.parse(wb.loggerGet());
process.stdout.write(r.atRestSecretKey);
"""

#: protector key 进程内缓存：atRestSecretKey 由客户端 DPAPI 保护，重装客户端才会变。
_lock = threading.Lock()
_cached_protector_key: bytes | None = None
_cached_app_path: str | None = None


class WbCryptoError(RuntimeError):
    """WorkBuddy 信封解密失败。消息面向用户，可直接展示。"""


def _encode_uint32(v: int) -> bytes:
    return struct.pack(">I", v)


def _encode_length_prefixed(s: str) -> bytes:
    b = s.encode("utf-8")
    return _encode_uint32(len(b)) + b


def _encode_optional_uint64(v: int | None) -> bytes:
    if v is None:
        return b"\x00"
    return b"\x01" + struct.pack(">Q", v)


def _build_aad(key_id: str, framing: str = "field") -> bytes:
    """复刻客户端 ``buildAuthenticatedContextAad(keyId, 1, {framing}, 'sym-v1')``。"""
    final_byte = 0  # file/field 信封不带 sequence / final
    return b"".join(
        [
            _WB_AAD,
            bytes([1]),
            _encode_length_prefixed(_FRAMING_TYPE[framing]),
            _encode_length_prefixed("sym-v1"),
            _encode_uint32(1),
            _encode_length_prefixed(key_id),
            bytes([_FRAMING_NUM[framing]]),
            _encode_optional_uint64(None),
            bytes([final_byte]),
        ]
    )


def _envelope_body(value: Any) -> dict[str, Any] | None:
    """从 ``{$wbEncrypted, envelope}`` 或原始 envelope dict 里取 envelope JSON。"""
    if not isinstance(value, dict):
        return None
    if "envelope" in value:
        raw = value.get("envelope")
    elif all(k in value for k in ("suite", "keyId", "nonce", "authTag", "ciphertext")):
        return value
    else:
        return None
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        body = json.loads(base64.b64decode(raw, validate=True).decode("utf-8", "replace"))
    except (ValueError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


def _decrypt_envelope(envelope: dict[str, Any], key: bytes, framing: str = "field") -> bytes:
    """解一个 ``{suite, keyId, nonce, authTag, ciphertext}`` 信封。"""
    key_id = str(envelope.get("keyId") or "")
    nonce = base64.b64decode(envelope["nonce"])
    tag = base64.b64decode(envelope["authTag"])
    ciphertext = base64.b64decode(envelope["ciphertext"])
    aad = _build_aad(key_id, framing)
    return AESGCM(key).decrypt(nonce, ciphertext + tag, aad)


def _encrypt_envelope(
    plaintext: bytes, key: bytes, key_id: str, *, suite: int = 1, framing: str = "field"
) -> dict[str, Any]:
    """构造一个 ``{suite, keyId, nonce, authTag, ciphertext}`` 信封。

    nonce 12 字节随机、authTag 16 字节（AES-GCM）。``suite`` 取客户端实测值 ``1``。
    """
    nonce = secrets.token_bytes(12)
    aad = _build_aad(key_id, framing)
    ct_and_tag = AESGCM(key).encrypt(nonce, plaintext, aad)
    ciphertext = ct_and_tag[:-16]
    tag = ct_and_tag[-16:]
    return {
        "suite": suite,
        "keyId": key_id,
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "authTag": base64.b64encode(tag).decode("ascii"),
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }


def _wrap_envelope(body: dict[str, Any]) -> dict[str, Any]:
    """把信封体包成客户端识别的 ``{$wbEncrypted: 1, envelope: <base64>}`` 形态。"""
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    envelope = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    return {"$wbEncrypted": 1, "envelope": envelope}


def _protector_key_id(key: bytes) -> str:
    return hashlib.sha256(key).hexdigest()[:16]


def reencrypt_workbuddy_auth(
    raw: bytes,
    *,
    access_token: str | None = None,
    refresh_token: str | None = None,
    expires_at: int | None = None,
    refresh_expires_at: int | None = None,
    app_path: Path | None = None,
) -> bytes:
    """把新 token 重新加密写回 ``workbuddy-desktop.info`` 原文，返回新的 raw bytes。

    只替换 ``auth.accessToken`` / ``auth.refreshToken`` 两个信封，以及可选的过期时间
    （毫秒时间戳）；account / accounts / allAccounts 等其余字段原样保留，避免切号时
    丢昵称、手机号等信息。原字段是明文（旧版）时也改写明文，保持与原值同形态。
    """
    body = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise WbCryptoError("登录态文件不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise WbCryptoError("登录态结构异常")
    auth = data.get("auth")
    if not isinstance(auth, dict):
        raise WbCryptoError("登录态里没有 auth 段")

    if access_token is not None or refresh_token is not None:
        key = get_protector_key(app_path)
        key_id = _protector_key_id(key)
        for field, value in (
            ("accessToken", access_token),
            ("refreshToken", refresh_token),
        ):
            if value is None:
                continue
            current = auth.get(field)
            if isinstance(current, dict) and "envelope" in current:
                env = _encrypt_envelope(value.encode("utf-8"), key, key_id, framing="field")
                auth[field] = _wrap_envelope(env)
            else:
                auth[field] = value

    if expires_at is not None:
        auth["expiresAt"] = expires_at
    if refresh_expires_at is not None:
        auth["refreshExpiresAt"] = refresh_expires_at

    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _fetch_at_rest_secret_key(app_path: Path) -> str:
    """起 WorkBuddy 子进程调 ``loggerGet`` 拿 atRestSecretKey。"""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".js", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(_LOGGER_SCRIPT)
        script_path = tmp.name
    try:
        env = dict(os.environ)
        env["ELECTRON_RUN_AS_NODE"] = "1"
        proc = subprocess.run(
            [str(app_path), script_path],
            capture_output=True,
            text=True,
            env=env,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise WbCryptoError(f"无法启动 WorkBuddy 取密钥：{type(exc).__name__}") from exc
    finally:
        with __import__("contextlib").suppress(OSError):
            os.unlink(script_path)

    secret = (proc.stdout or "").strip()
    if not secret:
        stderr = (proc.stderr or "").strip()[:200]
        raise WbCryptoError(f"WorkBuddy 未返回密钥（exit={proc.returncode}）{stderr}")
    return secret


def get_protector_key(app_path: Path | None = None) -> bytes:
    """返回本机 WorkBuddy 的 protector key（32 字节），进程内缓存。

    ``app_path`` 缺省时按适配器默认路径找 WorkBuddy.exe；找不到就抛错。
    """
    global _cached_protector_key, _cached_app_path
    with _lock:
        if _cached_protector_key is not None and (
            app_path is None or str(app_path) == _cached_app_path
        ):
            return _cached_protector_key

        if app_path is None:
            from chaierww.adapters.workbuddy import default_paths

            paths = default_paths()
            app_path = next((c for c in paths.app_candidates if c.is_file()), None)
            if app_path is None:
                raise WbCryptoError("未找到 WorkBuddy 客户端，无法解密信封")

        secret = _fetch_at_rest_secret_key(app_path)
        key = hashlib.sha256(secret.encode("utf-8")).digest()
        key_id = hashlib.sha256(key).hexdigest()[:16]
        logger.info("WorkBuddy protector key 已加载（keyId=%s）", key_id)
        _cached_protector_key = key
        _cached_app_path = str(app_path)
        return key


def decrypt_workbuddy_auth(raw: bytes, app_path: Path | None = None) -> dict[str, Any]:
    """把 ``workbuddy-desktop.info`` 原文解成明文凭据。

    返回 ``{accessToken, refreshToken, uid, domain, expiresAt, refreshExpiresAt}``，
    字段缺失时对应值为空串 / None。信封解不开抛 :class:`WbCryptoError`。
    """
    body = raw[3:] if raw.startswith(b"\xef\xbb\xbf") else raw
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise WbCryptoError("登录态文件不是有效 JSON") from exc
    if not isinstance(data, dict):
        raise WbCryptoError("登录态结构异常")

    auth = data.get("auth")
    if not isinstance(auth, dict):
        raise WbCryptoError("登录态里没有 auth 段")

    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    key = get_protector_key(app_path)

    result: dict[str, Any] = {
        "uid": str(account.get("uid") or ""),
        "domain": str(auth.get("domain") or ""),
    }
    for field in ("accessToken", "refreshToken"):
        env = _envelope_body(auth.get(field))
        if env is None:
            # 旧版明文 token
            raw_val = auth.get(field)
            result[field] = str(raw_val) if isinstance(raw_val, str) else ""
            continue
        try:
            plaintext = _decrypt_envelope(env, key, framing="field")
            result[field] = plaintext.decode("utf-8", "replace").strip()
        except Exception as exc:  # noqa: BLE001
            raise WbCryptoError(f"解密 {field} 失败：信封可能不属于本机") from exc

    # 过期时间直接读 auth 段（未加密字段），统一成毫秒时间戳
    for src, dst in (
        ("expiresAt", "expiresAt"),
        ("expiresIn", "expiresIn"),
        ("refreshExpiresAt", "refreshExpiresAt"),
        ("refreshExpiresIn", "refreshExpiresIn"),
    ):
        val = auth.get(src)
        if val is not None:
            result[dst] = val
    return result


def decrypt_workbuddy_credential(
    credential: dict[str, Any], app_path: Path | None = None
) -> dict[str, Any]:
    """把 ``raw-file`` 凭据解成 ``oauth-token`` 形态的 dict（不写库，返回新 dict）。

    非 raw-file 直接返回原凭据的浅拷贝。
    """
    if credential.get("format") != "raw-file":
        return dict(credential)

    from chaierww.domain.platform_api import CredentialError, decode_raw_file

    try:
        data = decode_raw_file(credential)
    except CredentialError as exc:
        raise WbCryptoError(str(exc)) from exc

    raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
    auth = decrypt_workbuddy_auth(raw, app_path)
    return {
        "format": "oauth-token",
        "uid": auth.get("uid", ""),
        "domain": auth.get("domain", ""),
        "accessToken": auth.get("accessToken", ""),
        "refreshToken": auth.get("refreshToken", ""),
        "expiresAt": auth.get("expiresAt"),
        "refreshExpiresAt": auth.get("refreshExpiresAt"),
    }
