"""本机 `/api/*` 访问守卫与安全响应头。

威胁模型（详见 `SECURITY.md`）：
- 拦截**浏览器跨站**请求、DNS 重绑定、以及没有本机会话的直接调用；
- **不**拦截同一用户下的本机进程 —— 能读 `~/.chaierww/api-token` 的进程
  本来就能读整个数据目录，鉴权在这里只负责把「不需要授权的网页」挡在外面。

浏览器/桌面端通过同源 Cookie 自动放行，用户不需要登录；外部脚本用
`~/.chaierww/api-token` 的 Bearer 令牌调用。
"""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
from hashlib import sha256
from hmac import compare_digest
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from chaierww.config import Settings

logger = logging.getLogger("chaierww")

TOKEN_COOKIE = "chaierww_api"
CLIENT_HEADER = "X-Chaierww-Client"
CLIENT_VALUE = "web"
#: 公开路径：健康检查与「领取会话」本身不能要求已持有会话。
EXEMPT_PATHS = {"/api/health", "/api/auth/session"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def load_or_create_token(path: Path) -> str:
    """读取（或首次生成）本机 API 令牌。

    文件权限尽力设为 0600；Windows 上实际依赖用户目录 ACL。已有文件长度
    不足 32 视为损坏，重新生成（旧令牌随即失效）。
    """
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # 并发启动时另一个进程先创建了；用它的令牌即可。
        existing = path.read_text(encoding="utf-8").strip()
        if len(existing) >= 32:
            return existing
        path.write_text(token, encoding="utf-8")
        return token
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token)
    finally:
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    return token


def _host_only(value: str | None) -> str:
    """Host / Origin 归一化：去端口、去方括号，统一小写。"""
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if raw.startswith("["):
        end = raw.find("]")
        return raw[: end + 1] if end != -1 else raw
    return raw.split(":", 1)[0]


def _origin_allowed(origin: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return _host_only(parsed.hostname) in LOOPBACK_HOSTS


def _deny(status: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail})


def install_api_guard(app: FastAPI, settings: Settings) -> str | None:
    """装顺序守卫；返回令牌（关闭守卫时返回 ``None``）。

    必须在 ``install_security_headers`` **之前**调用，这样 401/403 也带安全头。
    """
    if not settings.api_guard:
        return None
    token = load_or_create_token(settings.api_token_path)
    app.state.api_token = token
    token_digest = sha256(token.encode("utf-8")).digest()

    @app.middleware("http")
    async def api_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        host = _host_only(request.headers.get("host"))
        if host not in LOOPBACK_HOSTS:
            return _deny(403, "仅允许本机访问")

        origin = request.headers.get("origin")
        if origin and not _origin_allowed(origin):
            return _deny(403, "来源不被允许")

        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return _deny(403, "来源不被允许")

        path = request.url.path
        if path.startswith("/api/") and path not in EXEMPT_PATHS:
            cookie = request.cookies.get(TOKEN_COOKIE, "")
            header = request.headers.get("authorization", "")
            bearer = header[7:].strip() if header.lower().startswith("bearer ") else ""
            configured = request.app.state.api_token
            cookie_ok = bool(cookie) and compare_digest(cookie, configured)
            bearer_ok = bool(bearer) and compare_digest(
                sha256(bearer.encode("utf-8")).digest(), token_digest
            )
            if not (cookie_ok or bearer_ok):
                return _deny(401, "需要本机访问令牌")

        # 反 CSRF：即使 Cookie 被同站页面带上，也必须显式声明是网页客户端。
        if (
            request.method.upper() in UNSAFE_METHODS
            and request.headers.get(CLIENT_HEADER, "").strip() != CLIENT_VALUE
        ):
            return _deny(403, "缺少本机客户端标识")

        return await call_next(request)

    return token


def install_security_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def security_headers(request: Request, call_next):  # type: ignore[no-untyped-def]
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        return response


def build_auth_router(settings: Settings, token: str | None) -> APIRouter:
    router = APIRouter(tags=["认证"])

    @router.get("/api/auth/session", summary="领取本机 API 会话")
    def auth_session(response: Response) -> dict[str, bool]:
        if not settings.api_guard or token is None:
            return {"guard": False}
        response.set_cookie(
            TOKEN_COOKIE,
            token,
            httponly=True,
            samesite="strict",
            path="/",
            max_age=30 * 24 * 3600,
        )
        return {"guard": True}

    return router
