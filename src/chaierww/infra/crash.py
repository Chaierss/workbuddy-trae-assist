"""崩溃与未捕获异常落盘。

三层：
- Python 主线程 ``sys.excepthook``、子线程 ``threading.excepthook``；
- FastAPI 兜底异常处理器 + ``X-Request-Id``；
- 前端通过 ``POST /api/diagnostics/client-error`` 把错误带回后端日志。

落盘文件固定为 ``<home>/logs/crash.log``：只追加、不求轮转（量很小），
内容与运行日志同一套脱敏规则（调用方传入已格式化文本）。
"""

from __future__ import annotations

import logging
import sys
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger("chaierww")


def _append(home: Path, text: str) -> None:
    try:
        path = home / "logs" / "crash.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)
    except OSError:
        # 崩溃日志写不进去时只能退回 logger；绝不能再抛。
        logger.exception("崩溃日志写入失败")


def install_excepthooks(home: Path) -> None:
    previous = sys.excepthook

    def main_hook(exc_type, exc_value, exc_tb):  # type: ignore[no-untyped-def]
        _append(
            home,
            f"\n[{time_stamp()}] 未捕获异常\n"
            + "".join(traceback.format_exception(exc_type, exc_value, exc_tb)),
        )
        previous(exc_type, exc_value, exc_tb)

    sys.excepthook = main_hook

    previous_thread = threading.excepthook

    def thread_hook(args: Any) -> None:
        _append(
            home,
            f"\n[{time_stamp()}] 子线程未捕获异常（{args.thread.name if args.thread else '?'}）\n"
            + "".join(
                traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback
                )
            ),
        )
        previous_thread(args)

    threading.excepthook = thread_hook


def time_stamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(microsecond=0).isoformat()


def install_request_id(app: FastAPI) -> None:
    @app.middleware("http")
    async def request_id(request: Request, call_next):  # type: ignore[no-untyped-def]
        request.state.request_id = uuid.uuid4().hex[:12]
        response = await call_next(request)
        response.headers["X-Request-Id"] = request.state.request_id
        return response


def install_exception_handler(app: FastAPI) -> None:
    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", "-")
        logger.exception(
            "未处理异常 request_id=%s path=%s", request_id, request.url.path
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "服务内部错误", "requestId": request_id},
        )
