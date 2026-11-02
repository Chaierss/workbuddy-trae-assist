"""凭据续期协议层（0.2.0 阶段 1）。

把「用 refreshToken 换新的 accessToken」这件事关在一个模块里：上游端点一变，
只改这里，不动生命周期服务与路由。

## 诚实边界

1. 端点来自阶段 0 的探针（`scripts/_probe_refresh.py`），**没有实测通过之前**不假设
   它一定存在：候选端点全返回 404 / 405，或者 200 但响应里没有令牌，一律归类成
   `unsupported`，由界面如实写「当前版本未验证到可用的续期端点」。
2. 两种凭据形态都能续期：`oauth-token`（扫码明文，凭据内带设备私钥）与 `raw-file`
   （客户端信封，设备私钥在信封里）。Trae 2026-10 起续期要校验 `DeviceProof`，
   **没有设备私钥就无法续期**，只能重新登录；这一点不掩饰、不重试。
3. 日志只写状态与账号 id，**任何分支都不打印 token**。

## 结果状态

- `refreshed`：拿到新 accessToken（可能同时拿到新 refreshToken）；
- `relogin_required`：上游明确拒绝（400 / 401 / invalid_grant），只能重新登录；
- `unsupported`：端点不存在或响应形状不认识 —— 不是账号的问题；
- `transient`：网络 / 429 / 5xx / WAF 指纹拦截（WorkBuddy 403 code=10085），
  下一次可能就好了，**不得据此降级账号状态**。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from chaierww.domain.platform_api import (
    ICUBE_CLOUDIDE_KEY,
    TIMEOUT_SECONDS,
    TRAE_APP_VERSION,
    CredentialError,
    decode_raw_file,
    trae_credentials,
    workbuddy_credentials,
    workbuddy_headers,
    workbuddy_origin,
)
from chaierww.domain.trae_crypto import (
    DeviceKeyPair,
    TraeCryptoError,
    device_keypair_from_auth_blob,
    device_keypair_from_storage,
    sign_device_proof,
    tc_decrypt,
    tc_encrypt,
)
from chaierww.domain.trae_oauth import CLIENT_ID, EXCHANGE_TOKEN_PATH

logger = logging.getLogger("chaierww")

STATUS_REFRESHED = "refreshed"
STATUS_RELOGIN_REQUIRED = "relogin_required"
STATUS_UNSUPPORTED = "unsupported"
STATUS_TRANSIENT = "transient"

#: WorkBuddy 的 WAF 指纹错误码：403 + code=10085 是「像未知客户端」，不是令牌失效。
WB_WAF_CODE = 10085

#: 候选刷新端点。顺序即优先级；全部 404 / 405 视为「上游没有这个协议」。
WB_REFRESH_PATHS = ("/v2/plugin/auth/token/refresh", "/v2/plugin/auth/refresh")
TRAE_REFRESH_PATH = EXCHANGE_TOKEN_PATH

#: Trae 明确判定 refreshToken 失效（或设备不匹配）的错误码，来自客户端
#: ``request.js`` 的 ``RefreshTokenInvalid`` 集合；命中即只能重新登录。
TRAE_INVALID_REFRESH_CODES = {
    "20324",
    "20101",
    "20315",
    "20125",
    "20126",
    "20401",
    "20403",
}


#: 响应里可能承载新令牌的键（按优先级；大小写与下划线两种写法都见得到）。
_ACCESS_KEYS = ("accessToken", "access_token", "AccessToken", "token", "Token", "jwt", "JWT")
_REFRESH_KEYS = ("refreshToken", "refresh_token", "RefreshToken")
_EXPIRY_KEYS = ("expiresAt", "expires_at", "ExpiresAt", "expireTime", "expire_time")
_EXPIRES_IN_KEYS = ("expiresIn", "expires_in", "ExpiresIn")


@dataclass(frozen=True, slots=True)
class RefreshOutcome:
    """一次续期尝试的结论。`credential` 只在成功时非空。"""

    status: str
    credential: dict[str, Any] | None = None
    expires_at: str | None = None
    detail: str = ""
    retry_after: float | None = None


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _iso_from_epoch(value: Any) -> str | None:
    """epoch 秒 / 毫秒 → ISO；非法值返回 None。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    stamp = float(value)
    if stamp > 10_000_000_000:
        stamp /= 1000.0
    try:
        return datetime.fromtimestamp(stamp, tz=UTC).replace(microsecond=0).isoformat()
    except (OSError, ValueError, OverflowError):
        return None


def _iso_expiry(payload: Any) -> str | None:
    """从响应里找过期时间：绝对时间优先，其次 `expiresIn` 秒数。"""
    found = _search(payload, _EXPIRY_KEYS)
    if found is not None:
        if isinstance(found, str) and found.strip():
            try:
                return datetime.fromisoformat(found.strip()).replace(microsecond=0).isoformat()
            except ValueError:
                pass
        parsed = _iso_from_epoch(found)
        if parsed:
            return parsed
    seconds = _search(payload, _EXPIRES_IN_KEYS)
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
        expires = datetime.now(UTC) + timedelta(seconds=float(seconds))
        return expires.replace(microsecond=0).isoformat()
    return None


def _search(payload: Any, keys: tuple[str, ...], *, depth: int = 4) -> Any:
    """在嵌套响应里按优先级找第一个命中的键。

    上游的包裹层不统一（`data` / `result` / 直接顶层都见过），所以做有界深度搜索；
    先按 keys 的给定顺序做一层扫描，避免不同键的命中顺序随字典遍历顺序漂移。
    """
    if depth < 0:
        return None
    if isinstance(payload, dict):
        for key in keys:
            if key in payload and payload[key] not in (None, ""):
                return payload[key]
        for value in payload.values():
            found = _search(value, keys, depth=depth - 1)
            if found is not None:
                return found
        return None
    if isinstance(payload, list):
        for item in payload[:8]:
            found = _search(item, keys, depth=depth - 1)
            if found is not None:
                return found
    return None


def _retry_after(resp: httpx.Response) -> float | None:
    raw = (resp.headers.get("retry-after") or "").strip()
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except ValueError:
        return None


def _payload_of(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except ValueError:
        return None


def _code_of(payload: Any) -> Any:
    if isinstance(payload, dict):
        return payload.get("code", payload.get("error_code", payload.get("error")))
    return None


def _message_of(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("msg", "message", "error_description", "detail"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _merge(
    old: dict[str, Any], access: str, refresh: str | None, expires_at: str | None
) -> dict[str, Any]:
    """新旧凭据合并：令牌替换，其余字段（uid / domain / host / deviceId）原样保留。

    响应没带新 refreshToken 时**保留旧值** —— 旋转式上游有时不返回，直接置空会让
    下一次续期彻底没救。
    """
    merged = dict(old)
    merged["format"] = "oauth-token"
    # 两种命名都写，下游解包函数对两种都认。
    merged["accessToken"] = access
    merged.pop("access_token", None)
    if refresh:
        merged["refreshToken"] = refresh
        merged.pop("refresh_token", None)
    if expires_at:
        merged["expiresAt"] = expires_at
        merged.pop("expires_at", None)
    return merged


def _outcome_from_response(
    resp: httpx.Response, old: dict[str, Any], *, endpoint: str
) -> RefreshOutcome:
    """把一次 HTTP 响应翻译成 `RefreshOutcome`（成功 / 需重登 / 瞬时 / 不支持）。"""
    payload = _payload_of(resp)
    status = resp.status_code
    code = _code_of(payload)
    message = _message_of(payload)

    if status == 200:
        access = _search(payload, _ACCESS_KEYS)
        if isinstance(access, str) and access.strip():
            refresh = _search(payload, _REFRESH_KEYS)
            merged = _merge(
                old,
                access.strip(),
                refresh.strip() if isinstance(refresh, str) and refresh.strip() else None,
                _iso_expiry(payload),
            )
            return RefreshOutcome(
                status=STATUS_REFRESHED,
                credential=merged,
                expires_at=merged.get("expiresAt"),
                detail=f"已通过 {endpoint} 续期",
            )
        # 200 但没有令牌：不是账号的问题，是协议形状对不上。
        return RefreshOutcome(
            status=STATUS_UNSUPPORTED,
            detail=f"{endpoint} 返回 200 但响应里没有新令牌",
        )

    if status in (404, 405, 501):
        return RefreshOutcome(
            status=STATUS_UNSUPPORTED, detail=f"{endpoint} 不存在（HTTP {status}）"
        )

    if status == 403 and code == WB_WAF_CODE:
        # 指纹拦截：换号 / 重试都可能过，绝不据此判定登录失效。
        return RefreshOutcome(
            status=STATUS_TRANSIENT,
            detail="上游 WAF 拦截（code=10085），不是令牌失效",
        )

    if status == 429 or status >= 500:
        return RefreshOutcome(
            status=STATUS_TRANSIENT,
            detail=message or f"上游暂不可用（HTTP {status}）",
            retry_after=_retry_after(resp),
        )

    if status in (400, 401):
        return RefreshOutcome(
            status=STATUS_RELOGIN_REQUIRED,
            detail=message or f"刷新被拒绝（HTTP {status}），请重新登录",
        )

    if status == 403:
        return RefreshOutcome(
            status=STATUS_RELOGIN_REQUIRED,
            detail=message or "刷新被拒绝（HTTP 403），请重新登录",
        )

    return RefreshOutcome(
        status=STATUS_TRANSIENT,
        detail=message or f"未预期的响应（HTTP {status}）",
    )


class _Refresher:
    """两个平台共用的「逐个候选端点试一次」流程。"""

    platform = ""
    paths: tuple[str, ...] = ()

    def _base(self, credential: dict[str, Any]) -> str:
        raise NotImplementedError

    def _headers(self, credential: dict[str, Any]) -> dict[str, str]:
        raise NotImplementedError

    def _bodies(self, credential: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        raise NotImplementedError

    def refresh(self, credential: dict[str, Any]) -> RefreshOutcome:
        refresh_token = credential.get("refreshToken") or credential.get("refresh_token")
        if not isinstance(refresh_token, str) or not refresh_token.strip():
            return RefreshOutcome(status=STATUS_UNSUPPORTED, detail="凭据里没有 refreshToken")
        if credential.get("format") != "oauth-token":
            return RefreshOutcome(
                status=STATUS_UNSUPPORTED,
                detail="客户端信封凭据不可解密，无法自动续期",
            )
        try:
            base = self._base(credential)
            headers = self._headers(credential)
        except CredentialError as exc:
            return RefreshOutcome(status=STATUS_UNSUPPORTED, detail=str(exc))

        bodies = self._bodies(credential)
        unsupported: list[str] = []
        for path in self.paths:
            url = f"{base.rstrip('/')}{path}"
            for body in bodies:
                try:
                    with httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False) as client:
                        resp = client.post(url, headers=headers, json=body)
                except httpx.HTTPError as exc:
                    return RefreshOutcome(
                        status=STATUS_TRANSIENT,
                        detail=f"续期请求失败：{type(exc).__name__}",
                    )
                outcome = _outcome_from_response(resp, credential, endpoint=url)
                if outcome.status != STATUS_UNSUPPORTED:
                    return outcome
                unsupported.append(outcome.detail)
        return RefreshOutcome(status=STATUS_UNSUPPORTED, detail="；".join(unsupported))


class WorkBuddyRefresher(_Refresher):
    platform = "workbuddy"
    paths = WB_REFRESH_PATHS

    def _base(self, credential: dict[str, Any]) -> str:
        domain = str(credential.get("domain") or "")
        return workbuddy_origin(domain)

    def _headers(self, credential: dict[str, Any]) -> dict[str, str]:
        token, uid, domain = workbuddy_credentials(credential)
        headers = workbuddy_headers(token, uid, domain)
        headers["Content-Type"] = "application/json"
        return headers

    def _bodies(self, credential: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        token = str(credential.get("refreshToken") or credential.get("refresh_token") or "")
        return (
            {"refreshToken": token},
            {"refresh_token": token, "grant_type": "refresh_token"},
        )


class TraeRefresher(_Refresher):
    """Trae 续期：2026-10 起改走 ``ExchangeToken`` + ``DeviceProof``。

    官方客户端（v0.1.45）的真实协议：

    ``POST {host}/trae/api/v3/oauth/ExchangeToken``，头只带
    ``Content-Type`` 与 ``x-cloudide-token``（旧 accessToken）；体为
    ``{ClientID, ClientSecret:"", RefreshToken, DeviceInfo, DeviceProof, IDEVersion}``。
    ``DeviceProof`` 用**签发该 refreshToken 的设备私钥**对
    ``"POST\\n/trae/api/v3/oauth/ExchangeToken\\n{ClientID}\\n{refreshToken}\\n{ts}\\n{nonce}"``
    做 ECDSA-SHA256 签名。

    旧端点 ``/trae/api/v3/oauth/RefreshToken`` 已失效（2026-11 实测 404/200 无令牌）。
    令牌与设备绑定：缺原设备私钥时服务端回 ``20403 Token device not match``，
    这种情况只能重新登录，不能靠换设备重试绕过。
    """

    platform = "trae"
    paths = (TRAE_REFRESH_PATH,)

    def refresh(self, credential: dict[str, Any]) -> RefreshOutcome:
        try:
            access, refresh_token, host = self._tokens(credential)
            device = self._device(credential)
        except CredentialError as exc:
            return RefreshOutcome(status=STATUS_UNSUPPORTED, detail=str(exc))

        if not refresh_token:
            return RefreshOutcome(
                status=STATUS_UNSUPPORTED, detail="凭据里没有 refreshToken"
            )
        if device is None:
            return RefreshOutcome(
                status=STATUS_RELOGIN_REQUIRED,
                detail=(
                    "凭据缺少设备私钥（旧版导出/其它机器导入的账号），"
                    "无法通过 Trae 的设备校验；请重新扫码登录或从本机客户端重新导入"
                ),
            )

        proof = sign_device_proof(
            method="POST",
            path=TRAE_REFRESH_PATH,
            client_id=CLIENT_ID,
            refresh_token=refresh_token,
            private_key_pem=device.private_key_pem,
        )
        body = {
            "ClientID": CLIENT_ID,
            "ClientSecret": "",
            "RefreshToken": refresh_token,
            "DeviceInfo": {
                "DeviceID": device.device_id,
                "MachineID": device.machine_id,
                "PlatformCode": "IDE_PC",
                "DeviceType": "PC",
                "DeviceName": self._device_name(),
                "DeviceModel": "",
                "ClientVersion": TRAE_APP_VERSION,
                "DevicePublicKey": device.public_key_pem,
                "DeviceBrand": "",
                "DeviceCPU": "",
                "OSInfo": self._os_info(),
                "OSVersion": "",
            },
            "DeviceProof": proof,
            "IDEVersion": TRAE_APP_VERSION,
        }
        headers = {
            "Content-Type": "application/json",
            "x-cloudide-token": access,
        }
        url = f"{host.rstrip('/')}{TRAE_REFRESH_PATH}"

        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False) as client:
                resp = client.post(url, headers=headers, json=body)
        except httpx.HTTPError as exc:
            return RefreshOutcome(
                status=STATUS_TRANSIENT,
                detail=f"续期请求失败：{type(exc).__name__}",
            )
        return self._interpret(resp, credential, endpoint=url)

    # ----- 凭据拆解 -----

    def _tokens(self, credential: dict[str, Any]) -> tuple[str, str, str]:
        """返回 ``(access_token, refresh_token, host)``。"""
        jwt, _device_id, host = trae_credentials(credential)
        if credential.get("format") == "oauth-token":
            refresh = credential.get("refreshToken") or credential.get("refresh_token")
            return _bare_jwt(jwt), str(refresh or "").strip(), host

        data = decode_raw_file(credential)
        info = _cloudide_info(data)
        refresh = info.get("refreshToken")
        return (
            _bare_jwt(str(info.get("token") or jwt)),
            str(refresh or "").strip(),
            host,
        )

    def _device(self, credential: dict[str, Any]) -> DeviceKeyPair | None:
        if credential.get("format") == "raw-file":
            try:
                return device_keypair_from_auth_blob(decode_raw_file(credential))
            except CredentialError:
                return None

        device_id = str(credential.get("deviceId") or "").strip()
        public_key = str(credential.get("devicePublicKey") or "").strip()
        private_key = str(credential.get("devicePrivateKey") or "").strip()
        machine_id = str(credential.get("deviceMachineId") or "").strip()
        if device_id and public_key and private_key:
            return DeviceKeyPair(device_id, machine_id, public_key, private_key)

        # 老凭据只有 deviceId（或什么都没有）：本机客户端若是同一台设备，
        # 用它的密钥对还能续期；否则服务端会回 20403，调用方据此提示重登。
        local = _local_device_keypair()
        if local is None:
            return None
        if device_id and local.device_id != device_id:
            return None
        return local

    @staticmethod
    def _os_info() -> str:
        if os.name == "nt":
            return "windows"
        return "linux" if os.name == "posix" else ""

    @staticmethod
    def _device_name() -> str:
        try:
            return socket.gethostname() or "chaierww"
        except OSError:
            return "chaierww"

    # ----- 响应翻译 -----

    def _interpret(
        self,
        resp: httpx.Response,
        credential: dict[str, Any],
        *,
        endpoint: str,
    ) -> RefreshOutcome:
        payload = _payload_of(resp)
        status = resp.status_code
        code = ""
        message = ""
        if isinstance(payload, dict):
            error = payload.get("ResponseMetadata")
            if isinstance(error, dict):
                detail = error.get("Error")
                if isinstance(detail, dict):
                    code = str(detail.get("Code") or "").strip()
                    message = str(detail.get("Message") or "").strip()

        if code and code in TRAE_INVALID_REFRESH_CODES:
            return RefreshOutcome(
                status=STATUS_RELOGIN_REQUIRED,
                detail=message or f"刷新被拒绝（code={code}），请重新登录",
            )
        if status in (404, 405, 501):
            return RefreshOutcome(
                status=STATUS_UNSUPPORTED, detail=f"{endpoint} 不存在（HTTP {status}）"
            )
        if status == 429 or status >= 500:
            return RefreshOutcome(
                status=STATUS_TRANSIENT,
                detail=message or f"上游暂不可用（HTTP {status}）",
                retry_after=_retry_after(resp),
            )
        if status in (400, 401, 403):
            return RefreshOutcome(
                status=STATUS_RELOGIN_REQUIRED,
                detail=message or f"刷新被拒绝（HTTP {status}），请重新登录",
            )
        if status != 200:
            return RefreshOutcome(
                status=STATUS_TRANSIENT,
                detail=message or f"未预期的响应（HTTP {status}）",
            )

        result = payload.get("Result") if isinstance(payload, dict) else None
        if not isinstance(result, dict):
            return RefreshOutcome(
                status=STATUS_UNSUPPORTED, detail=f"{endpoint} 响应里没有 Result"
            )
        token = str(result.get("Token") or "").strip()
        if not token:
            return RefreshOutcome(
                status=STATUS_UNSUPPORTED,
                detail=f"{endpoint} 返回 200 但响应里没有新 Token",
            )

        new_refresh = str(result.get("RefreshToken") or "").strip()
        expires_at = _trae_expiry(result.get("TokenExpireAt"), result.get("TokenExpireDuration"))
        refresh_expires = _iso_from_epoch(result.get("RefreshExpireAt"))

        if credential.get("format") == "raw-file":
            merged = _merge_raw_file(
                credential, token, new_refresh, expires_at, refresh_expires
            )
        else:
            merged = _merge(
                credential, token, new_refresh or None, expires_at
            )
        return RefreshOutcome(
            status=STATUS_REFRESHED,
            credential=merged,
            expires_at=expires_at,
            detail=f"已通过 {endpoint} 续期",
        )


def _bare_jwt(token: str) -> str:
    """去掉 ``Cloud-IDE-JWT `` 前缀：``x-cloudide-token`` 头里客户端放的是裸 JWT。"""
    value = token.strip()
    prefix = "Cloud-IDE-JWT "
    return value[len(prefix):] if value.startswith(prefix) else value


def _cloudide_info(data: dict[str, Any]) -> dict[str, Any]:
    """解密 ``raw-file`` 里的 cloudide 信封。"""
    envelope = data.get(ICUBE_CLOUDIDE_KEY)
    if not isinstance(envelope, str) or not envelope:
        raise CredentialError("登录态里没有 Trae 会话（iCubeAuthInfo://icube.cloudide）")
    try:
        info = json.loads(tc_decrypt(envelope))
    except (TraeCryptoError, json.JSONDecodeError) as exc:
        raise CredentialError(str(exc)) from exc
    if not isinstance(info, dict):
        raise CredentialError("Trae 会话结构异常")
    return info


def _trae_expiry(expire_at: Any, duration: Any) -> str | None:
    """按客户端 ``ZNe`` 语义算 accessToken 到期时间。

    ``TokenExpireAt`` 已过去且给了 ``TokenExpireDuration`` 时，用「现在 + 时长」；
    时长按毫秒理解（客户端直接把它加到毫秒时间戳上）。
    """
    if expire_at in (None, ""):
        return None
    stamp = _epoch_seconds(expire_at)
    if stamp is None:
        return None
    if stamp < datetime.now(UTC).timestamp():
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            return None
        # 客户端把 duration 当毫秒直接加在时间戳上，这里按数值换算成秒。
        raw = float(duration)
        seconds = raw / 1000.0 if raw >= 1000 else raw
        stamp = datetime.now(UTC).timestamp() + seconds
    return _iso_from_epoch(stamp)


def _epoch_seconds(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    stamp = float(value)
    if stamp > 10_000_000_000:
        stamp /= 1000.0
    return stamp


def _iso_ms(value: str) -> str:
    """ISO 时间 → 客户端信封里的毫秒 UTC 形态（``...123Z``）。"""
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _merge_raw_file(
    credential: dict[str, Any],
    token: str,
    refresh: str,
    expires_at: str | None,
    refresh_expires: str | None,
) -> dict[str, Any]:
    """续期后的 token 写回客户端信封，凭据形态仍是 ``raw-file``。

    保持 raw-file 形态，切换链路（``apply_switch``）才能继续原样写回客户端。
    """
    data = decode_raw_file(credential)
    info = _cloudide_info(data)
    info["token"] = token
    if refresh:
        info["refreshToken"] = refresh
    if expires_at:
        info["expiredAt"] = _iso_ms(expires_at)
    if refresh_expires:
        info["refreshExpiredAt"] = _iso_ms(refresh_expires)
    data[ICUBE_CLOUDIDE_KEY] = tc_encrypt(json.dumps(info, ensure_ascii=False))

    merged = dict(credential)
    merged["format"] = "raw-file"
    merged["encoding"] = "base64"
    merged["data"] = base64.b64encode(
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    ).decode("ascii")
    return merged


def _local_device_keypair() -> DeviceKeyPair | None:
    """读本机 ``storage.json`` 的设备密钥对（仅老凭据兜底，失败返回 None）。"""
    try:
        from chaierww.adapters.trae import default_paths

        storage = json.loads(default_paths().storage_file.read_text(encoding="utf-8"))
        return device_keypair_from_storage(storage)
    except (OSError, ValueError, TraeCryptoError, ImportError):
        return None



def refresher_for(platform: str) -> _Refresher | None:
    """平台名 → 续期器。未知平台返回 None（`unsupported`，不抛错）。"""
    if platform == "workbuddy":
        return WorkBuddyRefresher()
    if platform == "trae":
        return TraeRefresher()
    return None
