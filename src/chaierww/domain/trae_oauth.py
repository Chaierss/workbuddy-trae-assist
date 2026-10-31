"""Trae 扫码登录（浏览器授权 + 本地回调监听）。

协议见 upstream 的 ``oauth.rs``——https://github.com/NextAgentX/qoder-trae-workbuddy-switch
（Rust/Tauri 实现，与本项目同一许可，Required Notice 见仓库根 LICENSE）：

1. 从 ``storage.json`` 解出设备身份（device_id / machine_id / publicKeyPEM），
   ``device_id`` 必须与授权 URL、``DeviceInfo.DeviceID``、``x-device-id`` **三方同源**，
   否则上游回 ``20403/040036 Token device not match``。
2. PKCE S256：verifier（64 hex）+ challenge = base64url-nopad(SHA256(verifier))。
3. 构造 22 参数授权 URL，回调地址 = ``http://127.0.0.1:17388/authorize``。
4. 本地监听**固定端口 17388**：授权页在「认证中」会从浏览器侧探这个端口判断客户端在线，
   探到才走完授权；探不到就永远卡在「认证中」。收到**不带凭据标记**的请求（探测）回
   200 + CORS 头后继续等。
5. 回调带 ``authCodeInfo``（URL 编码 JSON，含 AuthCode）+ ``userInfo`` + ``host`` +
   ``loginTraceID``；校验 CSRF 后用 AuthCode + code_verifier 调
   ``${host}/trae/api/v3/oauth/ExchangeToken`` 换 access/refresh token。

登录会话放在进程内存（扫码是短时交互）。PKCE verifier 只存内存，不落盘、不进日志。

结果账号形态：JWT（access token）+ refresh token，uid 从 JWT 的 ``data.id`` 解析。
与 WorkBuddy 的「oauth-token」账号同构，切换时由 Trae 适配器按需处理。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

import httpx

from chaierww.domain.trae_crypto import (
    DeviceIdentity,
    TraeCryptoError,
    device_identity_from_storage,
)

# ---- 端点与契约常量（对照旧项目 mod.rs / oauth.rs）----

CONSOLE_BASE = "https://www.trae.cn"
ACCOUNT_BASE = "https://api.trae.cn"
ICUBE_BASE = "https://api.trae.com.cn"

AUTHORIZE_PATH = "/authorization"
CALLBACK_PATH = "/authorize"
CALLBACK_PORT = 17388

CLIENT_ID = "ono9krqynydwx5"  # TRAE 产品线
APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
PLATFORM_CODE = "IDE_PC"

EXCHANGE_TOKEN_PATH = "/trae/api/v3/oauth/ExchangeToken"
EXCHANGE_TOKEN_LEGACY_PATH = "/cloudide/api/v3/trae/oauth/ExchangeToken"

TIMEOUT_SECONDS = 300

# 回调「凭据标记」：带任一参数即视为真回调，否则是授权页的在线探测。
CREDENTIAL_MARKERS = (
    "authCodeInfo",
    "code",
    "accessToken",
    "access_token",
    "refreshToken",
    "refresh_token",
)

#: 换 token 响应里 access / refresh 的候选键。大小写不敏感匹配（见 ``_find_token_value``）。
ACCESS_TOKEN_KEYS = ("AccessToken", "access_token", "token", "Jwt", "JWT")
REFRESH_TOKEN_KEYS = ("RefreshToken", "refresh_token")


class TraeOAuthError(RuntimeError):
    """Trae 扫码登录的可预期失败。消息面向用户。"""


def _random_hex(length: int) -> str:
    return secrets.token_hex(length // 2)


def _pkce_pair() -> tuple[str, str]:
    """返回 ``(verifier, challenge)``。challenge = base64url-nopad(SHA256(verifier))。"""
    verifier = _random_hex(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_authorize_url(
    *,
    device_id: str,
    machine_id: str,
    trace_id: str,
    code_challenge: str,
    port: int = CALLBACK_PORT,
) -> str:
    """构造 22 参数授权 URL（TRAE 线，逐字对齐抓包固化值）。

    四个描述性系统字段（device_brand/device_type/os_version）留空 —— 它们在授权 URL 与
    请求体里取同一个值，留空不会引发「两处不一致」的 20403（对照旧项目 platform.rs）。
    """
    from urllib.parse import quote

    callback = quote(f"http://127.0.0.1:{port}{CALLBACK_PATH}", safe="")
    return (
        f"{CONSOLE_BASE}{AUTHORIZE_PATH}?"
        "login_version=1"
        "&auth_from=trae"
        "&login_channel=native_ide"
        "&plugin_version=2.3.83560"
        "&auth_type=local"
        f"&client_id={CLIENT_ID}"
        "&redirect=0"
        f"&login_trace_id={trace_id}"
        f"&auth_callback_url={callback}"
        f"&machine_id={quote(machine_id, safe='')}"
        f"&device_id={quote(device_id, safe='')}"
        f"&x_device_id={quote(device_id, safe='')}"
        f"&x_machine_id={quote(machine_id, safe='')}"
        "&x_device_brand="
        "&x_device_type=windows"
        "&x_os_version="
        "&x_env="
        "&x_app_version=3.3.100"
        "&x_app_type=stable"
        f"&code_challenge={code_challenge}"
        "&code_challenge_method=S256"
        "&channel_name=common"
    )


def _parse_query(raw_query: str) -> dict[str, str]:
    """把回调 URL 的查询串解析成键值对。

    先按 ``&`` 切、再逐段解码、最后按第一个 ``=`` 切键值 —— 顺序不可调：
    JWT / authCodeInfo 的值里合法含有 ``=``，按所有 ``=`` 切会截断凭据。
    """
    out: dict[str, str] = {}
    for pair in raw_query.split("&"):
        if not pair:
            continue
        decoded = pair
        for _ in range(3):
            from urllib.parse import unquote

            nxt = unquote(decoded)
            if nxt == decoded:
                break
            decoded = nxt
        if "=" in decoded:
            key, value = decoded.split("=", 1)
        else:
            key, value = decoded, ""
        out[key] = value
    return out


@dataclass
class TraeOAuthSession:
    login_id: str
    trace_id: str
    pkce_verifier: str
    identity: DeviceIdentity
    expires_at: float
    port: int = CALLBACK_PORT
    done: bool = False
    result: dict[str, Any] | None = None
    error: str | None = None
    # 回调监听线程句柄（用于关闭端口）。
    server: Any = None
    thread: Any = None
    # 线程间传递回调结果的通道。
    callback_event: Any = field(default_factory=threading.Event)
    callback_params: dict[str, str] | None = None


def _find_token_value(body: Any, keys: tuple[str, ...]) -> str | None:
    """在 JSON 树中递归查找指定键（**大小写不敏感**）的首个非空字符串值。

    先查 ``Result`` / ``data`` / ``Data`` 容器，再全树深挖：上游不同版本对响应的
    包裹层并不统一（2026-10-30 新版实测为 ``{"Result": {"Token": ...}}``，键名首字母
    大写），按精确键名匹配会把「换了个包裹层」表现成「响应里没有 Token」。
    """

    def search(value: Any) -> str | None:
        if isinstance(value, dict):
            for key in keys:
                raw = value.get(key)
                if isinstance(raw, str) and raw.strip():
                    return raw.strip()
            for actual, raw in value.items():
                if (
                    any(str(actual).lower() == key.lower() for key in keys)
                    and isinstance(raw, str)
                    and raw.strip()
                ):
                    return raw.strip()
            for child in value.values():
                found = search(child)
                if found:
                    return found
        elif isinstance(value, list):
            for item in value:
                found = search(item)
                if found:
                    return found
        return None

    if isinstance(body, dict):
        for wrapper in ("Result", "data", "Data"):
            container = body.get(wrapper)
            if container is not None:
                found = search(container)
                if found:
                    return found
    return search(body)


def _key_paths(body: Any) -> str:
    """收集 JSON 的键路径（只有键名、不含值），用于「找不到 Token」时的诊断。"""
    paths: list[str] = []

    def walk(value: Any, prefix: str, depth: int) -> None:
        if depth > 6 or len(paths) >= 40:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                paths.append(path)
                walk(child, path, depth + 1)
        elif isinstance(value, list):
            for index, child in enumerate(value[:3]):
                walk(child, f"{prefix}[{index}]", depth + 1)

    walk(body, "", 0)
    return " | ".join(paths)


def _dig_string(body: Any, path: list[str]) -> str | None:
    current = body
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    if isinstance(current, str):
        return current
    if isinstance(current, int):
        return str(current)
    return None


def _parse_jwt_uid(jwt: str) -> str | None:
    """从 JWT payload 的 ``data.id`` 解析 uid（对照旧项目 jwt.rs）。"""
    try:
        parts = jwt.split(".")
        if len(parts) < 2:
            return None
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        raw = base64.urlsafe_b64decode(payload + padding)
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return None
    data_block = data.get("data")
    if isinstance(data_block, dict):
        uid = data_block.get("id")
    else:
        uid = data.get("id") or data.get("userId")
    return str(uid) if uid is not None else None


class TraeOAuth:
    """Trae 扫码登录：发起（占端口 + 起监听）、轮询、取消。"""

    def __init__(self) -> None:
        self._sessions: dict[str, TraeOAuthSession] = {}
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=15.0)

    # ---------- 发起 ----------

    def start(self, storage: dict) -> dict[str, Any]:
        """发起登录。``storage`` 是 ``storage.json`` 解析出的对象（设备身份来源）。

        返回 ``{ loginId, verificationUri, expiresIn, port }``。
        设备身份取不到时直接失败（不做任何降级，见旧项目红线）。
        """
        try:
            identity = device_identity_from_storage(storage)
        except TraeCryptoError as exc:
            raise TraeOAuthError(str(exc)) from exc

        trace_id = _random_hex(32)
        pkce_verifier, code_challenge = _pkce_pair()
        login_id = f"trae_{uuid.uuid4().hex}"

        # 占固定端口：授权页会来探这个端口判断客户端在线。
        server, thread = self._start_listener(login_id)

        url = build_authorize_url(
            device_id=identity.device_id,
            machine_id=identity.machine_id,
            trace_id=trace_id,
            code_challenge=code_challenge,
        )

        with self._lock:
            self._sessions[login_id] = TraeOAuthSession(
                login_id=login_id,
                trace_id=trace_id,
                pkce_verifier=pkce_verifier,
                identity=identity,
                expires_at=time.time() + TIMEOUT_SECONDS,
                server=server,
                thread=thread,
            )

        return {
            "loginId": login_id,
            "verificationUri": url,
            "expiresIn": TIMEOUT_SECONDS,
            "port": CALLBACK_PORT,
        }

    def _start_listener(self, login_id: str) -> tuple[Any, threading.Thread]:
        """在 127.0.0.1:17388 起一个回调监听，随会话生命周期运行。"""
        oauth = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                params = _parse_query(parsed.query or "")
                if parsed.path == CALLBACK_PATH and any(
                    key in params for key in CREDENTIAL_MARKERS
                ):
                    # 真回调：把参数交给会话，回一张成功卡片。
                    oauth._on_callback(login_id, params)
                    body = b"<!doctype html><html><body>ok</body></html>"
                else:
                    # 在线探测：回 200 + CORS 头继续等。
                    body = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(200)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: Any) -> None:  # noqa: D102
                pass

        try:
            server = ThreadingHTTPServer(("127.0.0.1", CALLBACK_PORT), Handler)
        except OSError as exc:
            raise TraeOAuthError(
                f"本地回调端口 {CALLBACK_PORT} 无法监听：{exc}。"
                "该端口是 Trae 授权页确认「客户端在线」的固定端口，必须可用；"
                "请关闭占用它的程序（通常是 Trae 客户端自身或上一次未退出的登录会话）后重试。"
            ) from exc

        thread = threading.Thread(
            target=server.serve_forever, name=f"trae-oauth-{login_id}", daemon=True
        )
        thread.start()
        return server, thread

    def _on_callback(self, login_id: str, params: dict[str, str]) -> None:
        with self._lock:
            session = self._sessions.get(login_id)
            if session is None:
                return
            session.callback_params = params
            session.callback_event.set()

    # ---------- 轮询 ----------

    def poll(self, login_id: str) -> dict[str, Any]:
        """轮询一次。成功则完成 ExchangeToken 换 token 并返回账号信息。"""
        with self._lock:
            session = self._sessions.get(login_id)
            if session is None:
                return {"done": True, "error": "登录请求不存在或已过期"}
            if session.done:
                return {"done": True, "result": session.result, "error": session.error}
            if time.time() > session.expires_at:
                session.done = True
                session.error = "登录超时"
                self._release(session)
                return {"done": True, "error": "登录超时"}
            if session.callback_params is None:
                return {"done": False}

            # 拿到回调，摘出参数后立刻释放锁，网络交换在外面做。
            params = session.callback_params
            session.callback_params = None
            trace_id = session.trace_id
            pkce_verifier = session.pkce_verifier
            identity = session.identity

        try:
            account = self._complete(session, params, trace_id, pkce_verifier, identity)
        except TraeOAuthError as exc:
            with self._lock:
                session.done = True
                session.error = str(exc)
                self._release(session)
            return {"done": True, "error": str(exc)}

        with self._lock:
            session.done = True
            session.result = account
            self._release(session)
        return {"done": True, "result": account}

    def _complete(
        self,
        session: TraeOAuthSession,
        params: dict[str, str],
        trace_id: str,
        pkce_verifier: str,
        identity: DeviceIdentity,
    ) -> dict[str, Any]:
        """走完「回调解析 → CSRF 校验 → ExchangeToken → 落库视图」。"""
        # CSRF 校验：回调 loginTraceID 必须与发起的 trace_id 相等。
        callback_trace = params.get("loginTraceID") or params.get("login_trace_id")
        if callback_trace != trace_id:
            raise TraeOAuthError(
                "回调与本机发起的登录不匹配（loginTraceID 校验失败），"
                "可能为伪造或重放，请重新发起登录。"
            )

        # authCodeInfo（主路径）：URL 编码 JSON，含 AuthCode。
        auth_code: str | None = None
        raw_auth_info = params.get("authCodeInfo")
        if raw_auth_info:
            try:
                parsed = json.loads(raw_auth_info.strip())
            except json.JSONDecodeError as exc:
                raise TraeOAuthError(
                    f"authCodeInfo 解析失败（{exc}）：授权页回调格式异常"
                ) from exc
            auth_code = (parsed.get("AuthCode") or "").strip() or None
        if not auth_code:
            auth_code = params.get("code") or None
        if not auth_code:
            raise TraeOAuthError("回调字段缺失（无 AuthCode）；请重新发起登录。")

        # userInfo（展示名兜底）
        display_name: str | None = None
        raw_user_info = params.get("userInfo") or params.get("user_info")
        if raw_user_info:
            try:
                user_info = json.loads(raw_user_info.strip())
                if isinstance(user_info, dict):
                    for key in ("ScreenName", "NickName", "nickname", "name", "email"):
                        if user_info.get(key):
                            display_name = str(user_info[key]).strip()
                            break
            except json.JSONDecodeError:
                pass

        # host：回调回传优先，缺失回落 account_base。
        host = (params.get("host") or "").strip().rstrip("/") or ACCOUNT_BASE

        jwt, refresh_token = self._exchange_auth_code(
            auth_code, pkce_verifier, identity, host
        )
        uid = _parse_jwt_uid(jwt)
        if not uid:
            raise TraeOAuthError("无法从新令牌解析 UserID，已拒绝写库")

        return {
            "uid": uid,
            "displayName": display_name or f"账号{uid[-6:]}",
            "accessToken": jwt,
            "refreshToken": refresh_token,
            "host": host,
            "deviceId": identity.device_id,
        }

    def _exchange_auth_code(
        self,
        auth_code: str,
        code_verifier: str,
        identity: DeviceIdentity,
        host: str,
    ) -> tuple[str, str | None]:
        """用 AuthCode 换 token：主端点（DeviceInfo）+ 旧端点兜底。"""
        variants = [
            (
                "ExchangeToken/DeviceInfo",
                f"{host}{EXCHANGE_TOKEN_PATH}",
                {
                    "ClientID": CLIENT_ID,
                    "AuthCode": auth_code,
                    "CodeVerifier": code_verifier,
                    "DeviceInfo": {
                        "DeviceID": identity.device_id,
                        "MachineID": identity.machine_id,
                        "PlatformCode": PLATFORM_CODE,
                        "DeviceType": "PC",
                        "DeviceName": "",
                        "DeviceModel": "",
                        "ClientVersion": "3.3.100",
                        "DevicePublicKey": identity.public_key_pem,
                        "DeviceBrand": "",
                        "DeviceCPU": "",
                        "OSInfo": "windows",
                        "OSVersion": "",
                    },
                    "IDEVersion": "3.3.100",
                },
                True,
            ),
            (
                "ExchangeToken/AuthCode",
                f"{ICUBE_BASE}{EXCHANGE_TOKEN_LEGACY_PATH}",
                {
                    "ClientID": CLIENT_ID,
                    "AuthCode": auth_code,
                    "CodeVerifier": code_verifier,
                    "DeviceID": identity.device_id,
                    "PlatformCode": PLATFORM_CODE,
                },
                False,
            ),
            (
                "ExchangeToken/Code",
                f"{ICUBE_BASE}{EXCHANGE_TOKEN_LEGACY_PATH}",
                {
                    "ClientID": CLIENT_ID,
                    "Code": auth_code,
                    "CodeVerifier": code_verifier,
                    "DeviceID": identity.device_id,
                    "PlatformCode": PLATFORM_CODE,
                },
                False,
            ),
        ]

        errors: list[str] = []
        for tag, url, payload, with_cloudide_token in variants:
            try:
                jwt, refresh = self._try_exchange(
                    tag, url, payload, identity.device_id, with_cloudide_token
                )
                return jwt, refresh
            except TraeOAuthError as exc:
                errors.append(f"{tag}: {exc}")

        raise TraeOAuthError("全部交换变体失败 → " + " | ".join(errors))

    def _try_exchange(
        self,
        tag: str,
        url: str,
        payload: dict[str, Any],
        device_id: str,
        with_cloudide_token: bool,
    ) -> tuple[str, str | None]:
        headers = {
            "content-type": "application/json",
            "accept": "*/*",
            "x-device-id": device_id,
            "x-app-id": APP_ID,
            "x-platform-code": PLATFORM_CODE,
        }
        if with_cloudide_token:
            # 必须存在且为空串：缺失报 20403，带旧 token 报 20405。
            headers["x-cloudide-token"] = ""

        try:
            resp = self._client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise TraeOAuthError(f"请求失败: {exc}") from exc

        try:
            body = resp.json()
        except ValueError as exc:
            raise TraeOAuthError(f"解析响应失败 (HTTP {resp.status_code}): {exc}") from exc

        # 火山引擎标准信封：错误在 ResponseMetadata.Error。
        code = _dig_string(body, ["ResponseMetadata", "Error", "Code"])
        if code is not None and code != "0":
            message = _dig_string(body, ["ResponseMetadata", "Error", "Message"]) or "未知错误"
            standard = _dig_string(body, ["ResponseMetadata", "Error", "StandardCode"]) or ""
            raise TraeOAuthError(f"code={code}/{standard}: {message}")

        if (
            isinstance(body, dict)
            and isinstance(body.get("code"), int)
            and body["code"] != 0
        ):
            message = body.get("message") or "未知错误"
            raise TraeOAuthError(f"code={body['code']}: {message}")

        # 新版把业务错误放在 Data.__Message.error（不在火山引擎信封里）。
        upstream_error = _dig_string(
            body, ["Data", "__Message", "error"]
        ) or _dig_string(body, ["data", "__Message", "error"])
        if upstream_error:
            raise TraeOAuthError(f"上游拒绝: {upstream_error}")

        access = _find_token_value(body, ACCESS_TOKEN_KEYS)
        refresh = _find_token_value(body, REFRESH_TOKEN_KEYS)
        if access and refresh:
            return access, refresh
        if access and not refresh:
            raise TraeOAuthError(
                f"{tag}: 响应缺少 RefreshToken（该账号将无法自动续期，已拒绝写库）"
            )
        raise TraeOAuthError(
            f"{tag}: 响应中未找到 Token 字段（响应键：{_key_paths(body)}）"
        )

    # ---------- 取消 / 清理 ----------

    def cancel(self, login_id: str) -> bool:
        with self._lock:
            session = self._sessions.get(login_id)
            if session is None:
                return False
            session.done = True
            if not session.error:
                session.error = "已取消"
            self._release(session)
            return True

    def _release(self, session: TraeOAuthSession) -> None:
        """关闭回调监听端口。"""
        server = session.server
        if server is not None:
            try:
                server.shutdown()
                server.server_close()
            except Exception:  # noqa: BLE001
                pass
            session.server = None

    def close(self) -> None:
        with self._lock:
            for session in list(self._sessions.values()):
                self._release(session)
            self._sessions.clear()
        self._client.close()


def build_trae_oauth() -> TraeOAuth:
    return TraeOAuth()
