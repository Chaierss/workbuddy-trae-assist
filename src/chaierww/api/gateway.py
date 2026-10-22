"""网关对外接口：`/api/gateway/*`（界面用）与 `/v1/*`（兼容协议，客户端用）。

`/v1/*` 只认 `Authorization: Bearer sk-...`（Anthropic 客户端常用的
`x-api-key` 也接受），且只在网关真正监听时才工作（未启用返回 503，
而不是把前端的 HTML 兜底页丢给客户端）。

三条协议**收敛在 `_relay_request` 这一个入口**：校验 Key → 解析模型名
（上游原名，如 `deepseek-v4.1-flash`）→ 协议可用性与请求体转换 → 按号池挑号 →
用该账号的令牌请求 `copilot.tencent.com/v2/chat/completions` → 流式回传。
上游 401/403/429/5xx 会换下一个账号重试，最多 `maxHops` 次。

- `POST /v1/chat/completions`：上游原生形状，字节级透传。
- `POST /v1/messages`：上游原生 Anthropic 路由，**原样直转**（只改写模型名）。
- `POST /v1/responses`：上游没有这条路由，由网关按 chat 合成（含 SSE 事件映射）。
- 上游不提供某协议时返回 **501 + 明确文案**，不静默降级到 chat。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from chaierww.domain.accounts.models import Account
from chaierww.domain.gateway import (
    SCOPE_ALL,
    SCOPE_CAPABILITY,
    SCOPE_LIST,
    ApiKeyRecord,
    GatewayConfig,
    GatewayError,
    GatewayKeyNotFound,
    GatewayRuntime,
    GatewayStore,
)
from chaierww.domain.gateway_relay import (
    DEFAULT_SCOPE,
    PROTOCOL_CHAT,
    PROTOCOL_MESSAGES,
    PROTOCOL_RESPONSES,
    CatalogModel,
    GatewayRelay,
    GatewayRouteError,
    GatewayUpstreamError,
    ProtocolNotSupported,
    ResponsesStream,
    aggregate_sse,
    chat_completion_to_response,
    iter_sse_json,
    parse_catalog,
    prepare_relay,
    resolve_model,
    usage_tokens,
)
from chaierww.domain.platform_api import CredentialError

logger = logging.getLogger("chaierww")

api_router = APIRouter(prefix="/api/gateway", tags=["网关"])
v1_router = APIRouter(prefix="/v1", tags=["网关"])

CONNECT_TIMEOUT = 10.0
READ_TIMEOUT = 600.0
TAIL_BYTES = 8192


# ---------- 请求体 ----------


class GatewayConfigRequest(BaseModel):
    enabled: bool | None = None
    host: str | None = None
    port: int | None = None
    allowNonLoopback: bool | None = None
    maxHops: int | None = None
    #: 模型目录自动刷新间隔（分钟）；0 = 关闭。
    catalogRefreshMinutes: int | None = None


class GatewayKeyRequest(BaseModel):
    label: str = ""
    #: 模型范围：`all`（全部）/ `capability`（按能力）/ `list`（指定白名单）。
    #: 不传时按老语义推断：给了 `models` 当 `list`，否则当 `all`。
    scope: str | None = None
    #: `capability` 模式下选中的能力键（`free` / `image` / `long_context`）。
    capabilities: list[str] | None = None
    #: `list` 模式下的静态白名单；None / 空列表 = 空名单。
    models: list[str] | None = None


class GatewayKeyUpdateRequest(BaseModel):
    """改一把已有 Key 的模型范围 / 备注；`None` = 保持原样。"""

    label: str | None = None
    scope: str | None = None
    capabilities: list[str] | None = None
    models: list[str] | None = None


# ---------- 访问器 ----------


def _store(request: Request) -> GatewayStore:
    return request.app.state.gateway


def _relay(request: Request) -> GatewayRelay:
    return request.app.state.gateway_relay


def _runtime(request: Request) -> GatewayRuntime:
    return request.app.state.gateway_runtime


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=CONNECT_TIMEOUT, read=READ_TIMEOUT, write=60.0, pool=CONNECT_TIMEOUT
    )


def _openai_error(status: int, message: str, *, code: str = "gateway_error") -> JSONResponse:
    """OpenAI 风格的错误体：客户端（SDK）按这个结构报错，不能给 FastAPI 的 `detail`。"""
    return JSONResponse(
        status_code=status,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error" if status < 500 else "api_error",
                "code": code,
            }
        },
    )


def _authenticate(request: Request) -> ApiKeyRecord | None:
    header = request.headers.get("authorization") or ""
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if not token:
        token = (request.headers.get("x-api-key") or "").strip()
    return _store(request).authenticate(token)


def _protocol_error(protocol: str, status: int, message: str, *, code: str = "") -> JSONResponse:
    """错误体按入站协议各自的形状给：Anthropic 客户端只认它自己那套结构。"""
    if protocol == PROTOCOL_MESSAGES:
        return JSONResponse(
            status_code=status,
            content={
                "type": "error",
                "error": {
                    "type": "invalid_request_error" if status < 500 else "api_error",
                    "message": message,
                },
            },
        )
    return _openai_error(status, message, code=code or "gateway_error")


async def _json_body(request: Request) -> tuple[dict[str, Any] | None, str]:
    """读请求体：返回 `(对象, 错误文案)`；成功时错误文案为空串。"""
    try:
        body = await request.json()
    except ValueError:
        return None, "请求体不是合法 JSON"
    if not isinstance(body, dict):
        return None, "请求体必须是 JSON 对象"
    return body, ""


def _retryable(status: int) -> bool:
    return status in (401, 403, 408, 429) or status >= 500


# ---------- 界面接口 ----------


@api_router.get("/status", summary="网关状态：配置 / 运行 / 计数 / 模型目录")
def gateway_status(request: Request) -> dict[str, Any]:
    store, relay = _store(request), _relay(request)
    scope = DEFAULT_SCOPE
    catalog = relay.any_catalog()
    return {
        **store.config().payload(),
        "running": _runtime(request).running,
        "error": _runtime(request).error,
        "activeKeys": store.active_key_count(),
        "accounts": len(relay.candidates(scope)),
        "stats": {**store.stats(), "breakdown": store.breakdown()},
        "catalog": {
            "models": [model.payload() for model in (catalog or [])],
            **relay.catalog_meta(),
        },
    }


@api_router.put("/config", summary="修改网关配置（改完立即生效，按下开关会起停网关端口）")
def update_config(request: Request, body: GatewayConfigRequest) -> dict[str, Any]:
    store = _store(request)
    current = store.config()

    def pick(value: Any, fallback: Any) -> Any:
        return fallback if value is None else value

    updated = GatewayConfig(
        enabled=pick(body.enabled, current.enabled),
        host=str(pick(body.host, current.host)).strip(),
        port=int(pick(body.port, current.port)),
        allow_non_loopback=pick(body.allowNonLoopback, current.allow_non_loopback),
        max_hops=int(pick(body.maxHops, current.max_hops)),
        catalog_refresh_minutes=int(
            pick(body.catalogRefreshMinutes, current.catalog_refresh_minutes)
        ),
    )
    try:
        store.save_config(updated)
    except GatewayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="配置值不合法") from exc

    relay, runtime = _relay(request), _runtime(request)
    runtime.apply(lambda: build_gateway_app(store, relay, runtime))
    return gateway_status(request)


@api_router.get("/keys", summary="列出网关 API Key（只返回掩码，永不回明文）")
def list_keys(request: Request) -> dict[str, Any]:
    records = _store(request).keys()
    return {"keys": [record.payload() for record in records]}


@api_router.post("/keys", summary="签发一把网关 API Key（可限制可用模型，明文只返回这一次）")
def create_key(request: Request, body: GatewayKeyRequest) -> dict[str, Any]:
    # 老客户端只传 `models`：按老语义推断成 `list`，行为不变。
    scope = body.scope or (SCOPE_LIST if body.models else SCOPE_ALL)
    try:
        token, record = _store(request).issue(
            body.label,
            body.models,
            scope=scope,
            capabilities=body.capabilities,
        )
    except GatewayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"key": token, "record": record.payload()}


@api_router.put(
    "/keys/{key_id}",
    summary="改一把网关 API Key 的备注 / 模型范围（Key 与明文不变，无需重签）",
)
def update_key(request: Request, key_id: str, body: GatewayKeyUpdateRequest) -> dict[str, Any]:
    try:
        record = _store(request).update_key(
            key_id,
            label=body.label,
            scope=body.scope,
            capabilities=body.capabilities,
            models=body.models,
        )
    except GatewayKeyNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GatewayError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"record": record.payload()}


@api_router.get(
    "/keys/{key_id}/usage",
    summary="单把 Key 的用量：累计计数 + 采样窗口分解 + 最近请求",
)
def key_usage(
    request: Request,
    key_id: str,
    limit: int = Query(default=20, ge=1, le=200),
) -> dict[str, Any]:
    try:
        return _store(request).key_usage(key_id, limit=limit)
    except GatewayKeyNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@api_router.post("/keys/{key_id}/revoke", summary="吊销一把网关 API Key（停用，但保留记录与用量）")
def revoke_key(request: Request, key_id: str) -> dict[str, Any]:
    try:
        record = _store(request).revoke(key_id)
    except GatewayError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"record": record.payload()}


@api_router.post(
    "/keys/{key_id}/restore", summary="恢复一把已吊销的网关 API Key（重新可用）"
)
def restore_key(request: Request, key_id: str) -> dict[str, Any]:
    try:
        record = _store(request).restore(key_id)
    except GatewayKeyNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GatewayError as exc:
        # 未被吊销的 Key 无需恢复：语义上是「操作不被允许」，回 400。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"record": record.payload()}


@api_router.delete(
    "/keys/{key_id}", summary="删除一把已吊销的网关 API Key（记录不再出现在列表里）"
)
def delete_key(request: Request, key_id: str) -> dict[str, Any]:
    try:
        record = _store(request).delete(key_id)
    except GatewayKeyNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except GatewayError as exc:
        # 还在有效状态的 Key 不许直接删：必须先吊销。
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"record": record.payload()}


@api_router.get("/logs", summary="最近的网关请求日志（新的在前）")
def list_logs(
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    keyId: str = Query(default="", description="只看这把 Key 的请求"),
    sessionId: str = Query(default="", description="只看这个客户端会话标识的请求"),
) -> dict[str, Any]:
    return {
        "logs": _store(request).logs(
            limit,
            key_id=(keyId or "").strip() or None,
            session_id=(sessionId or "").strip() or None,
        )
    }


@api_router.delete("/logs", summary="清空网关请求日志")
def clear_logs(request: Request) -> dict[str, Any]:
    return {"removed": _store(request).clear_logs()}


@api_router.post("/models/refresh", summary="强制刷新上游模型目录")
async def refresh_models(request: Request) -> dict[str, Any]:
    # 必须走 _catalog（而不是 _fetch_catalog）：它会把结果写回 relay 缓存，
    # 否则界面「重新获取」拿到模型后，状态接口仍然显示「未获取」。
    models = await _catalog(_relay(request), refresh=True)
    return {
        "models": [model.payload() for model in models],
        "fresh": True,
    }


# ---------- 账号池 ----------


class PoolAccountRequest(BaseModel):
    """对池中某个账号的人工处置：排除 / 恢复；设为 / 取消指定偏好。

    两个字段都是可选：`None` 表示「这次不动这一项」。
    """

    excluded: bool | None = None
    preferred: bool | None = None


class PoolProbeRequest(BaseModel):
    accountId: str
    model: str = ""


#: 探测用的最小请求：Anthropic 路由 1 个 token（上游收非流式，比 chat 更省）。
PROBE_BODY: dict[str, Any] = {
    "max_tokens": 1,
    "messages": [{"role": "user", "content": "ping"}],
}


def _pool_payload(request: Request) -> dict[str, Any]:
    """账号池快照：每账号的可用状态、冷却剩余、积分（只读缓存）与人工处置。"""
    relay, store = _relay(request), _store(request)
    scope = DEFAULT_SCOPE
    preferred = store.preferred_account()
    accounts: list[dict[str, Any]] = []
    for account in relay.candidates(scope):
        info = relay.credits_cached(account.id)
        accounts.append(
            {
                "id": account.id,
                "name": account.display_name,
                "credentialState": str(account.credential_state),
                "state": relay.state(account),
                "cooldownSeconds": int(relay.cooldown(account.id)),
                "excluded": store.is_excluded(account.id),
                "preferred": account.id == preferred,
                # 余额查不到时给 None（界面要如实显示「查不到」，不能显示 0）。
                "credits": info.payload() if info is not None else None,
            }
        )
    return {"scope": scope, "preferred": preferred, "accounts": accounts}


@api_router.get("/pool", summary="账号池：每账号的可用 / 冷却 / 积分耗尽 / 已排除与指定偏好")
def gateway_pool(request: Request) -> dict[str, Any]:
    return _pool_payload(request)


@api_router.put(
    "/pool/accounts/{account_id}",
    summary="排除 / 恢复某个账号，或把它设为 / 取消「指定账号偏好」",
)
def update_pool_account(
    request: Request, account_id: str, body: PoolAccountRequest
) -> dict[str, Any]:
    store, relay = _store(request), _relay(request)
    if relay.account(DEFAULT_SCOPE, account_id) is None:
        raise HTTPException(status_code=404, detail="号池里没有这个账号")
    if body.excluded is not None:
        if body.excluded:
            store.exclude(account_id)
        else:
            store.include(account_id)
    if body.preferred is not None:
        store.set_preferred(account_id if body.preferred else None)
    return _pool_payload(request)


@api_router.post("/pool/probe", summary="探测某个账号：成功即解除它的冷却（结果写进请求日志）")
async def probe_pool_account(request: Request, body: PoolProbeRequest) -> dict[str, Any]:
    relay, store = _relay(request), _store(request)
    model = body.model.strip()
    if not model:
        raise HTTPException(status_code=400, detail="先选一个模型再探测")
    account = relay.account(DEFAULT_SCOPE, body.accountId)
    if account is None:
        raise HTTPException(status_code=404, detail="号池里没有这个账号")

    started = time.monotonic()
    ok, status, message = await _probe_account(relay, account, model)
    if ok:
        # 探测成功 = 这个账号此刻真的能用：清掉冷却与「凭据坏了」的标记。
        relay.report_success(DEFAULT_SCOPE, account.id)
    store.log(
        {
            "kind": "probe",
            "ok": ok,
            "stream": False,
            "status": status,
            "model": model,
            "requestedModel": model,
            "keyLabel": "账号池探测",
            "accountId": account.id,
            "accountName": account.display_name,
            "durationMs": int((time.monotonic() - started) * 1000),
            "promptTokens": 0,
            "completionTokens": 0,
            "totalTokens": 0,
            "hops": 0,
            "skipped": [],
            "error": message,
        }
    )
    return {
        "ok": ok,
        "status": status,
        "model": model,
        "accountId": account.id,
        "accountName": account.display_name,
        "error": message,
    }


async def _probe_account(
    relay: GatewayRelay, account: Account, model: str
) -> tuple[bool, int, str]:
    """发一次最小请求，返回 `(成功, 状态码, 错误文案)`。"""
    try:
        target = relay.messages_target(account, PROBE_BODY, model)
    except Exception as exc:  # noqa: BLE001 - 凭据缺失/账号被删都可能出问题，探测要如实回一句
        return False, 0, f"无法构造探测请求：{exc}"

    try:
        async with httpx.AsyncClient(timeout=_timeout(), trust_env=False) as client:
            response = await client.post(
                target["url"], headers=target["headers"], content=target["body"]
            )
    except httpx.HTTPError as exc:
        return False, 0, f"上游不可达（{type(exc).__name__}）"

    if response.status_code == 200:
        return True, 200, ""
    message = _upstream_message(response.text) or f"上游返回 HTTP {response.status_code}"
    return False, response.status_code, message


# ---------- OpenAI 兼容接口 ----------


@v1_router.get("/models", summary="列出号池可用的模型（OpenAI 兼容，按 Key 白名单过滤）")
async def v1_models(request: Request) -> Response:
    key = _authenticate(request)
    if key is None:
        return _openai_error(401, "缺少或无效的 API Key", code="invalid_api_key")
    if not _runtime(request).running:
        return _openai_error(503, "网关未启用，请先在「网关」页打开开关", code="gateway_disabled")
    try:
        models = await _catalog(_relay(request))
    except HTTPException as exc:
        return _openai_error(502, str(exc.detail))
    allowed = [model for model in models if key.allows(model.id, models)]
    return JSONResponse(
        content={
            "object": "list",
            "data": [
                {
                    "id": model.id,
                    "object": "model",
                    "owned_by": "workbuddy",
                }
                for model in allowed
            ],
        }
    )


@v1_router.post("/chat/completions", summary="OpenAI 兼容的号池转发（支持流式）")
async def v1_chat_completions(request: Request) -> Response:
    return await _relay_request(request, PROTOCOL_CHAT)


@v1_router.post("/responses", summary="OpenAI Responses 兼容转发（网关侧转换成 chat）")
async def v1_responses(request: Request) -> Response:
    return await _relay_request(request, PROTOCOL_RESPONSES)


@v1_router.post("/messages", summary="Anthropic Messages 兼容转发（原样直转到上游同名路由）")
async def v1_messages(request: Request) -> Response:
    return await _relay_request(request, PROTOCOL_MESSAGES)


async def _relay_request(request: Request, protocol: str) -> Response:
    """`/v1/*` 的统一入口：三个协议的差异只在这里分派。

    鉴权、模型解析、白名单、选号、换号、记账全部共用；协议差异只有两处 ——
    请求体转换（`prepare_relay`）与回写形状（字节透传 / 事件转换 / 错误体）。
    """
    key = _authenticate(request)
    if key is None:
        return _protocol_error(protocol, 401, "缺少或无效的 API Key", code="invalid_api_key")
    if not _runtime(request).running:
        return _protocol_error(
            protocol, 503, "网关未启用，请先在「网关」页打开开关", code="gateway_disabled"
        )

    store, relay = _store(request), _relay(request)
    body, problem = await _json_body(request)
    if body is None:
        return _protocol_error(protocol, 400, problem)

    # 会话标识只取一次，随后所有出口（含流式收尾）共用，免得各写各的。
    session_id = _session_hint(request, body)
    requested_model = str(body.get("model") or "")
    try:
        scope, model = resolve_model(requested_model)
    except GatewayRouteError as exc:
        return _protocol_error(protocol, 404, str(exc), code="model_not_found")

    try:
        chat_body = prepare_relay(protocol, body, scope=scope)
    except ProtocolNotSupported as exc:
        # 上游/该 scope 不提供这个协议：501 把原因说清楚，绝不静默降级到 chat。
        return _protocol_error(protocol, 501, str(exc), code="protocol_not_supported")
    except GatewayRouteError as exc:
        return _protocol_error(protocol, 400, str(exc))

    # 请求内容摘要：只取最后一条用户消息，随日志落盘供界面展示（见 `_prompt_preview`）。
    prompt_preview = _prompt_preview(chat_body)

    # 该 Key 的模型范围：越权调用按「模型不存在」处理，避免泄露目录信息。
    # `capability` 模式要拿当前目录判定，所以只有它才需要（且必须）加载目录。
    catalog: list[CatalogModel] | None = None
    if key.scope == SCOPE_CAPABILITY:
        try:
            catalog = await _catalog(relay)
        except HTTPException as exc:
            return _protocol_error(protocol, 502, str(exc.detail))
    if not key.allows(model, catalog):
        return _protocol_error(
            protocol, 404, f"该 API Key 无权调用模型 {model}", code="model_not_found"
        )

    stream = bool(body.get("stream"))
    hops = store.config().max_hops
    # 「指定账号偏好」来自账号池的人工设置：它可用时才优先，不可用由选号器回退。
    accounts = relay.pick_order(scope, preferred_account_id=store.preferred_account())
    if not accounts:
        return _protocol_error(
            protocol, 503, "号池里没有可用账号，请先在「号池」里添加账号", code="no_account"
        )

    client = httpx.AsyncClient(timeout=_timeout(), trust_env=False)
    handed_off = False
    skipped: list[str] = []
    try:
        for account in accounts[:hops]:
            try:
                target = _upstream_target(relay, protocol, account, chat_body, model)
            except CredentialError as exc:
                relay.report_failure(account.id, reason="credential")
                skipped.append(f"{account.display_name}（{exc}）")
                continue

            started = time.monotonic()
            try:
                upstream = await client.send(
                    client.build_request(
                        "POST", target["url"], headers=target["headers"], content=target["body"]
                    ),
                    stream=True,
                )
            except httpx.HTTPError as exc:
                relay.report_failure(account.id)
                skipped.append(f"{account.display_name}（上游不可达：{type(exc).__name__}）")
                continue

            status = upstream.status_code
            if status == 200:
                relay.report_success(scope, account.id)
                if stream:
                    handed_off = True
                    if protocol == PROTOCOL_RESPONSES:
                        # 上游没有 Responses 路由，事件得由网关翻一遍。
                        return StreamingResponse(
                            _translated_body(
                                store, key, account, model, requested_model, protocol,
                                upstream, client, started, skipped, session_id, prompt_preview,
                            ),
                            media_type="text/event-stream",
                        )
                    # chat 与 messages 都是上游原生形状：字节透传，中间不多插一手。
                    return StreamingResponse(
                        _stream_body(
                            store, key, account, model, requested_model, protocol,
                            upstream, client, started, skipped, session_id, prompt_preview,
                        ),
                        media_type=upstream.headers.get("content-type", "text/event-stream"),
                    )
                return await _buffered_response(
                    store, key, account, model, requested_model, protocol,
                    upstream, started, skipped, session_id, prompt_preview,
                )

            text = (await upstream.aread()).decode("utf-8", "replace")
            await upstream.aclose()
            relay.report_failure(account.id, status=status)
            message = _upstream_message(text) or f"上游返回 HTTP {status}"
            if not _retryable(status):
                # 400/404 这类是我们发出去的请求本身有问题，换号也没用 → 直接透给调用方。
                store.log(_entry(key, account, model, requested_model, ok=False, stream=stream,
                                 status=status, started=started, error=message, skipped=skipped,
                                 session_id=session_id, prompt_preview=prompt_preview))
                store.record_use(key.id)
                return _protocol_error(protocol, 400 if status < 500 else 502, message)

            skipped.append(f"{account.display_name}（HTTP {status}）")

        last = skipped[-1] if skipped else "没有可用账号"
        message = f"所有账号都没能完成请求：{last}"
        store.log(_entry(key, None, model, requested_model, ok=False, stream=stream, status=502,
                         started=time.monotonic(), error=message, skipped=skipped,
                         session_id=session_id, prompt_preview=prompt_preview))
        store.record_use(key.id)
        return _protocol_error(protocol, 502, message, code="upstream_failed")
    finally:
        if not handed_off:
            await client.aclose()


async def _buffered_response(
    store: GatewayStore,
    key: ApiKeyRecord,
    account: Account,
    model: str,
    requested_model: str,
    protocol: str,
    upstream: httpx.Response,
    started: float,
    skipped: list[str],
    session_id: str = "",
    prompt_preview: str = "",
) -> Response:
    """非流式：读完上游响应，按入站协议回写。

    - chat / responses：上游恒 SSE（`chat_target` 强制流式），先聚合成一条 chat
      completion，再按协议回写（chat 原样，responses 翻成 `response` 对象）。
    - messages：上游原生收非流式，直接把它那份 Anthropic JSON 原样透传；万一上游
      对非流式请求回了 SSE，说明形状不是我们认识的那套，宁可 502 也不猜。
    """
    raw = await upstream.aread()
    content_type = upstream.headers.get("content-type", "")
    await upstream.aclose()
    text = raw.decode("utf-8", "replace")
    is_sse = "text/event-stream" in content_type or text.lstrip().startswith("data:")

    if is_sse and protocol == PROTOCOL_MESSAGES:
        message = "上游对 Anthropic 非流式请求返回了 SSE，网关不支持这种形状"
        store.log(_entry(key, account, model, requested_model, ok=False, stream=False, status=502,
                         started=started, error=message, skipped=skipped,
                         session_id=session_id, prompt_preview=prompt_preview))
        store.record_use(key.id)
        return _protocol_error(protocol, 502, message, code="bad_upstream_body")

    completion: dict[str, Any] | None = None
    if is_sse:
        completion = aggregate_sse(text)
    else:
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        completion = parsed if isinstance(parsed, dict) else None

    if completion is None:
        message = _upstream_message(text) or "上游返回了无法解析的内容"
        store.log(_entry(key, account, model, requested_model, ok=False, stream=False, status=502,
                         started=started, error=message, skipped=skipped,
                         session_id=session_id, prompt_preview=prompt_preview))
        store.record_use(key.id)
        return _protocol_error(protocol, 502, message, code="bad_upstream_body")

    prompt, output = _usage_of(protocol, completion)
    store.log(_entry(key, account, model, requested_model, ok=True, stream=False, status=200,
                     started=started, prompt=prompt, completion=output, skipped=skipped,
                     session_id=session_id, prompt_preview=prompt_preview))
    store.record_use(key.id, tokens=prompt + output)
    return JSONResponse(content=_client_object(protocol, completion, model))


async def _translated_body(
    store: GatewayStore,
    key: ApiKeyRecord,
    account: Account,
    model: str,
    requested_model: str,
    protocol: str,
    upstream: httpx.Response,
    client: httpx.AsyncClient,
    started: float,
    skipped: list[str],
    session_id: str = "",
    prompt_preview: str = "",
) -> AsyncIterator[bytes]:
    """流式：把上游 chat SSE 逐条翻成 Responses 事件（只对 responses 有意义）。"""
    translator = _translator(protocol, model)
    ok = True
    error = ""
    prompt = output = 0
    try:
        for frame in translator.start():
            yield frame.encode("utf-8")
        async for line in upstream.aiter_lines():
            chunk = _line_chunk(line)
            if chunk is None:
                continue
            for frame in translator.feed(chunk):
                yield frame.encode("utf-8")
            _prompt, _output = _usage_of(protocol, chunk)
            if _prompt or _output:
                prompt, output = _prompt, _output
        for frame in translator.finish():
            yield frame.encode("utf-8")
    except httpx.HTTPError as exc:
        ok = False
        error = f"流式传输中断：{type(exc).__name__}"
    finally:
        await upstream.aclose()
        await client.aclose()
        store.log(_entry(key, account, model, requested_model, ok=ok, stream=True,
                         status=200 if ok else 502, started=started,
                         prompt=prompt, completion=output, error=error, skipped=skipped,
                         session_id=session_id, prompt_preview=prompt_preview))
        store.record_use(key.id, tokens=prompt + output)


async def _stream_body(
    store: GatewayStore,
    key: ApiKeyRecord,
    account: Account,
    model: str,
    requested_model: str,
    protocol: str,
    upstream: httpx.Response,
    client: httpx.AsyncClient,
    started: float,
    skipped: list[str],
    session_id: str = "",
    prompt_preview: str = "",
) -> AsyncIterator[bytes]:
    """流式：原样透传上游字节，只在收尾时从尾部解析 usage 并记账。

    透传对 chat 与 messages 都成立 —— 两边都是上游原生形状，中间不多插一手。
    """
    tail = b""
    ok = True
    error = ""
    try:
        async for chunk in upstream.aiter_bytes():
            yield chunk
            tail = (tail + chunk)[-TAIL_BYTES:]
    except httpx.HTTPError as exc:
        ok = False
        error = f"流式传输中断：{type(exc).__name__}"
    finally:
        await upstream.aclose()
        await client.aclose()
        prompt, completion = _tail_usage(tail, protocol)
        store.log(_entry(key, account, model, requested_model, ok=ok, stream=True,
                         status=200 if ok else 502, started=started,
                         prompt=prompt, completion=completion, error=error, skipped=skipped,
                         session_id=session_id, prompt_preview=prompt_preview))
        store.record_use(key.id, tokens=prompt + completion)


# ---------- 内部工具 ----------


#: 客户端可能把会话标识放在这些请求头里，按此顺序取第一个非空的。
_SESSION_HEADERS = ("x-session-id", "x-conversation-id", "openai-conversation-id", "x-thread-id")
#: 请求体里的候选键名（OpenAI 生态里各家叫法不一）。
_SESSION_BODY_KEYS = ("conversation_id", "session_id", "conversationId", "sessionId")
#: 会话标识只作线索展示，不参与匹配，截断以免日志被超长串撑爆。
_SESSION_MAX_CHARS = 120


def _session_hint(request: Request, body: dict[str, Any] | None) -> str:
    """从请求里取出**客户端传入**的会话标识（取不到就返回空串）。

    网关**不生成**会话 id —— 这是「哪个会话在用这把 Key」的线索，客户端给才有。
    空串在日志里如实显示成 `—`，绝不猜测、绝不伪造。
    """
    for name in _SESSION_HEADERS:
        value = (request.headers.get(name) or "").strip()
        if value:
            return value[:_SESSION_MAX_CHARS]

    payload = body if isinstance(body, dict) else {}
    for name in _SESSION_BODY_KEYS:
        value = payload.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_SESSION_MAX_CHARS]

    metadata = payload.get("metadata")
    if isinstance(metadata, dict):
        value = metadata.get("session_id") or metadata.get("conversation_id")
        if isinstance(value, str) and value.strip():
            return value.strip()[:_SESSION_MAX_CHARS]

    # OpenAI 兼容客户端常把稳定的终端用户标识塞在 `user` 字段里，当最后一道线索。
    user = payload.get("user")
    if isinstance(user, str) and user.strip():
        return user.strip()[:_SESSION_MAX_CHARS]

    return ""


#: 提示词摘要的最大字符数：只留一句能看懂的上下文，不整段落盘。
_PROMPT_MAX_CHARS = 200


def _prompt_preview(body: dict[str, Any] | None) -> str:
    """从（已归一成 chat 形状的）请求体里抽一段「请求内容」摘要。

    只取**最后一条有文本的 user 消息**（对多轮对话而言它就是「这次问的什么」），
    归一成单行、截断；取不到就返回空串（界面如实显示「—」，绝不伪造）。
    """
    if not isinstance(body, dict):
        return ""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if str(message.get("role") or "") not in ("user", "human"):
            continue
        content = message.get("content")
        text: str
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
            text = "".join(parts)
        else:
            continue
        collapsed = " ".join(text.split())
        if collapsed:
            return collapsed[:_PROMPT_MAX_CHARS]
    return ""


def _entry(
    key: ApiKeyRecord,
    account: Account | None,
    model: str,
    requested_model: str,
    *,
    ok: bool,
    stream: bool,
    status: int,
    started: float,
    prompt: int = 0,
    completion: int = 0,
    error: str = "",
    skipped: list[str] | None = None,
    session_id: str = "",
    prompt_preview: str = "",
) -> dict[str, Any]:
    return {
        "ok": ok,
        "stream": stream,
        "status": status,
        "model": model,
        "requestedModel": requested_model,
        "keyId": key.id,
        "keyLabel": key.label,
        "accountId": account.id if account else None,
        "accountName": account.display_name if account else None,
        "durationMs": int((time.monotonic() - started) * 1000),
        "promptTokens": prompt,
        "completionTokens": completion,
        "totalTokens": prompt + completion,
        "hops": len(skipped or []),
        "skipped": list(skipped or []),
        "error": error,
        # 客户端传入的会话标识；取不到就是空串（界面显示为 —）。
        "sessionId": session_id,
        # 请求内容摘要：最后一条用户消息的文本，截断后落盘，供日志与用量抽屉展示。
        "promptPreview": prompt_preview,
    }


def _usage_of(protocol: str, payload: Any) -> tuple[int, int]:
    """按入站协议取 `(promptTokens, completionTokens)`。

    Anthropic 的 usage 字段名与 OpenAI 不同（`input_tokens` / `output_tokens`，
    流式时 first 分片在 `message.usage` 里、结束分片在顶层 `usage` 里）。
    """
    if protocol != PROTOCOL_MESSAGES:
        return usage_tokens(payload)
    if not isinstance(payload, dict):
        return 0, 0
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        message = payload.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return 0, 0
    return int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def _tail_usage(raw: bytes, protocol: str) -> tuple[int, int]:
    """流式收尾：从尾部字节里解析令牌数。

    非零值覆盖零值 —— Anthropic 的输入 tokens 只在首个分片出现、输出 tokens
    只在结束分片出现，单向倒着扫会丢掉其中一半。
    """
    if not raw:
        return 0, 0
    text = raw.decode("utf-8", "replace")
    prompt = output = 0
    for chunk in iter_sse_json(text):
        _prompt, _output = _usage_of(protocol, chunk)
        prompt = _prompt or prompt
        output = _output or output
    return prompt, output


def _client_object(protocol: str, completion: dict[str, Any], model: str) -> dict[str, Any]:
    """上游 JSON → 入站协议的非流式对象。

    chat 与 messages 拿到的都是上游原生形状，原样返回；只有 responses 需要
    把聚合出来的 chat completion 翻成 `response` 对象。
    """
    if protocol == PROTOCOL_RESPONSES:
        return chat_completion_to_response(completion, model=model)
    return completion


def _translator(protocol: str, model: str) -> Any:
    """选一个流式事件翻译器（只有 responses 需要）。"""
    if protocol == PROTOCOL_RESPONSES:
        return ResponsesStream(model)
    raise ValueError(f"{protocol} 不需要事件转换")


def _upstream_target(
    relay: GatewayRelay, protocol: str, account: Account, body: dict[str, Any], model: str
) -> dict[str, Any]:
    """按入站协议挑上游路由：Anthropic 走它自家那条，其余走 chat。"""
    if protocol == PROTOCOL_MESSAGES:
        return relay.messages_target(account, body, model)
    return relay.chat_target(account, body, model)


def _line_chunk(line: str) -> dict[str, Any] | None:
    """把上游 SSE 的一行解析成分片对象；非 `data:` 行或 `[DONE]` 返回 None。"""
    text = line.strip()
    if not text.startswith("data:"):
        return None
    payload = text[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        parsed = json.loads(payload)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _upstream_message(text: str) -> str:
    """从上游响应体里挑一句能给用户看的话。"""
    snippet = text.strip()
    if not snippet:
        return ""
    try:
        data = json.loads(snippet)
    except ValueError:
        return snippet[:300]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])[:300]
        if data.get("msg"):
            return str(data["msg"])[:300]
        if data.get("message"):
            return str(data["message"])[:300]
    return snippet[:300]


# ---------- 模型目录 ----------


async def refresh_catalog(relay: GatewayRelay) -> list[CatalogModel]:
    """强制抓取一次模型目录并写回缓存。供「重新获取」按钮与后台自动刷新线程共用。"""
    return await _catalog(relay, refresh=True)


async def _catalog(relay: GatewayRelay, *, refresh: bool = False) -> list[CatalogModel]:
    if not refresh:
        cached = relay.cached_catalog()
        if cached is not None:
            return cached
    models = await _fetch_catalog(relay)
    relay.cache_catalog(models)
    return models


async def _fetch_catalog(relay: GatewayRelay) -> list[CatalogModel]:
    """用号池里第一个能用的账号取模型目录；全都失败就退回旧缓存。"""
    problems: list[str] = []
    for account in relay.pick_order(DEFAULT_SCOPE):
        try:
            target = relay.models_target(account)
        except CredentialError as exc:
            relay.report_failure(account.id, reason="credential")
            problems.append(f"{account.display_name}：{exc}")
            continue

        message = ""
        try:
            async with httpx.AsyncClient(timeout=_timeout(), trust_env=False) as client:
                response = await client.get(target["url"], headers=target["headers"])
            if response.status_code != 200:
                message = f"上游 HTTP {response.status_code}"
            else:
                document = response.json()
                if isinstance(document, dict):
                    code = document.get("code")
                    if code not in (None, 0):
                        message = str(document.get("msg") or "上游返回失败")
                    else:
                        relay.report_success(DEFAULT_SCOPE, account.id)
                        return parse_catalog(document)
                else:
                    message = "目录响应不是对象"
        except httpx.HTTPError as exc:
            message = f"上游不可达（{type(exc).__name__}）"
        except ValueError as exc:
            message = f"目录响应不是 JSON（{exc.__class__.__name__}）"
        except GatewayUpstreamError as exc:
            message = str(exc)

        relay.report_failure(account.id, status=0 if "不可达" in message else 500)
        problems.append(f"{account.display_name}：{message}")

    stale = relay.any_catalog()
    if stale:
        return stale
    detail = "；".join(problems[:3]) or "号池里没有可用账号"
    raise HTTPException(status_code=502, detail=f"无法获取模型目录：{detail}")


# ---------- 独立运行的网关应用 ----------


def build_gateway_app(store: GatewayStore, relay: GatewayRelay, runtime: GatewayRuntime) -> FastAPI:
    """网关端口的独立 FastAPI 应用：挂 `/v1/*`，同时把两个端点也挂在根路径上。

    与主服务共用同一个 store / relay / runtime 对象（同进程），
    所以「界面改配置 → 网关端口立即生效」是直接改内存。

    为什么要根路径别名：客户端对 base_url 的理解有两种 —— 有的自带 `/v1`
    （`base+"/models"` → `/v1/models`），有的不带、由客户端自己拼 `/v1`
    （`base+"/models"` → `/models`）。两种都接受，用户就不用去猜该填哪一段。
    """
    app = FastAPI(
        title="chaierww gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.gateway = store
    app.state.gateway_relay = relay
    app.state.gateway_runtime = runtime
    app.include_router(v1_router)
    app.add_api_route("/models", v1_models, methods=["GET"], summary="列出可用模型（根路径别名）")
    app.add_api_route(
        "/chat/completions",
        v1_chat_completions,
        methods=["POST"],
        summary="OpenAI 兼容转发（根路径别名）",
    )
    app.add_api_route(
        "/responses",
        v1_responses,
        methods=["POST"],
        summary="Responses 兼容转发（根路径别名）",
    )
    app.add_api_route(
        "/messages",
        v1_messages,
        methods=["POST"],
        summary="Anthropic Messages 兼容转发（根路径别名）",
    )
    return app

__all__ = ["api_router", "v1_router", "build_gateway_app"]
