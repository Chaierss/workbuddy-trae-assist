"""网关的**选号与协议层**：号池挑号策略、上游请求构造、模型目录解析、SSE 聚合。

只有纯逻辑 + 上游契约常量，**不发请求**（HTTP 由 `api/gateway.py` 用 httpx 执行）。
这样选号策略、模型名解析、SSE 聚合都能被无网络的单测直接覆盖。

## 上游契约（WorkBuddy CN）

对齐参考实现与官方客户端（字段名/取值都是抓包固化值，改动前必须先验证）：

- chat：`POST https://copilot.tencent.com/v2/chat/completions`（OpenAI 兼容、恒 SSE）
- 模型目录：`GET https://copilot.tencent.com/console/enterprises/personal/models`
  （响应 `{code,msg,data:{agents:[{name:"cli",models:[...]}], models:[...]}}`，
  以 `agents[name=cli]` 白名单为准，跳过 `disabled` 与上下文窗口为 0 的项）
- UA：`CLI/2.63.2 CodeBuddy/2.63.2`
- chat 头必须带 `X-CodeBuddy-Request: 1` / `X-Agent-Purpose: conversation` /
  `X-Product: SaaS`，缺失会被网关 WAF 当未知客户端拒（且 **chat 绝不带 refresh token**）

## 协议面

实测（2026-10-20，真实账号）：WorkBuddy 上游**原生**两条路由 —— `/v2/chat/completions`
（OpenAI 兼容、恒 SSE）与 `/v1/messages`（Anthropic Messages、流式/非流式都收，
连 thinking 块与 cache 计数都在）；`/v1/responses` 是 404。

所以对外三条协议是这么落地的：

- `POST /v1/chat/completions` → 上游 chat，字节透传。
- `POST /v1/messages` → 上游 `/v1/messages`，**原样直转**（只改写模型名），
  转一手只会把客户端要的 thinking / cache_control 丢光。
- `POST /v1/responses` → 上游没有这条路由，由网关按 chat **合成**（含事件映射）；
  不支持 `previous_response_id` 续写。

某 scope 的上游不提供某协议时返回 501 并在正文写明原因，**不静默降级到 chat**。
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from secrets import token_hex
from typing import Any

from chaierww.domain.accounts.models import Account, CredentialState
from chaierww.domain.credits import CreditInfo
from chaierww.domain.platform_api import CredentialError, workbuddy_credentials

WB_SCOPE = "workbuddy-cn"

WB_CHAT_BASE = "https://copilot.tencent.com"
WB_BILLING_ORIGIN = "https://www.codebuddy.cn"
WB_CHAT_URL = f"{WB_CHAT_BASE}/v2/chat/completions"
#: 上游**原生**的 Anthropic Messages 路由（2026-10-20 实测：200 + 自家 SSE 事件，
#: 含 thinking / cache 计数），所以这条协议原样直转，不做任何转换。
WB_MESSAGES_URL = f"{WB_CHAT_BASE}/v1/messages"
WB_MODELS_URL = f"{WB_CHAT_BASE}/console/enterprises/personal/models"
WB_USER_AGENT = "CLI/2.63.2 CodeBuddy/2.63.2"

#: 网关对外只用上游原名（如 `deepseek-v4.1-flash`），不加任何平台前缀。
DEFAULT_SCOPE = WB_SCOPE

#: 会把 Key 打到哪个平台的顺序（网关一期只接 WorkBuddy）。
CHAT_SCOPES: tuple[str, ...] = (WB_SCOPE,)

CATALOG_TTL_SECONDS = 600

#: 账号级冷却：同一账号失败后这段时间内不再被挑中（秒）。
COOLDOWN_UNAUTHORIZED = 600.0
COOLDOWN_RATE_LIMITED = 60.0
COOLDOWN_UPSTREAM_5XX = 30.0
COOLDOWN_TRANSPORT = 10.0


class GatewayRouteError(RuntimeError):
    """模型/作用域无法路由。消息面向用户。"""


class GatewayUpstreamError(RuntimeError):
    """上游拒绝或不可达（已带状态码与可读消息）。"""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


# ---------- 模型目录 ----------


@dataclass(frozen=True, slots=True)
class CatalogModel:
    id: str
    name: str
    context_window: int
    supports_images: bool
    free: bool
    badges: tuple[str, ...] = ()
    max_output_tokens: int = 0
    credits: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "ownedBy": "workbuddy",
            "contextWindow": self.context_window,
            "supportsImages": self.supports_images,
            "free": self.free,
            "badges": list(self.badges),
            "maxOutputTokens": self.max_output_tokens,
            "credits": self.credits,
        }


def parse_catalog(document: dict[str, Any]) -> list[CatalogModel]:
    """上游模型目录 → 模型列表（以 cli agent 白名单为准）。空目录抛错。"""
    data = document.get("data")
    if not isinstance(data, dict):
        data = document if isinstance(document, dict) else {}

    models = data.get("models") if isinstance(data.get("models"), list) else []
    agents = data.get("agents") if isinstance(data.get("agents"), list) else []

    whitelist: list[str] | None = None
    for agent in agents:
        if isinstance(agent, dict) and agent.get("name") == "cli":
            raw = agent.get("models")
            if isinstance(raw, list):
                whitelist = [str(item) for item in raw if isinstance(item, str)]
            break
    if not whitelist:
        raise GatewayUpstreamError("上游模型目录里没有 cli agent 的模型清单")

    by_id: dict[str, CatalogModel] = {}
    for item in models:
        if not isinstance(item, dict):
            continue
        model_id = str(item.get("id") or "")
        if not model_id or item.get("disabled") is True:
            continue
        max_input = int(item.get("maxInputTokens") or 0)
        max_output = int(item.get("maxOutputTokens") or 0)
        if max_input <= 0 or max_output <= 0:
            continue
        window = item.get("contextWindow")
        context = 0
        if isinstance(window, dict):
            context = int(window.get("defaultLength") or 0)
        if context <= 0:
            context = max_input
        tags = item.get("tags") if isinstance(item.get("tags"), list) else []
        badges = tuple(
            str(tag).split(":", 2)[1]
            for tag in tags
            if isinstance(tag, str) and tag.startswith("badge:") and tag.count(":") >= 2
        )
        credits = str(item.get("credits") or "")
        by_id[model_id] = CatalogModel(
            id=model_id,
            name=str(item.get("name") or model_id),
            context_window=context,
            supports_images=bool(item.get("supportsImages")),
            free=credits.startswith("x0.00"),
            badges=badges,
            max_output_tokens=max_output,
            credits=credits,
        )

    ordered = [by_id[model] for model in whitelist if model in by_id]
    if not ordered:
        raise GatewayUpstreamError("上游模型目录里没有可用的 cli 模型")
    return ordered


# ---------- 模型名解析 ----------


def resolve_model(raw: str, default_scope: str = DEFAULT_SCOPE) -> tuple[str, str]:
    """`"deepseek-v4.1-flash"` → `("workbuddy-cn", "deepseek-v4.1-flash")`。

    模型名就是上游原名，客户端不需要加任何平台前缀。带了 `wb/`、`trae/` 这类前缀
    会直接报「不需要前缀」，而不是把带斜杠的假名发给上游去撞 404。
    """
    name = (raw or "").strip()
    if not name:
        raise GatewayRouteError("缺少 model 参数")
    if "/" in name:
        raise GatewayRouteError(f"模型名不需要前缀，直接用 {name.rsplit('/', 1)[-1]!r} 即可")
    return default_scope, name


# ---------- SSE / usage ----------


def iter_sse_json(text: str) -> list[dict[str, Any]]:
    """从 SSE 文本里抽出所有 JSON 分片（`data:` 行；容错裸 JSON 行）。"""
    chunks: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(":"):
            continue
        payload = line[5:].strip() if line.startswith("data:") else line
        if not payload or payload == "[DONE]" or not payload.startswith("{"):
            continue
        try:
            item = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            chunks.append(item)
    return chunks


def usage_tokens(payload: Any) -> tuple[int, int]:
    """从 usage 对象里取 `(promptTokens, completionTokens)`。"""
    if not isinstance(payload, dict):
        return 0, 0
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return 0, 0
    prompt = int(usage.get("prompt_tokens") or usage.get("promptTokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("completionTokens") or 0)
    return prompt, completion


def aggregate_sse(text: str) -> dict[str, Any] | None:
    """把上游 SSE 分片聚合成一条非流式 `chat.completion`。

    非流式客户端（`stream:false`）碰到的却是 SSE 上游时用它兜底：内容、思考、
    工具调用按 index 归并，usage 取最后一个带 usage 的分片。
    """
    head: dict[str, Any] = {}
    content: list[str] = []
    reasoning: list[str] = []
    tool_calls: dict[int, dict[str, Any]] = {}
    finish_reason: str | None = None
    usage: dict[str, Any] | None = None

    for chunk in iter_sse_json(text):
        if not head:
            head = chunk
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                if isinstance(delta.get("content"), str) and delta["content"]:
                    content.append(delta["content"])
                if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
                    reasoning.append(delta["reasoning_content"])
                for call in delta.get("tool_calls") or []:
                    if not isinstance(call, dict):
                        continue
                    index = int(call.get("index") or 0)
                    slot = tool_calls.setdefault(
                        index,
                        {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                    )
                    if call.get("id"):
                        slot["id"] = str(call["id"])
                    function = call.get("function")
                    if isinstance(function, dict):
                        if function.get("name"):
                            slot["function"]["name"] = str(function["name"])
                        if isinstance(function.get("arguments"), str):
                            slot["function"]["arguments"] += function["arguments"]
            if choice.get("finish_reason"):
                finish_reason = str(choice["finish_reason"])

    if not head:
        return None

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content) if content else None,
    }
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    if tool_calls:
        message["tool_calls"] = [tool_calls[index] for index in sorted(tool_calls)]

    result: dict[str, Any] = {
        key: head[key]
        for key in ("id", "created", "model", "system_fingerprint")
        if key in head
    }
    result["object"] = "chat.completion"
    result["choices"] = [
        {"index": 0, "message": message, "finish_reason": finish_reason or "stop"}
    ]
    if usage is not None:
        result["usage"] = usage
    return result


# ---------- 选号 ----------


class GatewayRelay:
    """号池选号 + 上游请求构造 + 目录缓存。

    冷却 / 损坏 / 粘性 / 轮转都在内存里（重启即清）；**排除集与指定偏好**是人工
    处置，由持久层提供（`excluded` 提供排除集、`credits` 提供积分缓存快照），
    所以重启后仍然生效。两个提供者都可缺省：不传就是「没有排除、不查积分」。
    """

    def __init__(
        self,
        repository: Any,
        *,
        excluded: Callable[[], set[str]] | None = None,
        credits: Callable[[str], CreditInfo | None] | None = None,
    ) -> None:
        self._repository = repository
        self._excluded_source = excluded
        self._credits = credits
        self._cooldown: dict[str, float] = {}
        self._broken: set[str] = set()
        self._sticky: dict[str, str] = {}
        self._rotor: dict[str, int] = {}
        self._catalog: list[CatalogModel] | None = None
        self._catalog_at: float = 0.0
        #: 目录上次写入的**墙上时间**（ISO），只用于界面展示；TTL 判定仍用单调时钟。
        self._catalog_wall: str | None = None
        self._clock = time.monotonic

    # ----- 号池 -----

    def candidates(self, scope: str) -> list[Account]:
        """可参与网关转发的账号：删掉的、需重新登录的、凭据解不开的都不算。"""
        if scope not in CHAT_SCOPES:
            return []
        accounts = self._repository.list_accounts(scope)
        usable = [
            account
            for account in accounts
            if account.credential_state is not CredentialState.RELOGIN_REQUIRED
            and account.id not in self._broken
        ]
        order = {
            CredentialState.USABLE: 0,
            CredentialState.UNVERIFIED: 1,
            CredentialState.UNKNOWN: 2,
        }
        return sorted(
            usable,
            key=lambda account: (order.get(account.credential_state, 3), account.created_at),
        )

    def account(self, scope: str, account_id: str) -> Account | None:
        """按 id 取号池里的账号（含冷却中 / 已排除 / 凭据损坏的，供探测与人工处置用）。"""
        if scope not in CHAT_SCOPES:
            return None
        for item in self._repository.list_accounts(scope):
            if item.id == account_id:
                return item
        return None

    def excluded_ids(self) -> set[str]:
        """被手工排除的账号 id（没有持久层时视为空集）。"""
        if self._excluded_source is None:
            return set()
        return set(self._excluded_source())

    def credits_cached(self, account_id: str) -> CreditInfo | None:
        """积分缓存快照：**只读**，绝不因为选号而打一次上游。"""
        if self._credits is None:
            return None
        return self._credits(account_id)

    def credits_exhausted(self, account_id: str) -> bool:
        """积分是否已耗尽。查不到余额（含「这种凭据查不了」）时按**没耗尽**处理 ——
        宁可多试一个账号，也不要拿缺失的数据误杀一个还能用的账号。
        """
        info = self.credits_cached(account_id)
        if info is None or not info.available or info.credits is None:
            return False
        return info.credits <= 0

    def state(self, account: Account) -> str:
        """账号当前在号池里的状态（界面按这个渲染）。

        `usable` / `cooling` / `exhausted` / `excluded` / `unavailable`
        （`unavailable` = 需重新登录或凭据结构性解不开）。
        """
        if account.id in self.excluded_ids():
            return "excluded"
        if (
            account.credential_state is CredentialState.RELOGIN_REQUIRED
            or account.id in self._broken
        ):
            return "unavailable"
        if self.credits_exhausted(account.id):
            return "exhausted"
        if self.cooldown(account.id) > 0:
            return "cooling"
        return "usable"

    def cooldown(self, account_id: str) -> float:
        """剩余冷却秒数（0 表示可用）。"""
        until = self._cooldown.get(account_id, 0.0)
        remaining = until - self._clock()
        return max(0.0, remaining)

    def pick_order(self, scope: str, *, preferred_account_id: str | None = None) -> list[Account]:
        """挑号顺序：指定偏好 → 粘住上次成功的账号 → 轮转起点依次补位。

        已被排除的、积分耗尽的、需重登/凭据坏了的一律**不出现**在结果里；
        冷却中的账号仍然留在队尾等着被重试（冷却只是「先别首选」，不是「不能用」）。
        `preferred_account_id` 只在它**当前可用**时才被提到最前，否则回退常规择优 ——
        对齐「指定账号不可用会回退」的语义。
        """
        accounts = self.candidates(scope)
        if not accounts:
            return []

        excluded = self.excluded_ids()
        now = self._clock()
        healthy: list[Account] = []
        cooling: list[Account] = []
        for account in accounts:
            if account.id in excluded or self.credits_exhausted(account.id):
                continue
            if self._cooldown.get(account.id, 0.0) > now:
                cooling.append(account)
            else:
                healthy.append(account)

        preferred = (
            next((item for item in healthy if item.id == preferred_account_id), None)
            if preferred_account_id
            else None
        )
        if preferred is not None:
            healthy = [preferred, *(item for item in healthy if item.id != preferred.id)]
        else:
            sticky_id = self._sticky.get(scope)
            if sticky_id:
                healthy.sort(key=lambda account: 0 if account.id == sticky_id else 1)
            elif len(healthy) > 1:
                start = self._rotor.get(scope, 0) % len(healthy)
                healthy = healthy[start:] + healthy[:start]

        self._rotor[scope] = self._rotor.get(scope, 0) + 1
        return healthy + cooling

    def pick(self, scope: str, *, preferred_account_id: str | None = None) -> Account | None:
        """挑一个账号：`pick_order(...)` 的第一名；一个都挑不到时返回 `None`。"""
        order = self.pick_order(scope, preferred_account_id=preferred_account_id)
        return order[0] if order else None

    def sticky_account(self, scope: str) -> str | None:
        return self._sticky.get(scope)

    def report_success(self, scope: str, account_id: str) -> None:
        self._sticky[scope] = account_id
        self._cooldown.pop(account_id, None)
        self._broken.discard(account_id)

    def report_failure(self, account_id: str, *, status: int = 0, reason: str = "") -> None:
        if reason == "credential":
            # 凭据解不开（客户端 DPAPI 信封）是**结构性**问题，本次进程内不再尝试。
            self._broken.add(account_id)
            self._cooldown.pop(account_id, None)
            return
        if status in (401, 403):
            seconds = COOLDOWN_UNAUTHORIZED
        elif status == 429:
            seconds = COOLDOWN_RATE_LIMITED
        elif status >= 500 or status == 0:
            seconds = COOLDOWN_UPSTREAM_5XX if status else COOLDOWN_TRANSPORT
        else:
            seconds = COOLDOWN_TRANSPORT
        self._cooldown[account_id] = self._clock() + seconds

    # ----- 上游请求 -----

    def chat_target(self, account: Account, body: dict[str, Any], model: str) -> dict[str, Any]:
        """构造上游 chat 请求。凭据解不开（加密信封）时抛 `CredentialError`。"""
        # 上游 chat 只支持流式：带 stream=false 过去会被直接 400
        # 「Non-stream chat request is currently not supported」。
        # 所以出站一律要求流式；客户端要非流式时，由 api 层把 SSE 聚合成一条 completion。
        return self._body_target(account, body, model, WB_CHAT_URL, force_stream=True)

    def messages_target(self, account: Account, body: dict[str, Any], model: str) -> dict[str, Any]:
        """构造上游 Anthropic Messages 请求：**原样**转发，只改写模型名。

        上游认这套协议（连 thinking 块、cache 计数都在），而且流式/非流式都收，
        所以不做任何字段转换、也不强改 stream —— 转一手只会把客户端要的东西丢掉。
        """
        return self._body_target(account, body, model, WB_MESSAGES_URL, force_stream=False)

    def _body_target(
        self,
        account: Account,
        body: dict[str, Any],
        model: str,
        url: str,
        *,
        force_stream: bool,
    ) -> dict[str, Any]:
        credential = self._repository.current_credential(account.id)
        token, uid, domain = workbuddy_credentials(credential)
        if not token:
            raise CredentialError("账号没有可用的访问令牌")
        payload = dict(body)
        payload["model"] = model
        if force_stream:
            payload["stream"] = True
        return {
            "url": url,
            "headers": _chat_headers(token, uid, domain),
            "body": json.dumps(payload, ensure_ascii=False),
        }

    def models_target(self, account: Account) -> dict[str, Any]:
        credential = self._repository.current_credential(account.id)
        token, uid, domain = workbuddy_credentials(credential)
        return {
            "url": WB_MODELS_URL,
            "headers": _models_headers(token, uid, domain),
        }

    # ----- 目录缓存 -----

    def cached_catalog(self) -> list[CatalogModel] | None:
        if self._catalog is None:
            return None
        if self._clock() - self._catalog_at > CATALOG_TTL_SECONDS:
            return None
        return self._catalog

    def any_catalog(self) -> list[CatalogModel] | None:
        return self._catalog

    def catalog_meta(self) -> dict[str, Any]:
        """目录缓存状态：是否仍在 TTL 内 + 上次抓取的墙上时间（没抓过为 None）。"""
        return {
            "fresh": self.cached_catalog() is not None,
            "fetchedAt": self._catalog_wall,
        }

    def cache_catalog(self, models: list[CatalogModel]) -> None:
        self._catalog = models
        self._catalog_at = self._clock()
        self._catalog_wall = datetime.now(UTC).replace(microsecond=0).isoformat()


def _chat_headers(token: str, uid: str, domain: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Accept-Language": "zh-CN",
        "Content-Type": "application/json",
        "Origin": WB_BILLING_ORIGIN,
        "Referer": f"{WB_BILLING_ORIGIN}/",
        "User-Agent": WB_USER_AGENT,
        "X-CodeBuddy-Request": "1",
        "X-Agent-Purpose": "conversation",
        "X-Requested-With": "XMLHttpRequest",
        "X-Product": "SaaS",
    }
    # 缺失的身份字段用官方 CLI 的 X-No-* 约定表达，而不是干脆不发头。
    headers["X-User-Id" if uid else "X-No-User-Id"] = uid or "1"
    headers["X-Domain" if domain else "X-No-Department-Info"] = domain or "1"
    return headers


def _models_headers(token: str, uid: str, domain: str) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Origin": WB_BILLING_ORIGIN,
        "Referer": f"{WB_BILLING_ORIGIN}/",
        "User-Agent": WB_USER_AGENT,
    }
    if uid:
        headers["X-User-Id"] = uid
    if domain:
        headers["X-Domain"] = domain
    return headers


# ---------- 协议：可用性与请求转换 ----------

PROTOCOL_CHAT = "chat"
PROTOCOL_RESPONSES = "responses"
PROTOCOL_MESSAGES = "messages"

#: 每个协议在哪些 scope 上可用（上游原生支持，或网关能合成出来）。
#: 实测（2026-10-20，真实账号）：WorkBuddy 上游原生只有 `/v2/chat/completions` 与
#: `/v1/messages` 两条路由，`/v1/responses` 是 404 —— 所以 Responses 由网关合成。
#: 将来接 Trae 时把它的条目标掉，路由层就会如实返回 501。
PROTOCOL_SCOPES: dict[str, tuple[str, ...]] = {
    PROTOCOL_CHAT: CHAT_SCOPES,
    PROTOCOL_RESPONSES: (WB_SCOPE,),
    PROTOCOL_MESSAGES: (WB_SCOPE,),
}

_PROTOCOL_LABELS = {
    PROTOCOL_RESPONSES: "OpenAI Responses（/v1/responses）",
    PROTOCOL_MESSAGES: "Anthropic Messages（/v1/messages）",
}


class ProtocolNotSupported(GatewayRouteError):
    """上游/该 scope 不提供这个协议。路由层翻成 501，绝不静默降级。"""


def unsupported_reason(protocol: str, scope: str) -> str:
    """该 scope 不支持该协议时给出可读原因；支持则返回空串。"""
    if scope in PROTOCOL_SCOPES.get(protocol, ()):
        return ""
    label = _PROTOCOL_LABELS.get(protocol, protocol)
    return (
        f"{scope} 的上游不提供 {label}，网关不会静默降级到 chat；"
        "请改用 /v1/chat/completions"
    )


def prepare_relay(protocol: str, payload: dict[str, Any], *, scope: str) -> dict[str, Any]:
    """统一入口的第一步：协议可用性 + 请求体转成上游要的形状。

    `key.allows(model)` 这类鉴权校验留在路由层（它只认 `ApiKeyRecord`）。
    """
    reason = unsupported_reason(protocol, scope)
    if reason:
        raise ProtocolNotSupported(reason)
    return upstream_body(protocol, payload)


def upstream_body(protocol: str, payload: dict[str, Any]) -> dict[str, Any]:
    """入站协议请求 → 上游请求体。

    chat 与 messages 上游都原生认，只改模型名（由 `*_target` 做）；只有 responses
    需要真正转换 —— 上游没有这条路由，是网关按 chat 合成出来的。
    """
    if protocol in (PROTOCOL_CHAT, PROTOCOL_MESSAGES):
        return dict(payload)
    if protocol == PROTOCOL_RESPONSES:
        return responses_to_chat(payload)
    raise GatewayRouteError(f"未知协议 {protocol}")


def _content_text(content: Any) -> str:
    """把入站 content 归一成纯文本：字符串原样，块数组只取其中的文本片段。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and isinstance(block.get("text"), str):
            parts.append(block["text"])
    return "".join(parts)


# ----- Responses → chat -----


def responses_to_chat(payload: dict[str, Any]) -> dict[str, Any]:
    """Responses 请求 → chat 请求。

    只搬上游认得的字段。`previous_response_id` / `store` / `include` /
    `background` / `stream_options` 以及文件、音频、reasoning 输入项**忽略而非报错**
    —— 但本网关没有服务端会话状态，依赖 `previous_response_id` 续写的调用会丢上下文，
    这条边界必须在接入说明里写明。
    """
    messages: list[dict[str, Any]] = []
    instructions = _content_text(payload.get("instructions"))
    if instructions:
        messages.append({"role": "system", "content": instructions})
    messages.extend(_responses_input(payload.get("input")))
    if not messages:
        raise GatewayRouteError("Responses 请求里没有可转换的 input")

    chat: dict[str, Any] = {"messages": messages}
    for field in ("temperature", "top_p"):
        if payload.get(field) is not None:
            chat[field] = payload[field]
    if payload.get("max_output_tokens") is not None:
        chat["max_tokens"] = payload["max_output_tokens"]
    if isinstance(payload.get("parallel_tool_calls"), bool):
        chat["parallel_tool_calls"] = payload["parallel_tool_calls"]
    tools = _responses_tools(payload.get("tools"))
    if tools:
        chat["tools"] = tools
    choice = _responses_tool_choice(payload.get("tool_choice"))
    if choice is not None:
        chat["tool_choice"] = choice
    return chat


def _responses_input(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, str):
        return [{"role": "user", "content": raw}] if raw else []
    if not isinstance(raw, list):
        return []
    messages: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind in (None, "message"):
            text = _content_text(item.get("content"))
            if text:
                messages.append({"role": str(item.get("role") or "user"), "content": text})
        elif kind == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": str(item.get("call_id") or item.get("id") or ""),
                            "type": "function",
                            "function": {
                                "name": str(item.get("name") or ""),
                                "arguments": str(item.get("arguments") or ""),
                            },
                        }
                    ],
                }
            )
        elif kind == "function_call_output":
            output = item.get("output")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": _content_text(output) or ("" if output is None else str(output)),
                }
            )
    return messages


def _responses_tools(raw: Any) -> list[dict[str, Any]]:
    """扁平 `tools` → chat 的 `{type:function, function:{…}}`。"""
    if not isinstance(raw, list):
        return []
    tools: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "")
        if not name:
            continue
        schema = item.get("parameters")
        tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": str(item.get("description") or ""),
                    "parameters": schema
                    if isinstance(schema, dict)
                    else {"type": "object", "properties": {}},
                },
            }
        )
    return tools


def _responses_tool_choice(raw: Any) -> Any:
    if isinstance(raw, str) and raw in ("auto", "none", "required"):
        return raw
    if isinstance(raw, dict):
        if raw.get("type") == "function" and raw.get("name"):
            return {"type": "function", "function": {"name": str(raw["name"])}}
        if raw.get("type") in ("auto", "none", "required"):
            return raw["type"]
    return None


# ---------- 协议：出站事件转换 ----------


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _chat_delta(chunk: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """从一个 chat SSE 分片里取出（正文增量, 工具调用增量列表）。"""
    text = ""
    calls: list[dict[str, Any]] = []
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return text, calls
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            continue
        if isinstance(delta.get("content"), str):
            text += delta["content"]
        for call in delta.get("tool_calls") or []:
            if isinstance(call, dict):
                calls.append(call)
    return text, calls


def _chat_meta(chunk: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    """从一个 chat SSE 分片里取出（finish_reason, usage）。"""
    reason = ""
    usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else None
    choices = chunk.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if isinstance(choice, dict) and choice.get("finish_reason"):
                reason = str(choice["finish_reason"])
    return reason, usage


def _finish_reason(completion: dict[str, Any]) -> str:
    choices = completion.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        return str(choices[0].get("finish_reason") or "stop")
    return "stop"


def _completion_message(completion: dict[str, Any]) -> dict[str, Any]:
    choices = completion.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict):
            return message
    return {}


class ResponsesStream:
    """上游 chat SSE → Responses API 的 SSE 事件（有状态）。

    覆盖范围就是接入说明里写明的子集：正文、函数调用、usage。缺项按「空但是合法」
    输出，让客户端拿到结构完整的 `response`，而不是半截流。
    """

    def __init__(self, model: str) -> None:
        self._model = model
        self._id = f"resp_{token_hex(12)}"
        self._created = int(time.time())
        self._text: list[str] = []
        self._message_item: str | None = None
        self._message_index = 0
        self._outputs: dict[int, dict[str, Any]] = {}
        self._tools: dict[int, dict[str, Any]] = {}
        self._next_index = 0
        self._finish = "stop"
        self._usage: dict[str, Any] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        }

    def start(self) -> list[str]:
        return [
            _sse(
                "response.created",
                {"type": "response.created", "response": self._skeleton("in_progress")},
            )
        ]

    def feed(self, chunk: dict[str, Any]) -> list[str]:
        frames: list[str] = []
        text, calls = _chat_delta(chunk)
        if text:
            frames.extend(self._open_message())
            self._text.append(text)
            frames.append(
                _sse(
                    "response.output_text.delta",
                    {
                        "type": "response.output_text.delta",
                        "item_id": self._message_item,
                        "output_index": self._message_index,
                        "content_index": 0,
                        "delta": text,
                    },
                )
            )
        for call in calls:
            frames.extend(self._tool_delta(call))
        reason, usage = _chat_meta(chunk)
        if reason:
            self._finish = reason
        if usage:
            prompt, completion = usage_tokens({"usage": usage})
            self._usage = {
                "input_tokens": prompt,
                "output_tokens": completion,
                "total_tokens": prompt + completion,
            }
        return frames

    def finish(self) -> list[str]:
        frames = self._close_message() + self._close_tools()
        if not self._outputs:
            frames.extend(self._open_message())
            frames.extend(self._close_message())
        frames.append(
            _sse(
                "response.completed",
                {"type": "response.completed", "response": self._skeleton("completed")},
            )
        )
        return frames

    # ----- 内部 -----

    def _skeleton(self, status: str) -> dict[str, Any]:
        return {
            "id": self._id,
            "object": "response",
            "created_at": self._created,
            "status": status,
            "model": self._model,
            "output": [self._outputs[index] for index in sorted(self._outputs)],
            "usage": self._usage if status == "completed" else None,
        }

    def _open_message(self) -> list[str]:
        if self._message_item is not None:
            return []
        self._message_item = f"msg_{token_hex(12)}"
        self._message_index = self._next_index
        self._next_index += 1
        return [
            _sse(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": self._message_index,
                    "item": {
                        "id": self._message_item,
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                },
            ),
            _sse(
                "response.content_part.added",
                {
                    "type": "response.content_part.added",
                    "item_id": self._message_item,
                    "output_index": self._message_index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            ),
        ]

    def _close_message(self) -> list[str]:
        item_id = self._message_item
        if item_id is None:
            return []
        index = self._message_index
        body = "".join(self._text)
        self._text = []
        self._message_item = None
        part = {"type": "output_text", "text": body, "annotations": []}
        item = {
            "id": item_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [part],
        }
        self._outputs[index] = item
        return [
            _sse(
                "response.output_text.done",
                {
                    "type": "response.output_text.done",
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "text": body,
                },
            ),
            _sse(
                "response.content_part.done",
                {
                    "type": "response.content_part.done",
                    "item_id": item_id,
                    "output_index": index,
                    "content_index": 0,
                    "part": part,
                },
            ),
            _sse(
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": index, "item": item},
            ),
        ]

    def _tool_delta(self, call: dict[str, Any]) -> list[str]:
        raw_index = int(call.get("index") or 0)
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        name = str(function.get("name") or "")
        arguments = function.get("arguments")
        slot = self._tools.get(raw_index)
        frames: list[str] = []
        if slot is None:
            self._tools[raw_index] = slot = {
                "index": self._next_index,
                "item": f"fc_{token_hex(8)}",
                "call_id": str(call.get("id") or "") or f"call_{token_hex(8)}",
                "name": name,
                "arguments": "",
                "sent": False,
                "closed": False,
            }
            self._next_index += 1
        elif name and not slot["name"]:
            slot["name"] = name
        frames.extend(self._open_tool(slot))
        if isinstance(arguments, str) and arguments:
            slot["arguments"] += arguments
            frames.append(
                _sse(
                    "response.function_call_arguments.delta",
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": slot["item"],
                        "output_index": slot["index"],
                        "delta": arguments,
                    },
                )
            )
        return frames

    def _open_tool(self, slot: dict[str, Any]) -> list[str]:
        """`response.output_item.added` 里要带函数名，所以拿到名字再发这一帧。"""
        if slot["sent"] or not slot["name"]:
            return []
        slot["sent"] = True
        return [
            _sse(
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": slot["index"],
                    "item": {
                        "id": slot["item"],
                        "type": "function_call",
                        "status": "in_progress",
                        "call_id": slot["call_id"],
                        "name": slot["name"],
                        "arguments": "",
                    },
                },
            )
        ]

    def _close_tools(self) -> list[str]:
        frames: list[str] = []
        for raw_index in sorted(self._tools):
            slot = self._tools[raw_index]
            if slot["closed"]:
                continue
            frames.extend(self._open_tool(slot))
            if not slot["sent"]:
                continue
            slot["closed"] = True
            item = {
                "id": slot["item"],
                "type": "function_call",
                "status": "completed",
                "call_id": slot["call_id"],
                "name": slot["name"],
                "arguments": slot["arguments"],
            }
            self._outputs[slot["index"]] = item
            frames.append(
                _sse(
                    "response.function_call_arguments.done",
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": slot["item"],
                        "output_index": slot["index"],
                        "arguments": slot["arguments"],
                    },
                )
            )
            frames.append(
                _sse(
                    "response.output_item.done",
                    {
                        "type": "response.output_item.done",
                        "output_index": slot["index"],
                        "item": item,
                    },
                )
            )
        return frames


# ----- 非流式：chat.completion → 入站协议对象 -----


def chat_completion_to_response(completion: dict[str, Any], *, model: str) -> dict[str, Any]:
    """`chat.completion` → Responses 的 `response` 对象。"""
    message = _completion_message(completion)
    prompt, completion_tokens = usage_tokens(completion)
    outputs: list[dict[str, Any]] = []
    text = message.get("content")
    if isinstance(text, str) and text:
        outputs.append(
            {
                "id": f"msg_{token_hex(12)}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    for call in message.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        function = call.get("function") if isinstance(call.get("function"), dict) else {}
        outputs.append(
            {
                "id": f"fc_{token_hex(8)}",
                "type": "function_call",
                "status": "completed",
                "call_id": str(call.get("id") or ""),
                "name": str(function.get("name") or ""),
                "arguments": str(function.get("arguments") or ""),
            }
        )
    return {
        "id": f"resp_{token_hex(12)}",
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": str(completion.get("model") or model),
        "output": outputs,
        "usage": {
            "input_tokens": prompt,
            "output_tokens": completion_tokens,
            "total_tokens": prompt + completion_tokens,
        },
    }

