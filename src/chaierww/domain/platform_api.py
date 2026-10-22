"""两个平台的协议公共层：凭据解包、请求头、端点选择。

积分查询（`credits.py`）与活动检测（`activities.py`）都要把「账号库里的凭据」
变成「一串能发出去的请求头」。这段逻辑**只写一份**：凭据格式一旦变化
（例如新增一种 `format`），改这里就够了，不会出现两条链各自解包、
其中一条漏改而在界面上静默显示空值。

## 凭据双形态

- `raw-file`：客户端登录态原文（base64）。Trae 的令牌藏在
  `iCubeAuthInfo://icube.cloudide` 的 tc 信封里，pepper 是公开常量表 ⇒ 可解；
  WorkBuddy 的令牌是 `$wbEncrypted`（DPAPI）信封 ⇒ **本工具不解密**，
  抛 `CredentialError` 让调用方诚实报「不可查」。
- `oauth-token`：扫码登录拿到的明文令牌，两个平台都能直接用。

## 端点选择（WorkBuddy）

CN 有两个官方 origin：`www.codebuddy.cn`（API/计费域）与 `www.workbuddy.cn`（官网域）。
令牌是签给哪个域，请求就该发哪个域 —— 域不一致时 `X-Domain` 与令牌不匹配，
网关会直接拒绝。所以按账号里的 `domain` 选，且**只允许**这两个常量之一，
绝不用账号数据拼任意主机。
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any

from chaierww.domain.trae_crypto import TraeCryptoError, tc_decrypt

TRAE_ACCOUNT_BASE = "https://api.trae.cn"
WORKBUDDY_API_ORIGIN = "https://www.codebuddy.cn"
WORKBUDDY_WEB_ORIGIN = "https://www.workbuddy.cn"

TRAE_APP_VERSION = "0.1.45"
TRAE_UA = "VSCode 1.107.1 (TRAE CN)"
TRAE_USER_REGION = "CN"
TRAE_PACKAGE_TYPE = "stable_cn"
TRAE_MARKET_AID = "787976"
TRAE_SDK_TYPE = "3"
TRAE_MARKET_CLIENT_ID = "VSCode 1.107.1"

ICUBE_DC_PREFIX = "iCubeAuthInfo://icube-dc:"
ICUBE_CLOUDIDE_KEY = "iCubeAuthInfo://icube.cloudide"

TIMEOUT_SECONDS = 15.0


class CredentialError(RuntimeError):
    """凭据形态不支持解密、或凭据已损坏。消息面向用户，可以直接展示。"""


def decode_raw_file(credential: dict[str, Any]) -> dict[str, Any]:
    """`raw-file` 凭据 → 客户端登录态 JSON 对象。"""
    if credential.get("encoding") != "base64":
        raise CredentialError("凭据格式不受支持")
    raw = credential.get("data")
    if not isinstance(raw, str):
        raise CredentialError("凭据内容损坏")
    try:
        blob = base64.b64decode(raw.encode("ascii"), validate=True)
        data = json.loads(blob.decode("utf-8", "replace"))
    except (ValueError, json.JSONDecodeError) as exc:
        raise CredentialError("凭据内容损坏，无法解析") from exc
    if not isinstance(data, dict):
        raise CredentialError("凭据结构异常")
    return data


def derive_device_id(seed: str) -> str:
    """给拿不到真实设备号的账号派生一个稳定的 16 位设备号。

    Trae 的签到接口把 ``x-device-id`` 当作必填项：缺了直接返回 ``9004``
    （"order parameters are incorrect"），而「每设备每天只能签一次」的限制
    也是按这个头判定的（重复签到返回 ``9095``）。扫码登录拿到的凭据里
    没有设备号，如果原样发空串，签到必然全部失败。

    形态取「6 个 0 + 10 位数字」：实测这一形态（``0000000000000001``、
    ``0000000000000099`` 等）能稳定通过校验，而全随机的 16 位数有一半会被
    拒成 ``9074``（上游把不认识的设备号也归到「参与用户太多」这个笼统错误里）。

    派生保证两件事：**同一账号永远得到同一个值**（重启、换网络都不变），
    **不同账号必然得到不同的值**（避免互相挤掉当天的名额）。
    """
    digest = hashlib.sha256(f"chaierww/{seed}".encode("utf-8")).hexdigest()
    return f"000000{int(digest[:16], 16) % 10**10:010d}"


def trae_credentials(credential: dict[str, Any]) -> tuple[str, str, str]:
    """返回 ``(jwt, device_id, host)``。

    ``device_id`` 保证非空：优先用凭据里的真实设备号（从客户端导入时带过来），
    没有则按账号标识派生，见 :func:`derive_device_id`。
    """
    if credential.get("format") == "oauth-token":
        jwt = str(credential.get("accessToken") or "").strip()
        if not jwt:
            raise CredentialError("扫码账号缺少令牌，请重新登录")
        device_id = str(credential.get("deviceId") or "").strip()
        if not device_id:
            device_id = derive_device_id(str(credential.get("uid") or "").strip() or jwt)
        return (
            jwt,
            device_id,
            str(credential.get("host") or TRAE_ACCOUNT_BASE),
        )

    data = decode_raw_file(credential)
    device_id = ""
    for key in data:
        if str(key).startswith(ICUBE_DC_PREFIX):
            device_id = str(key)[len(ICUBE_DC_PREFIX):]
            break

    envelope = data.get(ICUBE_CLOUDIDE_KEY)
    if not isinstance(envelope, str) or not envelope:
        raise CredentialError("登录态里没有 Trae 令牌（iCubeAuthInfo://icube.cloudide）")
    try:
        info = json.loads(tc_decrypt(envelope))
    except (TraeCryptoError, json.JSONDecodeError) as exc:
        raise CredentialError(str(exc)) from exc
    if not isinstance(info, dict):
        raise CredentialError("Trae 令牌结构异常")

    jwt = str(info.get("token") or "").strip()
    if not jwt:
        raise CredentialError("Trae 令牌为空，请重新登录客户端")
    if not device_id:
        device_id = derive_device_id(str(info.get("userId") or "").strip() or jwt)
    host = str(info.get("host") or TRAE_ACCOUNT_BASE).strip().rstrip("/")
    return jwt, device_id, host


def workbuddy_credentials(credential: dict[str, Any]) -> tuple[str, str, str]:
    """返回 ``(access_token, uid, domain)``。"""
    if credential.get("format") == "oauth-token":
        token = str(credential.get("accessToken") or "").strip()
        if not token:
            raise CredentialError("扫码账号缺少令牌，请重新登录")
        return (
            token,
            str(credential.get("uid") or ""),
            str(credential.get("domain") or ""),
        )

    data = decode_raw_file(credential)
    auth = data.get("auth")
    if not isinstance(auth, dict):
        raise CredentialError("登录态里没有 auth 段")
    token = auth.get("accessToken")
    if not isinstance(token, str) or not token.strip():
        # 加密信封：本工具不解密 WorkBuddy 的 DPAPI 信封。
        raise CredentialError(
            "该账号凭据是客户端加密信封，查不了积分与活动；用「扫码登录」添加的账号可查"
        )
    account = data.get("account") if isinstance(data.get("account"), dict) else {}
    return (
        token.strip(),
        str(account.get("uid") or ""),
        str(auth.get("domain") or ""),
    )


def workbuddy_origin(domain: str) -> str:
    """返回计费/签到/成长等 API 接口的统一 origin。

    实测（2026-10-19）：WorkBuddy 的计费、签到、成长任务、连签、抽奖、用量等
    接口都要发到 `www.codebuddy.cn`（API 域）才通；扫码 token 即便 `domain`
    字段是 `www.workbuddy.cn`，拿去 codebuddy.cn 同样 200。发到 workbuddy.cn
    反而对非 Web 客户端返回 403 code:10085。

    所以这里**不再按 token 签发域选**，统一走 API 域。只有「领奖」这类
    Web 域专属操作才需要 workbuddy.cn（本工具当前未实现领奖）。
    """
    return WORKBUDDY_API_ORIGIN


def trae_headers(jwt: str, device_id: str) -> dict[str, str]:
    """Trae 请求头。``authorization`` 必须是 ``Cloud-IDE-JWT <jwt>`` 前缀形态。"""
    token = jwt.strip()
    if not token.startswith("Cloud-IDE-JWT "):
        token = f"Cloud-IDE-JWT {token}"
    return {
        "accept": "*/*",
        "accept-language": "zh-CN",
        "authorization": token,
        "content-type": "application/json",
        "user-agent": TRAE_UA,
        "x-market-client-id": TRAE_MARKET_CLIENT_ID,
        "x-market-user-id": "",
        "x-user-region": TRAE_USER_REGION,
        "x-device-id": device_id,
        "x-lgw-req-sdk-type": TRAE_SDK_TYPE,
        "package-type": TRAE_PACKAGE_TYPE,
        "x-lscbd-aid": TRAE_MARKET_AID,
        "x-lscbd-platform": "windows",
        "app-version": TRAE_APP_VERSION,
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "no-cors",
        "sec-fetch-site": "none",
    }


def workbuddy_headers(token: str, uid: str, domain: str) -> dict[str, str]:
    """WorkBuddy 请求头。缺了指纹头会被网关 WAF 当成「未知客户端」拦截
    （code 10085，不是 token 失效）——这些头是 403→200 的关键（实测）。

    ``X-User-Id`` / ``X-Domain`` 缺失会被当成匿名请求；`X-Client-Platform` /
    `Origin` / `Referer` / `User-Agent` 是用户中心 Axios 拦截器恒带的指纹。
    """
    origin = WORKBUDDY_API_ORIGIN
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Client-Platform": "web",
        "Origin": origin,
        "Referer": f"{origin}/profile/plans-usage",
        "User-Agent": "CLI/2.63.2 CodeBuddy/2.63.2",
    }
    if uid:
        headers["X-User-Id"] = uid
    if domain:
        headers["X-Domain"] = domain
    return headers
