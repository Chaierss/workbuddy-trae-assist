"""WorkBuddy 扫码登录采集（轮询式 OAuth）。

协议（对照旧项目 `modules/oauth.rs`，2026-10-19 实测验证）：

1. 发起：`POST https://www.workbuddy.cn/v2/plugin/auth/state?platform=workbuddy`
   → 返回 `data.state` 与 `data.authUrl`（授权页二维码地址）。
2. 引导：前端打开 `authUrl`（或 `{base}/login?state={state}`）让用户扫码。
3. 轮询：`GET https://www.workbuddy.cn/v2/plugin/auth/token?state={state}`
   → `code` 为 0/200 且带 `accessToken` 即登录成功。
4. 拉账号：`GET .../login/account?state={state}`（Bearer token + X-Domain）
   → uid / nickname / email 等。

登录会话放在进程内存：扫码是短时交互，重启即失效，无需持久化。
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

BASE = "https://www.codebuddy.cn"
API_PREFIX = "/v2/plugin"
PLATFORM = "workbuddy"
TIMEOUT_SECONDS = 600


@dataclass
class OAuthSession:
    state: str
    expires_at: float
    done: bool = False
    result: dict[str, Any] | None = None
    error: str | None = None


class WorkBuddyOAuth:
    """WorkBuddy 扫码登录：发起 / 轮询。线程安全（内存字典 + 锁）。"""

    def __init__(self) -> None:
        self._sessions: dict[str, OAuthSession] = {}
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=15.0)

    def start(self) -> dict[str, Any]:
        """发起登录，返回 loginId / verificationUri / expiresIn。"""
        url = f"{BASE}{API_PREFIX}/auth/state?platform={PLATFORM}"
        resp = self._client.post(url, json={})
        resp.raise_for_status()
        body = resp.json()
        data = body.get("data") or {}
        state = str(data.get("state") or "")
        if not state:
            raise RuntimeError(f"auth/state 响应缺少 state：{str(body)[:300]}")
        auth_url = str(
            data.get("authUrl")
            or data.get("auth_url")
            or data.get("url")
            or f"{BASE}/login?state={state}"
        )

        login_id = f"wb_{uuid.uuid4().hex}"
        with self._lock:
            self._sessions[login_id] = OAuthSession(
                state=state,
                expires_at=time.time() + TIMEOUT_SECONDS,
            )
        return {
            "loginId": login_id,
            "verificationUri": auth_url,
            "expiresIn": TIMEOUT_SECONDS,
        }

    def poll(self, login_id: str) -> dict[str, Any]:
        """轮询一次登录结果。成功则返回账号信息（不含 token 落库，由调用方入库）。"""
        with self._lock:
            session = self._sessions.get(login_id)
            if session is None:
                return {"done": True, "error": "登录请求不存在或已过期"}
            if session.done:
                return {
                    "done": True,
                    "result": session.result,
                    "error": session.error,
                }
            if time.time() > session.expires_at:
                session.done = True
                session.error = "登录超时"
                return {"done": True, "error": "登录超时"}
            state = session.state

        # 轮询 token
        token_url = f"{BASE}{API_PREFIX}/auth/token?state={state}"
        resp = self._client.get(token_url)
        resp.raise_for_status()
        body = resp.json()
        code = body.get("code", -1)
        if code not in (0, 200):
            return {"done": False}
        data = body.get("data") or {}
        access_token = str(
            data.get("accessToken") or data.get("access_token") or ""
        )
        if not access_token:
            return {"done": False}

        # 拉账号信息
        account = self._fetch_account(state, access_token, data)

        with self._lock:
            session = self._sessions.get(login_id)
            if session is not None:
                session.done = True
                session.result = account
        return {"done": True, "result": account}

    def _fetch_account(
        self, state: str, access_token: str, token_data: dict[str, Any]
    ) -> dict[str, Any]:
        account_url = f"{BASE}{API_PREFIX}/login/account?state={state}"
        headers = {"Authorization": f"Bearer {access_token}"}
        domain = str(token_data.get("domain") or "")
        if domain:
            headers["X-Domain"] = domain
        resp = self._client.get(account_url, headers=headers)
        resp.raise_for_status()
        acc_data = (resp.json().get("data") or {}) if isinstance(resp.json(), dict) else {}

        return {
            "uid": acc_data.get("uid"),
            "nickname": acc_data.get("nickname"),
            "email": acc_data.get("email"),
            "enterpriseName": acc_data.get("enterpriseName"),
            "accessToken": access_token,
            "refreshToken": token_data.get("refreshToken")
            or token_data.get("refresh_token"),
            "tokenType": token_data.get("tokenType")
            or token_data.get("token_type")
            or "Bearer",
            "domain": domain,
            "expiresAt": token_data.get("expiresAt") or token_data.get("expires_at"),
            "refreshExpiresAt": token_data.get("refreshExpiresAt")
            or token_data.get("refresh_expires_at"),
        }

    def close(self) -> None:
        self._client.close()


# 进程级单例：路由通过 app.state 注入，此处提供构造。
def build_oauth() -> WorkBuddyOAuth:
    return WorkBuddyOAuth()
