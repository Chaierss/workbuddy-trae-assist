"""网关（P12）：配置 / Key / 选号 / 协议聚合 / OpenAI 兼容端点。

上游一律用 `httpx.MockTransport` 假造，测试不发真实网络请求。
"""

from __future__ import annotations

import base64
import json
import socket
from hashlib import sha256
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from chaierww.api.gateway import _prompt_preview, _session_hint, build_gateway_app
from chaierww.config import Settings
from chaierww.domain.credits import CreditInfo
from chaierww.domain.gateway import (
    LOG_KEEP,
    SCOPE_ALL,
    SCOPE_CAPABILITY,
    SCOPE_LIST,
    CatalogRefresher,
    GatewayConfig,
    GatewayError,
    GatewayKeyNotFound,
    GatewayStore,
)
from chaierww.domain.gateway_relay import (
    DEFAULT_SCOPE,
    PROTOCOL_MESSAGES,
    PROTOCOL_RESPONSES,
    CatalogModel,
    GatewayRelay,
    GatewayRouteError,
    ProtocolNotSupported,
    ResponsesStream,
    aggregate_sse,
    parse_catalog,
    prepare_relay,
    resolve_model,
    unsupported_reason,
    upstream_body,
)
from chaierww.main import create_app

SCOPE = "workbuddy-cn"

CATALOG_DOC = {
    "code": 0,
    "data": {
        "agents": [{"name": "cli", "models": ["deepseek-v3", "claude-4", "gone"]}],
        "models": [
            {
                "id": "deepseek-v3",
                "name": "DeepSeek V3",
                "maxInputTokens": 128000,
                "maxOutputTokens": 8192,
                "supportsImages": False,
                "credits": "x0.00",
                "tags": ["badge:限时免费:#FF0000"],
            },
            {
                "id": "claude-4",
                "name": "Claude 4",
                "maxInputTokens": 200000,
                "maxOutputTokens": 64000,
                "supportsImages": True,
            },
            {"id": "gone", "maxInputTokens": 1, "maxOutputTokens": 1, "disabled": True},
            {"id": "not-in-whitelist", "maxInputTokens": 1, "maxOutputTokens": 1},
        ],
    },
}


class _Running:
    """替身：让 `/v1/*` 认为网关正在监听（避免测试真起端口）。"""

    running = True
    error = None

    def apply(self, app_factory) -> None:  # noqa: ANN001 - 与 GatewayRuntime 同形
        return None

    def stop(self) -> None:
        return None


class _Recorder:
    """记录出站请求，并依次吐回预设响应。"""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.responses: list[httpx.Response] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        return httpx.Response(200, content=b"{}")

    def client_factory(self):
        handler = self.handler

        class _FakeAsyncClient(httpx.AsyncClient):
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                kwargs.pop("timeout", None)
                kwargs.pop("trust_env", None)
                super().__init__(transport=httpx.MockTransport(handler))

        return _FakeAsyncClient


@pytest.fixture
def recorder(monkeypatch) -> _Recorder:
    rec = _Recorder()
    monkeypatch.setattr(httpx, "AsyncClient", rec.client_factory())
    return rec


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(home=tmp_path))
    app.state.gateway_runtime = _Running()
    with TestClient(app) as test_client:
        yield test_client


def _seed(client: TestClient, external_id: str, name: str, token: str = "tok") -> str:
    result = client.app.state.repository.upsert(
        scope=SCOPE,
        external_id=external_id,
        display_name=name,
        credential={
            "format": "oauth-token",
            "accessToken": token,
            "uid": f"uid-{external_id}",
            "domain": "www.codebuddy.cn",
        },
    )
    return result.account.id


def _seed_encrypted(client: TestClient, external_id: str, name: str) -> str:
    """工作台的 DPAPI 信封态：本工具解不开，网关应跳过它。"""
    blob = json.dumps({"auth": {"accessToken": {"$wbEncrypted": "xxx"}}}).encode("utf-8")
    result = client.app.state.repository.upsert(
        scope=SCOPE,
        external_id=external_id,
        display_name=name,
        credential={
            "format": "raw-file",
            "encoding": "base64",
            "data": base64.b64encode(blob).decode("ascii"),
        },
    )
    return result.account.id


def _issue_key(client: TestClient, label: str = "测试客户端") -> str:
    response = client.post("/api/gateway/keys", json={"label": label})
    assert response.status_code == 200, response.text
    return response.json()["key"]


def _sse(*lines: str) -> bytes:
    return ("\n\n".join(lines) + "\n\n").encode("utf-8")


# ---------- 存储 / 配置 ----------


def test_config_defaults_to_disabled_loopback():
    config = GatewayConfig()
    assert config.enabled is False
    assert config.host == "127.0.0.1"
    config.validate()


def test_config_rejects_non_loopback_without_flag():
    with pytest.raises(GatewayError):
        GatewayConfig(host="0.0.0.0").validate()
    GatewayConfig(host="0.0.0.0", allow_non_loopback=True).validate()


def test_catalog_refresh_interval_validation():
    """自动刷新间隔：0（关闭）或 5..1440 分钟；4 / 1441 这类值直接拒。"""
    GatewayConfig(catalog_refresh_minutes=0).validate()
    GatewayConfig(catalog_refresh_minutes=5).validate()
    GatewayConfig(catalog_refresh_minutes=1440).validate()
    with pytest.raises(GatewayError):
        GatewayConfig(catalog_refresh_minutes=4).validate()
    with pytest.raises(GatewayError):
        GatewayConfig(catalog_refresh_minutes=1441).validate()


def test_catalog_refresher_only_fires_when_due():
    """间隔 0 = 不抓；打开或改间隔立即抓一次，之后按间隔排下一轮。"""
    state = {"minutes": 0, "now": 1000.0, "calls": 0}
    refresher = CatalogRefresher(
        lambda: state["minutes"],
        lambda: state.__setitem__("calls", state["calls"] + 1),
        clock=lambda: state["now"],
    )

    refresher.tick()  # 关闭：一次都不抓
    assert state["calls"] == 0

    state["minutes"] = 5
    refresher.tick()  # 刚打开：立刻抓一次
    assert state["calls"] == 1

    state["now"] += 60
    refresher.tick()  # 才过 1 分钟：还没到点
    assert state["calls"] == 1

    state["now"] += 4 * 60
    refresher.tick()  # 满 5 分钟：再抓一次
    assert state["calls"] == 2

    state["minutes"] = 0
    refresher.tick()  # 关掉后不再抓
    assert state["calls"] == 2


def test_catalog_refresher_survives_fetcher_failure():
    """单次抓取失败只记日志、不退出线程，也不会因此疯狂重试。"""
    state = {"now": 0.0, "calls": 0}

    def boom() -> None:
        state["calls"] += 1
        raise RuntimeError("upstream down")

    refresher = CatalogRefresher(lambda: 5, boom, clock=lambda: state["now"])
    refresher.tick()  # 不抛出
    assert state["calls"] == 1
    state["now"] += 60
    refresher.tick()  # 未到点，不重试
    assert state["calls"] == 1


def test_store_key_lifecycle(tmp_path):
    store = GatewayStore(tmp_path)
    token, record = store.issue("我的客户端")
    assert token.startswith("sk-")
    assert not token.startswith("sk-wb-")
    assert "…" in record.payload()["mask"]
    assert token not in json.dumps(store.keys()[0].payload(), ensure_ascii=False)

    assert store.authenticate(token) is not None
    assert store.authenticate("sk-deadbeef") is None
    assert store.authenticate("sk-wb-deadbeef") is None
    assert store.authenticate(None) is None

    store.revoke(record.id)
    assert store.authenticate(token) is None
    with pytest.raises(GatewayError):
        store.revoke("不存在")


def test_legacy_sk_wb_key_still_authenticates(tmp_path):
    """老格式 Key（`sk-wb-…`）不能因为前缀改成 `sk-` 就失效：客户端里早配好了。"""
    legacy = "sk-wb-" + "a" * 32
    record = {
        "id": "legacy",
        "label": "老 Key",
        "prefix": legacy[:11],
        "suffix": legacy[-4:],
        "digest": sha256(legacy.encode("utf-8")).hexdigest(),
        "createdAt": "2026-10-20T00:00:00+00:00",
        "lastUsedAt": None,
        "revokedAt": None,
        "requests": 0,
        "tokens": 0,
    }
    (tmp_path / "gateway-keys.json").write_text(json.dumps([record]), encoding="utf-8")

    store = GatewayStore(tmp_path)
    assert store.authenticate(legacy) is not None
    assert store.authenticate("sk-" + "b" * 32) is None


def test_store_logs_and_stats(tmp_path):
    store = GatewayStore(tmp_path)
    store.log({"ok": True, "promptTokens": 10, "completionTokens": 5})
    store.log({"ok": False, "error": "boom"})
    assert store.stats() == {
        "requests": 2,
        "errors": 1,
        "promptTokens": 10,
        "completionTokens": 5,
        "totalTokens": 15,
        "lastRequestAt": store.logs(1)[0]["at"],
    }
    assert len(store.logs(10)) == 2
    assert store.clear_logs() == 2
    assert store.logs(10) == []


def test_store_logs_filter_by_key_and_session(tmp_path):
    """日志筛选是内存里的等值过滤：先过滤再取末 N 条，返回最近的命中项。"""
    store = GatewayStore(tmp_path)
    store.log({"ok": True, "keyId": "k1", "sessionId": "s1"})
    store.log({"ok": True, "keyId": "k2", "sessionId": "s1"})
    store.log({"ok": True, "keyId": "k1", "sessionId": "s2"})

    # 新的在前
    assert [item["sessionId"] for item in store.logs(key_id="k1")] == ["s2", "s1"]
    assert [item["keyId"] for item in store.logs(session_id="s1")] == ["k2", "k1"]
    assert len(store.logs(key_id="k1", session_id="s1")) == 1
    assert store.logs(key_id="nope") == []
    # 不传筛选 = 全部（没有 keyId / sessionId 的老日志一样照常返回）
    assert len(store.logs()) == 3


def test_store_key_usage_splits_total_and_sample(tmp_path):
    """单 Key 用量：累计计数来自 Key 记录，分解只覆盖当前保留的日志，两者都要标清口径。"""
    store = GatewayStore(tmp_path)
    _, record = store.issue("用量测试")
    store.record_use(record.id, tokens=100)
    store.record_use(record.id, tokens=50)
    store.log(
        {"ok": True, "keyId": record.id, "model": "glm-5.2", "totalTokens": 100, "durationMs": 20}
    )
    store.log(
        {"ok": False, "keyId": record.id, "model": "glm-5.2", "totalTokens": 50, "durationMs": 40}
    )
    store.log({"ok": True, "keyId": "other", "model": "kimi-k2", "totalTokens": 999})
    store.log({"ok": True, "keyId": record.id, "kind": "probe"})

    usage = store.key_usage(record.id)
    assert usage["key"]["requests"] == 2
    assert usage["key"]["tokens"] == 150
    assert usage["sample"]["scope"] == LOG_KEEP
    assert usage["sample"]["requests"] == 2
    assert usage["sample"]["ok"] == 1
    assert usage["sample"]["failed"] == 1
    assert usage["sample"]["tokens"] == 150
    assert usage["sample"]["avgDurationMs"] == 30
    assert usage["byModel"] == [
        {"model": "glm-5.2", "requests": 2, "tokens": 150, "errors": 1}
    ]
    assert [item["ok"] for item in usage["recent"]] == [False, True]

    with pytest.raises(GatewayKeyNotFound):
        store.key_usage("nope")


def test_key_usage_endpoint_reports_404_for_unknown_key(client):
    assert client.get("/api/gateway/keys/nope/usage").status_code == 404
    key = _issue_key(client)
    key_id = client.get("/api/gateway/keys").json()["keys"][0]["id"]
    body = client.get(f"/api/gateway/keys/{key_id}/usage").json()
    assert body["key"]["id"] == key_id
    assert body["sample"]["requests"] == 0
    assert key.startswith("sk-")


def _request(headers: dict[str, str] | None = None) -> Request:
    """造一个只带请求头的最小 `Request`，够 `_session_hint` 读 headers 用。"""
    raw = [(name.lower().encode("latin-1"), value.encode("latin-1")) for name, value in
           (headers or {}).items()]
    return Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                    "headers": raw})


def test_session_hint_takes_client_supplied_id_only():
    """会话标识只来自客户端：请求头 → 请求体键 → metadata → user → 空串。

    网关**不生成**会话 id：取不到就返回空串（界面显示为「未提供」），绝不猜测。
    """
    assert _session_hint(_request({"x-session-id": "hdr"}), {"session_id": "body"}) == "hdr"
    assert _session_hint(_request({"openai-conversation-id": "h"}), {}) == "h"
    assert _session_hint(_request(), {"conversation_id": "body"}) == "body"
    assert _session_hint(_request(), {"metadata": {"session_id": "meta"}}) == "meta"
    assert _session_hint(_request(), {"user": "u-9"}) == "u-9"
    assert _session_hint(_request(), {}) == ""
    assert _session_hint(_request(), None) == ""
    # 非字符串值不当成标识（`session_id: 123` 是猜不出来的）
    assert _session_hint(_request(), {"session_id": 123}) == ""
    # 超长截断，免得日志被撑爆
    assert len(_session_hint(_request(), {"user": "x" * 300})) == 120


def test_prompt_preview_takes_last_user_message_only():
    """请求内容摘要：只取最后一条 user 消息文本，归一成单行、截断；取不到返回空串。"""
    # 多轮：取最后一条 user（不是 system，也不是中间那轮）
    body = {
        "messages": [
            {"role": "system", "content": "你是助手"},
            {"role": "user", "content": "第一问"},
            {"role": "assistant", "content": "第一答"},
            {"role": "user", "content": "  第二问\n换行  "},
        ]
    }
    assert _prompt_preview(body) == "第二问 换行"
    # 块数组（Anthropic 风格）只取 text 片段
    assert (
        _prompt_preview(
            {"messages": [{"role": "user", "content": [{"type": "text", "text": "块文本"}]}]}
        )
        == "块文本"
    )
    # 没有 user 消息 / 空消息 / 非对象 → 空串
    assert _prompt_preview({"messages": [{"role": "assistant", "content": "答"}]}) == ""
    assert _prompt_preview({"messages": []}) == ""
    assert _prompt_preview(None) == ""
    # 超长截断到 200 字符
    assert len(_prompt_preview({"messages": [{"role": "user", "content": "x" * 500}]})) == 200


# ---------- 协议纯函数 ----------


def test_resolve_model_uses_bare_upstream_name():
    assert resolve_model("deepseek-v4.1-flash") == (SCOPE, "deepseek-v4.1-flash")
    assert resolve_model("  glm-5.3  ") == (DEFAULT_SCOPE, "glm-5.3")
    with pytest.raises(GatewayRouteError):
        resolve_model("wb/deepseek-v3")
    with pytest.raises(GatewayRouteError):
        resolve_model("")


def test_parse_catalog_uses_cli_whitelist():
    models = parse_catalog(CATALOG_DOC)
    assert [model.id for model in models] == ["deepseek-v3", "claude-4"]
    assert models[0].free is True
    assert models[0].badges == ("限时免费",)
    assert models[1].supports_images is True
    assert models[1].context_window == 200000


def test_parse_catalog_without_cli_agent_is_error():
    from chaierww.domain.gateway_relay import GatewayUpstreamError

    document = {"data": {"models": [{"id": "a", "maxInputTokens": 1, "maxOutputTokens": 1}]}}
    with pytest.raises(GatewayUpstreamError):
        parse_catalog(document)


def test_aggregate_sse_merges_content_and_usage():
    payload = aggregate_sse(
        "\n".join(
            [
                'data: {"id":"c1","created":1,"model":"m",'
                '"choices":[{"index":0,"delta":{"role":"assistant"}}]}',
                'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"你好"}}]}',
                'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"世界"}}],'
                '"usage":{"prompt_tokens":7,"completion_tokens":2}}',
                "data: [DONE]",
            ]
        )
    )
    assert payload is not None
    assert payload["id"] == "c1"
    assert payload["object"] == "chat.completion"
    assert payload["choices"][0]["message"]["content"] == "你好世界"
    assert payload["usage"]["prompt_tokens"] == 7


def test_aggregate_sse_merges_tool_calls_by_index():
    payload = aggregate_sse(
        "\n".join(
            [
                'data: {"id":"c","choices":[{"index":0,"delta":{"tool_calls":'
                '[{"index":0,"id":"call_1","function":{"name":"f","arguments":"{\\"a\\":"}}]}}]}',
                'data: {"id":"c","choices":[{"index":0,"delta":{"tool_calls":'
                '[{"index":0,"function":{"arguments":"1}"}}]},"finish_reason":"tool_calls"}]}',
            ]
        )
    )
    assert payload is not None
    call = payload["choices"][0]["message"]["tool_calls"][0]
    assert call["id"] == "call_1"
    assert call["function"] == {"name": "f", "arguments": '{"a":1}'}
    assert payload["choices"][0]["finish_reason"] == "tool_calls"


# ---------- 选号 ----------


def test_pick_order_drops_undecryptable_accounts(client):
    """凭据解不开（DPAPI 信封）的账号是结构性不可用：直接剔除，不再白跑一次。"""
    relay: GatewayRelay = client.app.state.gateway_relay
    _seed(client, "u-1", "账号甲")
    encrypted_id = _seed_encrypted(client, "u-2", "信封账号")

    order = relay.pick_order(SCOPE)
    assert [account.display_name for account in order] == ["账号甲", "信封账号"]

    relay.report_failure(encrypted_id, reason="credential")
    assert relay.cooldown(encrypted_id) == 0
    assert [account.display_name for account in relay.pick_order(SCOPE)] == ["账号甲"]


def test_sticky_account_stays_first_after_success(client):
    relay: GatewayRelay = client.app.state.gateway_relay
    first = _seed(client, "u-1", "账号甲")
    _seed(client, "u-2", "账号乙")
    relay.report_success(SCOPE, first)
    order = relay.pick_order(SCOPE)
    assert order[0].id == first


def test_pool_exclude_drops_account_until_included(client):
    """排除是持久化的人工处置：被排除的账号不再参与转发，恢复到候选后才会回来。"""
    relay: GatewayRelay = client.app.state.gateway_relay
    first = _seed(client, "u-1", "账号甲")
    second = _seed(client, "u-2", "账号乙")

    response = client.put(f"/api/gateway/pool/accounts/{first}", json={"excluded": True})
    assert response.status_code == 200
    states = {item["id"]: item["state"] for item in response.json()["accounts"]}
    assert states[first] == "excluded"
    assert states[second] == "usable"
    assert [account.id for account in relay.pick_order(SCOPE)] == [second]

    client.put(f"/api/gateway/pool/accounts/{first}", json={"excluded": False})
    assert first in {account.id for account in relay.pick_order(SCOPE)}

    missing = client.put("/api/gateway/pool/accounts/不存在", json={"excluded": True})
    assert missing.status_code == 404


def test_pick_prefers_account_then_falls_back_when_unusable(client):
    """指定账号偏好：它可用时排第一；一旦进冷却/被排除就回退常规择优。"""
    relay: GatewayRelay = client.app.state.gateway_relay
    store: GatewayStore = client.app.state.gateway
    first = _seed(client, "u-1", "账号甲")
    second = _seed(client, "u-2", "账号乙")
    relay.report_success(SCOPE, first)  # 粘性在甲：常规择优会先甲，正好用来区分偏好

    pool = client.put(f"/api/gateway/pool/accounts/{second}", json={"preferred": True}).json()
    assert pool["preferred"] == second
    assert store.preferred_account() == second
    picked = relay.pick(SCOPE, preferred_account_id=store.preferred_account())
    assert picked is not None and picked.id == second

    # 偏好账号进冷却 → 不是首选，但仍在队尾等着被重试。
    relay.report_failure(second, status=429)
    order = relay.pick_order(SCOPE, preferred_account_id=second)
    assert order[0].id == first
    assert order[-1].id == second

    # 偏好账号被排除 → 直接不出现在候选里，回退到常规择优。
    client.put(f"/api/gateway/pool/accounts/{second}", json={"excluded": True})
    assert relay.pick(SCOPE, preferred_account_id=second).id == first


def test_pool_skips_credit_exhausted_account(client, monkeypatch):
    """积分耗尽（只读缓存口径）的账号被跳过；余额查不到时不误杀。"""
    relay: GatewayRelay = client.app.state.gateway_relay
    credits = client.app.state.credits
    first = _seed(client, "u-1", "账号甲")
    second = _seed(client, "u-2", "账号乙")
    account = relay.account(SCOPE, first)
    assert account is not None

    monkeypatch.setattr(
        credits, "_query_one", lambda item: CreditInfo(available=True, credits=0.0)
    )
    credits.query_many([account])

    pool = client.get("/api/gateway/pool").json()
    states = {item["id"]: item["state"] for item in pool["accounts"]}
    assert states[first] == "exhausted"
    assert [item.id for item in relay.pick_order(SCOPE)] == [second]

    # available=False（这种凭据查不了余额）按「没耗尽」处理，不能拿缺失数据误杀账号。
    monkeypatch.setattr(
        credits, "_query_one", lambda item: CreditInfo(available=False, error="查不了余额")
    )
    credits.invalidate(first)
    credits.query_many([account])
    assert first in {item.id for item in relay.pick_order(SCOPE)}


def test_probe_clears_cooldown_and_logs(client, recorder):
    """探测：成功即解冷却（队列里复活），结果进请求日志但不算业务请求。"""
    relay: GatewayRelay = client.app.state.gateway_relay
    account_id = _seed(client, "u-1", "账号甲")
    relay.report_failure(account_id, status=429)
    assert relay.cooldown(account_id) > 0

    recorder.responses.append(httpx.Response(200, json={"id": "msg_1", "content": []}))
    response = client.post(
        "/api/gateway/pool/probe", json={"accountId": account_id, "model": "deepseek-v3"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert relay.cooldown(account_id) == 0
    # 探测走的是上游原生 Anthropic 路由，1 个 token 就够。
    assert recorder.requests[0].url.path == "/v1/messages"

    logs = client.get("/api/gateway/logs").json()["logs"]
    assert logs[0]["kind"] == "probe"
    assert logs[0]["accountName"] == "账号甲"
    assert client.get("/api/gateway/status").json()["stats"]["requests"] == 0

    # 探测失败不清冷却，并且把上游的说明如实带回。
    relay.report_failure(account_id, status=429)
    recorder.responses.append(httpx.Response(401, json={"error": {"message": "token expired"}}))
    failed = client.post(
        "/api/gateway/pool/probe", json={"accountId": account_id, "model": "deepseek-v3"}
    ).json()
    assert failed["ok"] is False
    assert "token expired" in failed["error"]
    assert relay.cooldown(account_id) > 0

    assert client.post(
        "/api/gateway/pool/probe", json={"accountId": account_id, "model": " "}
    ).status_code == 400


# ---------- 界面接口 ----------


def test_status_reports_disabled_gateway(client):
    body = client.get("/api/gateway/status").json()
    assert body["enabled"] is False
    assert body["baseUrl"] == "http://127.0.0.1:8788"
    assert body["running"] is True  # 测试替身
    assert body["activeKeys"] == 0
    assert body["accounts"] == 0
    assert body["stats"]["requests"] == 0


def test_config_update_rejects_non_loopback(client):
    response = client.put("/api/gateway/config", json={"host": "0.0.0.0"})
    assert response.status_code == 400
    assert "回环" in response.json()["detail"]

    ok = client.put("/api/gateway/config", json={"allowNonLoopback": True, "host": "0.0.0.0"})
    assert ok.status_code == 200
    assert ok.json()["host"] == "0.0.0.0"
    assert ok.json()["loopbackOnly"] is False


def test_config_port_range_rejected(client):
    assert client.put("/api/gateway/config", json={"port": 80}).status_code == 400


def test_keys_are_revoked_then_deleted(client):
    """三态：有效 →（吊销）→ 已吊销 →（删除）→ 不存在。"""
    token = _issue_key(client)
    listed = client.get("/api/gateway/keys").json()["keys"]
    assert len(listed) == 1
    assert listed[0]["label"] == "测试客户端"
    assert listed[0]["active"] is True
    assert token not in json.dumps(listed)

    revoked = client.post(f"/api/gateway/keys/{listed[0]['id']}/revoke").json()["record"]
    assert revoked["active"] is False
    # 吊销后已不可用，但记录还在列表里
    assert client.get("/api/gateway/keys").json()["keys"][0]["active"] is False

    assert client.post("/api/gateway/keys/nope/revoke").status_code == 404
    assert client.post("/api/gateway/keys/nope/restore").status_code == 404
    assert client.delete("/api/gateway/keys/nope").status_code == 404

    deleted = client.delete(f"/api/gateway/keys/{listed[0]['id']}").json()["record"]
    assert deleted["id"] == listed[0]["id"]
    assert client.get("/api/gateway/keys").json()["keys"] == []


def test_delete_refuses_active_key(client):
    """未吊销的 Key 不许直接删：回 400 并写明原因，记录仍在。"""
    _issue_key(client)
    key_id = client.get("/api/gateway/keys").json()["keys"][0]["id"]

    response = client.delete(f"/api/gateway/keys/{key_id}")
    assert response.status_code == 400
    assert "吊销" in response.json()["detail"]
    assert len(client.get("/api/gateway/keys").json()["keys"]) == 1


def test_revoked_key_is_401_until_restored(client, recorder):
    """吊销后立刻 401；恢复后同一把 Key（明文没变）重新可用。"""
    token = _issue_key(client)
    key_id = client.get("/api/gateway/keys").json()["keys"][0]["id"]
    _seed(client, "u-1", "账号甲")

    assert client.post(f"/api/gateway/keys/{key_id}/revoke").status_code == 200
    refused = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "deepseek-v3"},
    )
    assert refused.status_code == 401

    restored = client.post(f"/api/gateway/keys/{key_id}/restore").json()["record"]
    assert restored["active"] is True

    recorder.responses.append(
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"好"}}],'
                '"usage":{"prompt_tokens":5,"completion_tokens":1}}',
                "data: [DONE]",
            ),
        )
    )
    allowed = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert allowed.status_code == 200, allowed.text


def test_restore_refuses_active_key(client):
    """有效 Key 无需恢复：回 400，而不是当成错误状态处理。"""
    _issue_key(client)
    key_id = client.get("/api/gateway/keys").json()["keys"][0]["id"]

    response = client.post(f"/api/gateway/keys/{key_id}/restore")
    assert response.status_code == 400
    assert "无需恢复" in response.json()["detail"]


def test_root_path_aliases_match_v1(client, recorder):
    """有些客户端把 base_url 当根地址、自己拼 `/models`；根路径别名必须与 `/v1` 等价。"""
    _seed(client, "u-1", "账号甲")
    recorder.responses.append(httpx.Response(200, json=CATALOG_DOC))
    token = _issue_key(client)

    app = build_gateway_app(
        client.app.state.gateway, client.app.state.gateway_relay, _Running()
    )
    with TestClient(app) as gateway:
        headers = {"Authorization": f"Bearer {token}"}
        root = gateway.get("/models", headers=headers)
        assert root.status_code == 200
        assert root.json() == gateway.get("/v1/models", headers=headers).json()

        recorder.responses.append(
            httpx.Response(
                200,
                json={
                    "id": "c",
                    "object": "chat.completion",
                    "model": "deepseek-v3",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                },
            )
        )
        chat = gateway.post(
            "/chat/completions",
            headers=headers,
            json={"model": "deepseek-v3", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert chat.status_code == 200
        assert chat.json()["choices"][0]["message"]["content"] == "ok"


def test_gateway_runtime_start_and_stop_on_real_port(tmp_path):
    """按下开关就真监听、关掉就真停 —— 用系统分配的空闲端口，不占用 8788。"""
    from chaierww.domain.gateway import GatewayRuntime

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    app = create_app(Settings(home=tmp_path))
    with TestClient(app):
        store, relay = app.state.gateway, app.state.gateway_relay
        runtime = GatewayRuntime(store)
        try:
            store.save_config(GatewayConfig(enabled=True, host="127.0.0.1", port=port))
            runtime.apply(lambda: build_gateway_app(store, relay, runtime))
            assert runtime.running is True
            assert runtime.error is None
            runtime.stop()
            assert runtime.running is False
        finally:
            runtime.stop()


# ---------- /v1 OpenAI 兼容接口 ----------


def test_v1_requires_api_key(client):
    response = client.post("/v1/chat/completions", json={"model": "deepseek-v3"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


def test_v1_rejects_model_name_with_prefix(client):
    """模型名就是上游原名；带了 `wb/`、`trae/` 这类前缀要立刻报错，不去撞上游 404。"""
    token = _issue_key(client)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "trae/gpt-4o"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"


def test_v1_models_lists_catalog_with_bare_ids(client, recorder):
    token = _issue_key(client)
    _seed(client, "u-1", "账号甲")
    recorder.responses.append(httpx.Response(200, json=CATALOG_DOC))

    body = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"}).json()
    # 对外不挂任何平台前缀，模型名就是上游原名
    assert [item["id"] for item in body["data"]] == ["deepseek-v3", "claude-4"]
    assert body["data"][0]["owned_by"] == "workbuddy"

    # 目录请求打到了正确的上游，并带上了客户端指纹头
    outbound = recorder.requests[0]
    assert str(outbound.url) == "https://copilot.tencent.com/console/enterprises/personal/models"
    assert outbound.headers["user-agent"] == "CLI/2.63.2 CodeBuddy/2.63.2"
    assert outbound.headers["authorization"] == "Bearer tok"

    # 第二次走缓存，不再发网络请求
    assert len(recorder.requests) == 1
    client.get("/v1/models", headers={"Authorization": f"Bearer {token}"})


def test_models_refresh_writes_through_to_status(client, recorder):
    """「重新获取」必须把结果写回缓存，否则界面状态永远显示「未获取」。"""
    _seed(client, "u-1", "账号甲")
    recorder.responses.append(httpx.Response(200, json=CATALOG_DOC))

    refreshed = client.post("/api/gateway/models/refresh").json()
    assert [model["id"] for model in refreshed["models"]] == ["deepseek-v3", "claude-4"]

    catalog = client.get("/api/gateway/status").json()["catalog"]
    assert catalog["fresh"] is True
    assert [model["id"] for model in catalog["models"]] == ["deepseek-v3", "claude-4"]


def test_v1_chat_aggregates_sse_when_client_asks_non_stream(client, recorder):
    token = _issue_key(client)
    _seed(client, "u-1", "账号甲")
    recorder.responses.append(
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                'data: {"id":"c1","created":1,"model":"deepseek-v3",'
                '"choices":[{"index":0,"delta":{"role":"assistant"}}]}',
                'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"你好"}}]}',
                'data: {"id":"c1","choices":[{"index":0,"delta":{"content":"！"}}],'
                '"usage":{"prompt_tokens":11,"completion_tokens":3}}',
                "data: [DONE]",
            ),
        )
    )

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": "deepseek-v3",
            "stream": False,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["choices"][0]["message"]["content"] == "你好！"
    assert body["usage"]["prompt_tokens"] == 11

    # 出站请求：上游 model 被改写成裸模型名，chat 指纹头齐全
    outbound = recorder.requests[0]
    payload = json.loads(outbound.content)
    assert payload["model"] == "deepseek-v3"
    # 上游只支持流式：客户端要非流式，出站也必须带 stream=true，由我们聚合。
    assert payload["stream"] is True
    assert outbound.headers["x-codebuddy-request"] == "1"
    assert outbound.headers["x-agent-purpose"] == "conversation"
    assert outbound.headers["x-product"] == "SaaS"
    assert "refresh" not in json.dumps(dict(outbound.headers)).lower()

    logs = client.get("/api/gateway/logs").json()["logs"]
    assert logs[0]["ok"] is True
    assert logs[0]["model"] == "deepseek-v3"
    assert logs[0]["requestedModel"] == "deepseek-v3"
    assert logs[0]["totalTokens"] == 14
    assert logs[0]["accountName"] == "账号甲"
    assert logs[0]["promptPreview"] == "hi"

    stats = client.get("/api/gateway/status").json()["stats"]
    assert stats["requests"] == 1
    assert stats["totalTokens"] == 14


def test_v1_chat_streams_sse_and_logs_tokens(client, recorder):
    token = _issue_key(client)
    _seed(client, "u-1", "账号甲")
    recorder.responses.append(
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                'data: {"id":"c2","choices":[{"index":0,"delta":{"content":"流式"}}]}',
                'data: {"id":"c2","choices":[{"index":0,"delta":{},'
                '"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":6}}',
                "data: [DONE]",
            ),
        )
    )

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "deepseek-v3", "stream": True, "messages": []},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "流式" in response.text

    logs = client.get("/api/gateway/logs").json()["logs"]
    assert logs[0]["stream"] is True
    assert logs[0]["totalTokens"] == 10
    assert logs[0]["promptTokens"] == 4


def test_v1_logs_capture_client_session_and_are_filterable(client, recorder):
    """请求日志要记下客户端传入的会话标识，并支持按 Key / 会话筛。"""
    token = _issue_key(client)
    _seed(client, "u-1", "账号甲")
    recorder.responses.append(
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                'data: {"choices":[{"index":0,"delta":{"content":"好"}}],'
                '"usage":{"prompt_tokens":5,"completion_tokens":1}}',
                "data: [DONE]",
            ),
        )
    )

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}", "X-Session-Id": "sess-42"},
        json={"model": "deepseek-v3", "messages": []},
    )
    assert response.status_code == 200, response.text

    key_id = client.get("/api/gateway/keys").json()["keys"][0]["id"]
    by_session = client.get("/api/gateway/logs", params={"sessionId": "sess-42"}).json()["logs"]
    assert len(by_session) == 1
    assert by_session[0]["sessionId"] == "sess-42"
    assert by_session[0]["keyId"] == key_id

    # 按 Key 也能筛到同一笔；不相干的会话标识筛不出东西。
    assert len(client.get("/api/gateway/logs", params={"keyId": key_id}).json()["logs"]) == 1
    assert client.get("/api/gateway/logs", params={"sessionId": "nope"}).json()["logs"] == []


def test_v1_chat_hops_to_next_account_on_401(client, recorder):
    token = _issue_key(client)
    _seed(client, "u-1", "账号甲")
    _seed(client, "u-2", "账号乙")
    recorder.responses.append(
        httpx.Response(401, json={"error": {"message": "token expired"}})
    )
    recorder.responses.append(
        httpx.Response(
            200,
            headers={"content-type": "application/json"},
            json={
                "id": "c3",
                "object": "chat.completion",
                "model": "deepseek-v3",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "deepseek-v3", "messages": []},
    )
    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "ok"
    assert len(recorder.requests) == 2

    logs = client.get("/api/gateway/logs").json()["logs"]
    assert logs[0]["ok"] is True
    assert logs[0]["hops"] == 1
    assert len(logs[0]["skipped"]) == 1


def test_v1_chat_reports_400_without_retrying(client, recorder):
    token = _issue_key(client)
    _seed(client, "u-1", "账号甲")
    _seed(client, "u-2", "账号乙")
    recorder.responses.append(httpx.Response(400, json={"error": {"message": "model not allowed"}}))

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "deepseek-v3", "messages": []},
    )
    assert response.status_code == 400
    assert "model not allowed" in response.json()["error"]["message"]
    assert len(recorder.requests) == 1  # 换号也没用，别白跑


def test_v1_chat_503_when_pool_is_empty(client):
    token = _issue_key(client)
    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "deepseek-v3", "messages": []},
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "no_account"


def test_v1_chat_503_when_gateway_disabled(tmp_path, recorder):
    app = create_app(Settings(home=tmp_path))
    with TestClient(app) as raw:
        store: GatewayStore = raw.app.state.gateway
        token, _ = store.issue("t")
        _seed(raw, "u-1", "账号甲")
        response = raw.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": "deepseek-v3", "messages": []},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "gateway_disabled"


# ---------- 模型能力标注 / 用量看板 / Key 限模型 ----------


def test_parse_catalog_exposes_abilities():
    models = {model.id: model for model in parse_catalog(CATALOG_DOC)}
    free = models["deepseek-v3"]
    assert free.max_output_tokens == 8192
    assert free.credits == "x0.00"
    assert free.free is True

    paid = models["claude-4"]
    assert paid.max_output_tokens == 64000
    assert paid.credits == ""
    assert paid.free is False


def test_status_models_expose_abilities(client, recorder):
    _seed(client, "u-1", "账号甲")
    recorder.responses.append(httpx.Response(200, json=CATALOG_DOC))
    client.post("/api/gateway/models/refresh")

    models = client.get("/api/gateway/status").json()["catalog"]["models"]
    deepseek = next(item for item in models if item["id"] == "deepseek-v3")
    assert deepseek["maxOutputTokens"] == 8192
    assert deepseek["credits"] == "x0.00"
    assert deepseek["free"] is True


def test_store_breakdown_from_logs(tmp_path):
    store = GatewayStore(tmp_path)
    store.log(
        {
            "ok": True,
            "model": "deepseek-v3",
            "accountId": "a1",
            "accountName": "账号甲",
            "keyId": "k1",
            "keyLabel": "Cursor",
            "totalTokens": 10,
            "hops": 2,
            "durationMs": 120,
        }
    )
    store.log(
        {
            "ok": False,
            "model": "deepseek-v3",
            "accountId": "a2",
            "accountName": "账号乙",
            "keyId": "k1",
            "keyLabel": "Cursor",
            "totalTokens": 0,
            "hops": 1,
            "durationMs": 80,
            "error": "boom",
        }
    )
    store.log(
        {
            "ok": True,
            "model": "claude-4",
            "accountId": "a1",
            "accountName": "账号甲",
            "keyId": "k2",
            "keyLabel": "VS Code",
            "totalTokens": 7,
            "hops": 1,
            "durationMs": 30,
        }
    )
    board = store.breakdown()

    assert board["scope"] == 200
    assert board["total"] == 3
    assert board["byModel"][0]["model"] == "deepseek-v3"
    assert board["byModel"][0]["requests"] == 2
    assert board["byModel"][0]["tokens"] == 10
    assert board["byModel"][0]["errors"] == 1

    by_account = {item["accountId"]: item for item in board["byAccount"]}
    assert by_account["a1"]["ok"] == 2
    assert by_account["a1"]["hops"] == 3
    assert by_account["a2"]["failed"] == 1

    # 按 Key 分：采样口径（每把 Key 各多少），与 Key 记录上的累计计数器无关。
    by_key = {item["keyId"]: item for item in board["byKey"]}
    assert set(by_key) == {"k1", "k2"}
    assert by_key["k1"]["keyLabel"] == "Cursor"
    assert by_key["k1"]["requests"] == 2
    assert by_key["k1"]["tokens"] == 10
    assert by_key["k1"]["ok"] == 1
    assert by_key["k1"]["failed"] == 1
    assert by_key["k2"]["requests"] == 1
    assert by_key["k2"]["failed"] == 0
    # 请求多的排在前面
    assert board["byKey"][0]["keyId"] == "k1"

    assert board["successRate"] == round(2 / 3, 4)
    assert board["avgDurationMs"] == 77
    assert board["totalHops"] == 4
    assert board["series"]  # at 会被截成小时桶


def test_status_includes_breakdown(client):
    board = client.get("/api/gateway/status").json()["stats"]["breakdown"]
    assert board["scope"] == 200
    assert board["byModel"] == []
    assert board["byKey"] == []
    assert board["successRate"] is None


def test_issue_key_with_model_whitelist(tmp_path):
    store = GatewayStore(tmp_path)
    _, record = store.issue("限定模型", ["deepseek-v3", "claude-4", "deepseek-v3"])
    # 去重且保序
    assert record.models == ("deepseek-v3", "claude-4")
    assert record.allows("deepseek-v3") is True
    assert record.allows("other-model") is False

    _, open_key = store.issue("不限模型")
    assert open_key.models == ()
    assert open_key.allows("anything") is True

    # 落盘 → 重新加载仍带白名单
    reloaded = {item.id: item for item in GatewayStore(tmp_path).keys()}  # noqa: SIM118 - 非 dict
    assert reloaded[record.id].models == ("deepseek-v3", "claude-4")


def test_legacy_key_without_scope_infers_from_models(tmp_path):
    """老存档没有 `modelScope`：白名单为空 → `all`，非空 → `list`，与旧行为一致。"""
    store = GatewayStore(tmp_path)
    token, record = store.issue("老 Key")
    _, listed = store.issue("老 Key 限模型", ["deepseek-v3"])
    path = tmp_path / "gateway-keys.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    for item in data:
        item.pop("modelScope", None)
        item.pop("capabilities", None)
    path.write_text(json.dumps(data), encoding="utf-8")

    reloaded = {item.id: item for item in GatewayStore(tmp_path).keys()}  # noqa: SIM118 - 非 dict
    assert reloaded[record.id].scope == SCOPE_ALL
    assert reloaded[record.id].allows("whatever") is True
    assert reloaded[listed.id].scope == SCOPE_LIST
    assert reloaded[listed.id].allows("deepseek-v3") is True
    assert reloaded[listed.id].allows("other") is False
    assert GatewayStore(tmp_path).authenticate(token) is not None


def test_capability_scope_follows_catalog_without_reissuing(tmp_path):
    """按能力：目录里后来新上的免费模型，这把 Key 自动就能用（不必重签）。"""
    store = GatewayStore(tmp_path)
    catalog = parse_catalog(CATALOG_DOC)
    _, record = store.issue("只要免费", scope=SCOPE_CAPABILITY, capabilities=["free"])

    assert record.allows("deepseek-v3", catalog) is True  # credits `x0.00` → 免费
    assert record.allows("claude-4", catalog) is False  # 计费模型
    assert record.allows("gone", catalog) is False  # 目录里没有这个模型 → 直接拒绝
    assert record.allows("deepseek-v3") is False  # 不给目录就不放行（宁严勿宽）

    # 目录里出现一个新的免费模型：同一把 Key 立刻可用，无需重签
    grown = [
        *catalog,
        CatalogModel(
            id="deepseek-new",
            name="DeepSeek New",
            context_window=128_000,
            supports_images=False,
            free=True,
        ),
    ]
    assert record.allows("deepseek-new", grown) is True
    # 未选能力 = 什么都不放行（保守，可解释）
    _, empty = store.issue("没选能力", scope=SCOPE_CAPABILITY)
    assert empty.allows("deepseek-v3", catalog) is False


def test_capability_requires_all_selected_abilities(tmp_path):
    """选中多个能力是「与」关系：大上下文 + 支持图片要同时满足。"""
    store = GatewayStore(tmp_path)
    catalog = parse_catalog(CATALOG_DOC)
    _, record = store.issue(
        "又要大又要图", scope=SCOPE_CAPABILITY, capabilities=["long_context", "image"]
    )

    assert record.allows("claude-4", catalog) is True  # 200k 上下文 + 支持图片
    assert record.allows("deepseek-v3", catalog) is False  # 128k 上下文但无图片


def test_list_scope_is_a_strict_whitelist(tmp_path):
    store = GatewayStore(tmp_path)
    _, record = store.issue("指定", ["deepseek-v3"], scope=SCOPE_LIST)
    assert record.allows("deepseek-v3") is True
    assert record.allows("claude-4") is False
    # 即使目录里新上了模型，指定模式也不会自动放行
    grown = [
        *parse_catalog(CATALOG_DOC),
        CatalogModel(
            id="deepseek-new",
            name="DeepSeek New",
            context_window=128_000,
            supports_images=False,
            free=True,
        ),
    ]
    assert record.allows("deepseek-new", grown) is False


def test_update_key_changes_scope_without_reissuing(tmp_path):
    """改范围只动记录，摘要与明文都不变 —— 客户端不用换 Key。"""
    store = GatewayStore(tmp_path)
    token, record = store.issue("待改")
    catalog = parse_catalog(CATALOG_DOC)
    assert record.allows("claude-4", catalog) is True  # 默认 all

    updated = store.update_key(record.id, scope=SCOPE_LIST, models=["deepseek-v3"])
    assert updated.scope == SCOPE_LIST
    assert updated.allows("claude-4") is False
    assert updated.allows("deepseek-v3") is True
    # 明文与摘要没动
    assert store.authenticate(token) is not None

    # 非法取值不落盘
    with pytest.raises(GatewayError):
        store.update_key(record.id, scope="nonsense")
    with pytest.raises(GatewayError):
        store.update_key(record.id, capabilities=["telepathy"])
    assert store.keys()[0].scope == SCOPE_LIST

    with pytest.raises(GatewayError):
        store.update_key("no-such-id", scope=SCOPE_ALL)


def test_update_key_endpoint_validation_and_404(client):
    record = client.post("/api/gateway/keys", json={"label": "改我"}).json()["record"]

    ok = client.put(
        f"/api/gateway/keys/{record['id']}",
        json={"scope": "capability", "capabilities": ["free"]},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["record"]["scope"] == "capability"
    assert ok.json()["record"]["capabilities"] == ["free"]

    bad_scope = client.put(f"/api/gateway/keys/{record['id']}", json={"scope": "bogus"})
    assert bad_scope.status_code == 400
    assert (
        client.put(f"/api/gateway/keys/{record['id']}", json={"capabilities": ["nope"]}).status_code
        == 400
    )
    assert client.put("/api/gateway/keys/does-not-exist", json={"scope": "all"}).status_code == 404


def test_v1_models_respects_capability_scope(client, recorder):
    """/v1/models 也按范围过滤：按能力的 Key 只看到当前目录里满足条件的模型。"""
    _seed(client, "u-1", "账号甲")
    recorder.responses.append(httpx.Response(200, json=CATALOG_DOC))
    token = client.post(
        "/api/gateway/keys",
        json={"label": "只要免费", "scope": "capability", "capabilities": ["free"]},
    ).json()["key"]

    body = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"}).json()
    assert [item["id"] for item in body["data"]] == ["deepseek-v3"]


def test_v1_models_filters_by_key_whitelist(client, recorder):
    _seed(client, "u-1", "账号甲")
    recorder.responses.append(httpx.Response(200, json=CATALOG_DOC))
    token = client.post(
        "/api/gateway/keys", json={"label": "只能用 v3", "models": ["deepseek-v3"]}
    ).json()["key"]

    body = client.get("/v1/models", headers={"Authorization": f"Bearer {token}"}).json()
    assert [item["id"] for item in body["data"]] == ["deepseek-v3"]


def test_v1_chat_rejects_model_outside_key_whitelist(client, recorder):
    _seed(client, "u-1", "账号甲")
    token = client.post(
        "/api/gateway/keys", json={"label": "只能用 v3", "models": ["deepseek-v3"]}
    ).json()["key"]

    response = client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "claude-4", "messages": []},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "model_not_found"
    # 越权请求不该打到上游
    assert recorder.requests == []


def test_keys_endpoint_returns_models(client):
    _seed(client, "u-1", "账号甲")
    response = client.post(
        "/api/gateway/keys", json={"label": "限定", "models": ["deepseek-v3"]}
    )
    assert response.status_code == 200
    assert response.json()["record"]["models"] == ["deepseek-v3"]


# ---------- /v1/responses（网关合成）与 /v1/messages（原样直转） ----------


def _sse_events(text: str) -> list[tuple[str, dict[str, Any]]]:
    """把 SSE 文本拆成 `[(事件名, 数据)]`。"""
    events: list[tuple[str, dict[str, Any]]] = []
    event = ""
    for line in text.splitlines():
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
        elif line.startswith("data: "):
            events.append((event, json.loads(line.removeprefix("data: "))))
    return events


def _names(events: list[tuple[str, dict[str, Any]]]) -> list[str]:
    return [name for name, _payload in events]


def _first(events: list[tuple[str, dict[str, Any]]], name: str) -> dict[str, Any]:
    for event, payload in events:
        if event == name:
            return payload
    raise AssertionError(f"没有 {name} 事件")


def _converted_sse(events: list[str]) -> list[tuple[str, dict[str, Any]]]:
    return _sse_events("".join(events))


def test_responses_request_maps_to_chat():
    chat = upstream_body(
        PROTOCOL_RESPONSES,
        {
            "model": "deepseek-v3",
            "instructions": "你是助手",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "你好"}],
                },
                {"type": "function_call", "call_id": "c1", "name": "lookup", "arguments": "{}"},
                {"type": "function_call_output", "call_id": "c1", "output": "结果"},
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "查",
                    "parameters": {"type": "object"},
                }
            ],
            "tool_choice": {"type": "function", "name": "lookup"},
            "max_output_tokens": 128,
            "temperature": 0.2,
            "parallel_tool_calls": False,
            "previous_response_id": "resp_old",
            "store": True,
            "include": ["reasoning.encrypted_content"],
        },
    )
    assert chat["messages"][0] == {"role": "system", "content": "你是助手"}
    assert chat["messages"][1] == {"role": "user", "content": "你好"}
    assert chat["messages"][2]["tool_calls"][0]["id"] == "c1"
    assert chat["messages"][3] == {"role": "tool", "tool_call_id": "c1", "content": "结果"}
    assert chat["tools"][0]["function"]["name"] == "lookup"
    assert chat["tool_choice"] == {"type": "function", "function": {"name": "lookup"}}
    assert chat["max_tokens"] == 128
    assert chat["temperature"] == 0.2
    assert chat["parallel_tool_calls"] is False
    # 网关没有服务端会话状态：这几个字段忽略而非报错，也不会漏进上游请求体
    assert "previous_response_id" not in chat
    assert "store" not in chat
    assert "include" not in chat


def test_responses_request_without_input_is_rejected():
    with pytest.raises(GatewayRouteError):
        upstream_body(PROTOCOL_RESPONSES, {"model": "deepseek-v3", "input": []})


def test_messages_request_is_passed_through_unchanged():
    """上游原生认这套协议：原样转发，连 thinking / cache_control 都不许动。"""
    payload = {
        "model": "deepseek-v3",
        "max_tokens": 256,
        "stream": False,
        "system": [{"type": "text", "text": "系统", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {"role": "user", "content": "问"},
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "答"},
                    {"type": "tool_use", "id": "t1", "name": "lookup", "input": {"q": "x"}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "结果"}],
                    }
                ],
            },
        ],
        "tools": [{"name": "lookup", "description": "查", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "any"},
        "stop_sequences": ["END"],
        "top_k": 40,
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "metadata": {"user_id": "u"},
    }
    assert upstream_body(PROTOCOL_MESSAGES, payload) == payload


def test_responses_stream_emits_documented_events():
    stream = ResponsesStream("deepseek-v3")
    frames = stream.start()
    frames += stream.feed({"choices": [{"index": 0, "delta": {"content": "你好"}}]})
    frames += stream.feed(
        {
            "choices": [{"index": 0, "delta": {"content": "！"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 2},
        }
    )
    frames += stream.finish()

    events = _converted_sse(frames)
    assert _names(events)[0] == "response.created"
    assert _names(events)[-1] == "response.completed"
    deltas = [payload["delta"] for name, payload in events if name == "response.output_text.delta"]
    assert deltas == ["你好", "！"]
    final = _first(events, "response.completed")["response"]
    assert final["status"] == "completed"
    assert final["usage"] == {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}
    assert final["output"][0]["content"][0]["text"] == "你好！"


def test_streams_map_tool_calls_to_native_blocks():
    """工具调用是这条链路最难的一段：逐块对照 Responses 的 function_call 事件。"""
    first = {
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": "call-1",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": '{"q"'},
                        }
                    ]
                },
            }
        ]
    }
    second = {
        "choices": [
            {
                "index": 0,
                "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": 1}"}}]},
                "finish_reason": "tool_calls",
            }
        ]
    }

    responses = ResponsesStream("m")
    events = _converted_sse(
        responses.start() + responses.feed(first) + responses.feed(second) + responses.finish()
    )
    added = _first(events, "response.output_item.added")["item"]
    assert added["type"] == "function_call"
    assert added["name"] == "lookup"
    assert _first(events, "response.function_call_arguments.done")["arguments"] == '{"q": 1}'
    final = _first(events, "response.completed")["response"]
    assert final["output"][0]["type"] == "function_call"
    assert final["output"][0]["call_id"] == "call-1"


def test_unsupported_protocol_reason_is_explicit():
    assert unsupported_reason(PROTOCOL_RESPONSES, SCOPE) == ""
    reason = unsupported_reason(PROTOCOL_RESPONSES, "trae-cn")
    assert "不会静默降级" in reason
    assert "/v1/chat/completions" in reason
    with pytest.raises(ProtocolNotSupported):
        prepare_relay(
            PROTOCOL_MESSAGES, {"messages": [{"role": "user", "content": "hi"}]}, scope="trae-cn"
        )


def test_v1_responses_returns_501_when_scope_lacks_protocol(client, recorder, monkeypatch):
    """上游不提供这个协议就 501 说清楚，绝不静默降级成 chat。"""
    import chaierww.domain.gateway_relay as relay_module

    monkeypatch.setitem(relay_module.PROTOCOL_SCOPES, PROTOCOL_RESPONSES, ())
    token = _issue_key(client)
    _seed(client, "u-1", "账号甲")

    response = client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "deepseek-v3", "input": "hi"},
    )
    assert response.status_code == 501
    assert response.json()["error"]["code"] == "protocol_not_supported"
    assert "不会静默降级" in response.json()["error"]["message"]
    # 501 是「说清楚不支持」，一个上游请求都没发
    assert recorder.requests == []


def _anthropic_sse() -> bytes:
    """一段真实形状的 Anthropic 流：输入 tokens 在首片、输出 tokens 在末片。"""
    return (
        "event: message_start\n"
        'data: {"type":"message_start","message":{"id":"msg_1","type":"message",'
        '"role":"assistant","model":"deepseek-v3","content":[],'
        '"usage":{"input_tokens":7,"output_tokens":1}}}\n\n'
        "event: content_block_start\n"
        'data: {"type":"content_block_start","index":0,'
        '"content_block":{"type":"text","text":""}}\n\n'
        "event: content_block_delta\n"
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"你好"}}\n\n'
        "event: message_delta\n"
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        '"usage":{"output_tokens":2}}\n\n'
        "event: message_stop\n"
        'data: {"type":"message_stop"}\n\n'
    ).encode()


def test_v1_messages_non_stream_passes_anthropic_json_through(client, recorder):
    """上游原生收非流式：网关不聚合、不转换，把它那份 message 对象原样回给客户端。"""
    token = _issue_key(client)
    _seed(client, "u-1", "账号甲")
    upstream_message = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "deepseek-v3",
        "content": [{"type": "text", "text": "你好！"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 4, "output_tokens": 2},
    }
    recorder.responses.append(
        httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps(upstream_message, ensure_ascii=False).encode("utf-8"),
        )
    )

    sent = {
        "model": "deepseek-v3",
        "max_tokens": 64,
        "system": "系统",
        "top_k": 40,
        "messages": [{"role": "user", "content": "hi"}],
    }
    response = client.post("/v1/messages", headers={"x-api-key": token}, json=sent)
    assert response.status_code == 200, response.text
    assert response.json() == upstream_message

    # 原样直转：客户端给的字段一个都不动，stream 也不会被改成 true
    assert recorder.requests[0].url.path == "/v1/messages"
    assert json.loads(recorder.requests[0].content) == sent

    logs = client.get("/api/gateway/logs").json()["logs"]
    assert logs[0]["ok"] is True
    assert logs[0]["stream"] is False
    assert logs[0]["promptTokens"] == 4
    assert logs[0]["completionTokens"] == 2


def test_v1_messages_stream_passes_anthropic_sse_through(client, recorder):
    """流式同样直转：事件名与分片都是上游的，网关一个字都不加工。"""
    token = _issue_key(client)
    _seed(client, "u-1", "账号甲")
    upstream_sse = _anthropic_sse()
    recorder.responses.append(
        httpx.Response(200, headers={"content-type": "text/event-stream"}, content=upstream_sse)
    )

    response = client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": "deepseek-v3",
            "max_tokens": 64,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.content == upstream_sse
    assert json.loads(recorder.requests[0].content)["stream"] is True

    logs = client.get("/api/gateway/logs").json()["logs"]
    assert logs[0]["ok"] is True
    assert logs[0]["stream"] is True
    # 输入与输出 tokens 分在两个分片里，只读尾部第一个 usage 会漏掉一半
    assert logs[0]["promptTokens"] == 7
    assert logs[0]["completionTokens"] == 2
    assert logs[0]["totalTokens"] == 9


def test_v1_responses_non_stream_returns_response_object(client, recorder):
    token = _issue_key(client)
    _seed(client, "u-1", "账号甲")
    recorder.responses.append(
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                'data: {"choices":[{"index":0,"delta":{"content":"你好"}}]}',
                'data: {"choices":[{"index":0,"delta":{"content":"！"},'
                '"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2}}',
                "data: [DONE]",
            ),
        )
    )

    response = client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "deepseek-v3", "input": "hi"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "你好！"
    assert body["usage"] == {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6}

    outbound = json.loads(recorder.requests[0].content)
    assert outbound["messages"] == [{"role": "user", "content": "hi"}]


def test_v1_responses_streams_converted_events(client, recorder):
    token = _issue_key(client)
    _seed(client, "u-1", "账号甲")
    recorder.responses.append(
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                'data: {"choices":[{"index":0,"delta":{"content":"你好"}}]}',
                'data: {"choices":[{"index":0,"delta":{"content":"！"},'
                '"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2}}',
                "data: [DONE]",
            ),
        )
    )

    response = client.post(
        "/v1/responses",
        headers={"Authorization": f"Bearer {token}"},
        json={"model": "deepseek-v3", "stream": True, "input": "hi"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)
    assert _names(events)[0] == "response.created"
    assert _names(events)[-1] == "response.completed"
    final = _first(events, "response.completed")["response"]
    assert final["output"][0]["content"][0]["text"] == "你好！"


def test_v1_messages_rejects_model_outside_key_whitelist(client, recorder):
    """Anthropic 客户端只认自家错误形状，越权 404 也得按它那套回。"""
    _seed(client, "u-1", "账号甲")
    token = client.post(
        "/api/gateway/keys", json={"label": "只能用 v3", "models": ["deepseek-v3"]}
    ).json()["key"]

    response = client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": "claude-4",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 404
    assert response.json()["type"] == "error"
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert recorder.requests == []


def test_v1_messages_non_stream_refuses_unexpected_sse(client, recorder):
    """上游对非流式请求回了 SSE：形状不是我们认识的那套，宁可 502 也不猜。"""
    token = _issue_key(client)
    _seed(client, "u-1", "账号甲")
    recorder.responses.append(
        httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse('data: {"type":"message_start"}', "data: [DONE]"),
        )
    )

    response = client.post(
        "/v1/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "model": "deepseek-v3",
            "max_tokens": 64,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert response.status_code == 502
    assert response.json()["type"] == "error"
    assert "SSE" in response.json()["error"]["message"]


def test_forget_account_clears_excluded_and_preferred(tmp_path):
    """永久删除账号后，账号池里不该再指着它。

    最要命的是 `preferred`：指向一个不存在的号 = 选号器永远找不到「指定账号」。
    落盘要一起改，重开一个新实例读回来也得干净。
    """
    store = GatewayStore(tmp_path)
    store.exclude("acc-1")
    store.exclude("acc-2")
    store.set_preferred("acc-1")

    assert store.forget_account("acc-1") is True

    assert store.pool_payload() == {"excluded": ["acc-2"], "preferred": None}
    # 重读落盘文件确认持久化
    assert GatewayStore(tmp_path).pool_payload() == {"excluded": ["acc-2"], "preferred": None}
    # 已经不在了，再摘一次如实返回 False
    assert store.forget_account("acc-1") is False
