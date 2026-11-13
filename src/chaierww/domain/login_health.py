"""登录健康：只读探测「这个号的登录态现在还能不能用」。

与凭据续期严格分开：

- 续期会轮换 refreshToken、会打上游写接口；探测**只读**，失败也不改凭据状态；
- 网络错误 / 5xx 不改变 ``login_state``，只记明细 —— 一次超时不该把一个好号判死；
- 401/403 才是 ``invalid``；Trae 的「设备绑在别处」是 ``blocked``（换机导入的号）。

``login_state`` 落在 ``accounts`` 表上，号池列表与网关选号都直接读它。
"""

from __future__ import annotations

import contextlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from chaierww.domain.credits import TRAE_ENTITLEMENT_PATH
from chaierww.domain.platform_api import (
    TRAE_ACCOUNT_BASE,
    CredentialError,
    trae_credentials,
    trae_headers,
    workbuddy_credentials,
    workbuddy_headers,
    workbuddy_origin,
)
from chaierww.infra.db import now_iso

LOGIN_UNKNOWN = "unknown"
LOGIN_VALID = "valid"
LOGIN_EXPIRING = "expiring"
LOGIN_INVALID = "invalid"
LOGIN_BLOCKED = "blocked"

LOGIN_STATES: tuple[str, ...] = (
    LOGIN_UNKNOWN,
    LOGIN_VALID,
    LOGIN_EXPIRING,
    LOGIN_INVALID,
    LOGIN_BLOCKED,
)

#: 登录态距过期不足这个窗口就算 ``expiring``（与设计文档的 24 小时一致）。
EXPIRING_WINDOW = timedelta(hours=24)
#: 探测超时短一些：登录健康是「顺手确认」，不该像活动执行那样长等。
PROBE_TIMEOUT_SECONDS = 8.0
#: 并发上限：同时打三个上游已经足够，再多容易被风控。
PROBE_CONCURRENCY = 3
#: WorkBuddy 的只读计费摘要接口（GET 语义、POST 空体，实测无副作用）。
WB_SUMMARY_PATH = "/billing/meter/get-user-resource-summary"


@dataclass(frozen=True, slots=True)
class LoginProbe:
    state: str
    detail: str = ""
    checked_at: str = ""

    def payload(self) -> dict[str, Any]:
        return {"state": self.state, "detail": self.detail, "checkedAt": self.checked_at}


def _result(state: str, detail: str = "") -> LoginProbe:
    return LoginProbe(state=state, detail=detail, checked_at=now_iso())


def probe_workbuddy_login(
    client: httpx.Client, credential: dict[str, Any]
) -> LoginProbe:
    """WorkBuddy：打一次只读计费摘要，只看状态码与业务码。"""
    try:
        token, uid, domain = workbuddy_credentials(credential)
    except CredentialError as exc:
        return _result(LOGIN_INVALID, str(exc))
    url = f"{workbuddy_origin(domain)}{WB_SUMMARY_PATH}"
    try:
        response = client.post(url, json={}, headers=workbuddy_headers(token, uid, domain))
    except httpx.HTTPError as exc:
        # 网络错误不改变登录态：这一轮探测不下结论，留给下一轮。
        return _result(LOGIN_UNKNOWN, f"网络错误：{type(exc).__name__}")
    if response.status_code in (401, 403):
        code = _body_code(response)
        if code == 10085:
            # 网关 WAF 的「请求不合法」，不是令牌失效；判 invalid 会误杀好号。
            return _result(LOGIN_UNKNOWN, "上游风控拒绝了探测请求（code 10085），本次不下结论")
        return _result(LOGIN_INVALID, f"令牌失效（HTTP {response.status_code}）")
    if response.status_code >= 500:
        return _result(LOGIN_UNKNOWN, f"上游异常（HTTP {response.status_code}），本次不下结论")
    if response.status_code == 200:
        code = _body_code(response)
        if code in (None, 0, 200):
            return _result(LOGIN_VALID)
        return _result(LOGIN_UNKNOWN, f"探测响应业务码 {code}，本次不下结论")
    return _result(LOGIN_UNKNOWN, f"探测响应 HTTP {response.status_code}，本次不下结论")


def probe_trae_login(client: httpx.Client, credential: dict[str, Any]) -> LoginProbe:
    """Trae：读一次权益包列表；设备绑定在别处直接判 blocked。"""
    try:
        jwt, device_id, host = trae_credentials(credential)
    except CredentialError as exc:
        return _result(LOGIN_INVALID, str(exc))
    # 设备私钥不在本机 = 这份凭据是从别的设备导入的，续期与使用都会失败。
    if _trae_device_foreign(credential):
        return _result(
            LOGIN_BLOCKED,
            "令牌绑定在另一台设备（通常是别处导入的号），需在本机重新登录",
        )
    url = f"{(host or TRAE_ACCOUNT_BASE).rstrip('/')}{TRAE_ENTITLEMENT_PATH}"
    try:
        response = client.post(
            url,
            json={"require_usage": True, "full_data": True},
            headers=trae_headers(jwt, device_id),
        )
    except httpx.HTTPError as exc:
        return _result(LOGIN_UNKNOWN, f"网络错误：{type(exc).__name__}")
    if response.status_code in (401, 403):
        return _result(LOGIN_INVALID, f"令牌失效（HTTP {response.status_code}）")
    if response.status_code >= 500:
        return _result(LOGIN_UNKNOWN, f"上游异常（HTTP {response.status_code}），本次不下结论")
    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError:
            return _result(LOGIN_UNKNOWN, "探测响应不是 JSON，本次不下结论")
        if isinstance(body, dict) and "user_entitlement_pack_list" in body:
            return _result(LOGIN_VALID)
        return _result(LOGIN_UNKNOWN, "探测响应里没有权益包列表，本次不下结论")
    return _result(LOGIN_UNKNOWN, f"探测响应 HTTP {response.status_code}，本次不下结论")


def _trae_device_foreign(credential: dict[str, Any]) -> bool:
    # 延迟导入：credential_refresh 依赖较重，且探测失败要能完全静默。
    try:
        from chaierww.domain.credential_refresh import trae_device_foreign

        return bool(trae_device_foreign(credential))
    except Exception:  # noqa: BLE001 - 判断本身不该阻断探测
        return False


def _body_code(response: httpx.Response) -> int | None:
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    value = body.get("code")
    if isinstance(value, bool):
        return None
    return value if isinstance(value, int) else None


def expiring_soon(expires_at: str | None, *, now: datetime | None = None) -> bool:
    """凭据过期时间是否落在 24 小时内；认不出来一律 False（不为缺数据制造告警）。"""
    moment = _parse_iso(expires_at)
    if moment is None:
        return False
    return moment - (now or datetime.now(UTC)) <= EXPIRING_WINDOW


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class LoginProbeService:
    """批量探测并落库；对外的 ``probe`` 与手动「一键复检」共用。"""

    def __init__(
        self,
        repository: Any,
        adapters: dict[str, Any],
        *,
        client_factory: Any | None = None,
        concurrency: int = PROBE_CONCURRENCY,
    ) -> None:
        self._repo = repository
        self._adapters = adapters
        self._concurrency = max(1, int(concurrency))
        self._client_factory = client_factory or (
            lambda: httpx.Client(timeout=PROBE_TIMEOUT_SECONDS, trust_env=False)
        )
        self._lock = threading.Lock()

    def probe(self, account_ids: list[str] | None = None) -> dict[str, Any]:
        """探测指定账号（空 = 全部未删除账号），返回逐号结论。"""
        accounts = self._targets(account_ids)
        if not accounts:
            return {"items": [], "total": 0, "valid": 0, "invalid": 0, "blocked": 0}
        with (
            self._client_factory() as client,
            ThreadPoolExecutor(max_workers=self._concurrency) as pool,
        ):
            results = list(
                pool.map(lambda account: self._probe_one(client, account), accounts)
            )
        for account, probe, expires_at in results:
            final = probe.state
            if final == LOGIN_VALID and expiring_soon(expires_at):
                final = LOGIN_EXPIRING
            with contextlib.suppress(Exception):
                # 落库失败不该让本轮探测结果丢失（界面仍会拿到 items）。
                self._repo.set_login_probe(account.id, final, probe.detail)
        items = [
            {
                "accountId": account.id,
                "displayName": account.display_name,
                "scope": account.scope,
                "state": (
                    LOGIN_EXPIRING
                    if probe.state == LOGIN_VALID and expiring_soon(expires_at)
                    else probe.state
                ),
                "detail": probe.detail,
                "checkedAt": probe.checked_at,
            }
            for account, probe, expires_at in results
        ]
        return {
            "items": items,
            "total": len(items),
            "valid": sum(1 for item in items if item["state"] in (LOGIN_VALID, LOGIN_EXPIRING)),
            "invalid": sum(1 for item in items if item["state"] == LOGIN_INVALID),
            "blocked": sum(1 for item in items if item["state"] == LOGIN_BLOCKED),
        }

    def _targets(self, account_ids: list[str] | None) -> list[Any]:
        scopes = sorted(self._adapters)
        accounts = self._repo.list_all(scopes) if scopes else []
        if account_ids:
            wanted = set(account_ids)
            accounts = [account for account in accounts if account.id in wanted]
        return accounts

    def _probe_one(
        self, client: httpx.Client, account: Any
    ) -> tuple[Any, LoginProbe, str | None]:
        try:
            credential = self._repo.current_credential(account.id)
        except Exception:  # noqa: BLE001 - 无凭据 = 结构性不可用
            return account, _result(LOGIN_INVALID, "没有可读取的凭据"), None
        platform = _platform_of(account.scope)
        try:
            if platform == "trae":
                probe = probe_trae_login(client, credential)
            else:
                probe = probe_workbuddy_login(client, credential)
        except Exception as exc:  # noqa: BLE001 - 单个号探测异常不拖垮整批
            probe = _result(LOGIN_UNKNOWN, f"探测异常：{type(exc).__name__}")
        expires_at = None
        try:
            expires_at = self._repo.credential_expiries([account.id]).get(account.id)
        except Exception:  # noqa: BLE001 - 到期时间读不到只影响 expiring 判定
            expires_at = None
        return account, probe, expires_at


def _platform_of(scope: str) -> str:
    if scope.startswith("trae"):
        return "trae"
    return "workbuddy"
