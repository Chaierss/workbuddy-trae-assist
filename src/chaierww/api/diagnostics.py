"""诊断相关接口：健康度、诊断包下载、前端错误上报。"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from chaierww.domain.diagnostics import build_health, build_zip

logger = logging.getLogger("chaierww")

router = APIRouter(prefix="/api/diagnostics", tags=["诊断"])

#: 前端错误上报限流：20 次/分钟。这里只防刷屏，不承担安全职责。
CLIENT_ERROR_LIMIT = 20
_client_errors: deque[float] = deque()


class ClientErrorPayload(BaseModel):
    message: str = Field(default="", max_length=1000)
    stack: str = Field(default="", max_length=8000)
    source: str = Field(default="web", max_length=40)
    url: str = Field(default="", max_length=500)


@router.get("/health", summary="运行健康度")
def diagnostics_health(request: Request) -> dict[str, Any]:
    return build_health(request.app)


@router.get("/bundle", summary="下载诊断包（zip）")
def diagnostics_bundle(request: Request) -> Response:
    payload = build_zip(request.app)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="chaierww-diagnostics-{stamp}.zip"'
        },
    )


@router.post("/client-error", summary="前端错误上报")
def client_error(payload: ClientErrorPayload) -> dict[str, bool]:
    now = time.time()
    while _client_errors and now - _client_errors[0] > 60:
        _client_errors.popleft()
    if len(_client_errors) >= CLIENT_ERROR_LIMIT:
        raise HTTPException(status_code=429, detail="上报过于频繁")
    _client_errors.append(now)
    logger.warning(
        "前端错误上报（%s）：%s | %s",
        payload.source,
        payload.message[:300],
        payload.stack.splitlines()[0][:300] if payload.stack else "",
    )
    return {"ok": True}
