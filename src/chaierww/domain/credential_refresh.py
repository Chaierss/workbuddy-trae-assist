"""凭据续期协议层（0.2.0 阶段 1）。

把「用 refreshToken 换新的 accessToken」这件事关在一个模块里：上游端点一变，
只改这里，不动生命周期服务与路由。

## 诚实边界

1. 端点来自阶段 0 的探针（`scripts/_probe_refresh.py`），**没有实测通过之前**不假设
   它一定存在：候选端点全返回 404 / 405，或者 200 但响应里没有令牌，一律归类成
   `unsupported`，由界面如实写「当前版本未验证到可用的续期端点」。
2. 只有 `oauth-token`（扫码明文）能续期；客户端导入的 `raw-file` 信封里 refreshToken
   不可解密，调用方根本不该把这类账号送进来。
3. 日志只写状态与账号 id，**任何分支都不打印 token**。

## 结果状态

- `refreshed`：拿到新 accessToken（可能同时拿到新 refreshToken）；
- `relogin_required`：上游明确拒绝（400 / 401 / invalid_grant），只能重新登录；
- `unsupported`：端点不存在或响应形状不认识 —— 不是账号的问题；
- `transient`：网络 / 429 / 5xx / WAF 指纹拦截（WorkBuddy 403 code=10085），
  下一次可能就好了，**不得据此降级账号状态**。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from chaierww.domain.platform_api import (
    TIMEOUT_SECONDS,
    TRAE_ACCOUNT_BASE,
    CredentialError,
    trae_credentials,
    workbuddy_credentials,
    workbuddy_headers,
    workbuddy_origin,
)

logger = logging.getLogger("chaierww")

STATUS_REFRESHED = "refreshed"
STATUS_RELOGIN_REQUIRED = "relogin_required"
STATUS_UNSUPPORTED = "unsupported"
STATUS_TRANSIENT = "transient"

#: WorkBuddy 的 WAF 指纹错误码：403 + code=10085 是「像未知客户端」，不是令牌失效。
WB_WAF_CODE = 10085

#: 候选刷新端点。顺序即优先级；全部 404 / 405 视为「上游没有这个协议」。
WB_REFRESH_PATHS = ("/v2/plugin/auth/token/refresh", "/v2/plugin/auth/refresh")
TRAE_REFRESH_PATH = "/trae/api/v3/oauth/RefreshToken"

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
    platform = "trae"
    paths = (TRAE_REFRESH_PATH,)

    def _base(self, credential: dict[str, Any]) -> str:
        _jwt, _device_id, host = trae_credentials(credential)
        return host or TRAE_ACCOUNT_BASE

    def _headers(self, credential: dict[str, Any]) -> dict[str, str]:
        _jwt, device_id, _host = trae_credentials(credential)
        return {
            "accept": "*/*",
            "content-type": "application/json",
            "x-device-id": device_id,
            "user-agent": "VSCode 1.107.1 (TRAE CN)",
        }

    def _bodies(self, credential: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        from chaierww.domain.trae_oauth import CLIENT_ID, PLATFORM_CODE

        token = str(credential.get("refreshToken") or credential.get("refresh_token") or "")
        _jwt, device_id, _host = trae_credentials(credential)
        return (
            {
                "ClientID": CLIENT_ID,
                "RefreshToken": token,
                "DeviceInfo": {
                    "DeviceID": device_id,
                    "PlatformCode": PLATFORM_CODE,
                    "DeviceType": "PC",
                },
            },
            {"ClientID": CLIENT_ID, "RefreshToken": token, "PlatformCode": PLATFORM_CODE},
            {"refresh_token": token, "grant_type": "refresh_token"},
        )


def refresher_for(platform: str) -> _Refresher | None:
    """平台名 → 续期器。未知平台返回 None（`unsupported`，不抛错）。"""
    if platform == "workbuddy":
        return WorkBuddyRefresher()
    if platform == "trae":
        return TraeRefresher()
    return None
