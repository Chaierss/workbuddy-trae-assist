"""凭据生命周期（0.2.0 阶段 1）：过期判断、续期窗口、验活、状态机。

## 为什么单独一层

路由（手动「验活 / 续期」）、计划任务（每天 08:00 / 20:00）都要回答同一个问题：
「这个号的凭据现在还能用吗，快过期了要不要续」。如果两边各写一套过期判断与状态
回写，迟早出现「手动点了说好、定时跑却说坏」。所以判定、限速、缓存、状态机都收在
:class:`CredentialLifecycleService`，调用方只挑模式。

## 三种模式

- ``due``：只续「有效期已知且距到期 ≤30 分钟」的号。未知有效期的号**不发请求** ——
  这是把「默认开启自动续期」的日常上游压力压到接近零的关键。
- ``force``：逐号强制续期（用户手动点的「续期凭据」）。没有 refreshToken 的号如实
  报「不支持」，不算失败。
- ``check``：不刷新，只打一次只读的状态接口验活（Trae `checkin_credits/status`、
  WorkBuddy `checkin-activity-status`），零新增协议。

## 状态机（写死）

| 事件 | 新状态 |
|:--|:--|
| 刷新成功 / 验活 200 | ``usable`` |
| 刷新 400/401/403（非 WAF） | ``relogin_required`` |
| 验活 401/403 | ``relogin_required`` |
| 网络 / 超时 / 429 / 5xx / WB 403 code=10085 | **保持原状态** |
| 信封凭据（``raw-file``）且验活失败 | ``relogin_required``（附「需重新在客户端登录」）|

「保持原状态」不是偷懒：断网不等于号坏了，把可用号标成需重登，会让用户在机场
Wi-Fi 下把整个号池都点一遍「重新登录」。
"""

from __future__ import annotations

import base64
import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from chaierww.domain.accounts.models import Account, CredentialState
from chaierww.domain.accounts.repository import AccountNotFound, AccountRepository
from chaierww.domain.activities import (
    TRAE_CHECKIN_STATUS_PATH,
    WB_CHECKIN_STATUS_PATH,
)
from chaierww.domain.credential_refresh import (
    STATUS_REFRESHED,
    STATUS_RELOGIN_REQUIRED,
    STATUS_TRANSIENT,
    STATUS_UNSUPPORTED,
    refresher_for,
)
from chaierww.domain.platform_api import (
    TIMEOUT_SECONDS,
    TRAE_ACCOUNT_BASE,
    CredentialError,
    trae_credentials,
    trae_headers,
    workbuddy_credentials,
    workbuddy_headers,
    workbuddy_origin,
)
from chaierww.domain.scopes import UnknownScope, get_scope

logger = logging.getLogger("chaierww")

#: 续期窗口：只在到期前 30 分钟内才发刷新请求。
REFRESH_WINDOW = timedelta(minutes=30)
#: 账号之间的间隔，与活动执行的风控口径一致。
ACCOUNT_GAP_SECONDS = 0.8
#: 验活结果的缓存时间：号池页反复读取不重复打上游。
CACHE_TTL_SECONDS = 300.0

MODE_DUE = "due"
MODE_FORCE = "force"
MODE_CHECK = "check"
MODES = (MODE_DUE, MODE_FORCE, MODE_CHECK)

#: 单账号结果状态（与 `credential_refresh` 的四态 + 两种「无需处理」）。
RESULT_REFRESHED = STATUS_REFRESHED
RESULT_RELOGIN_REQUIRED = STATUS_RELOGIN_REQUIRED
RESULT_UNSUPPORTED = STATUS_UNSUPPORTED
RESULT_FAILED = STATUS_TRANSIENT
RESULT_NOOP = "noop"
RESULT_OK = "ok"


@dataclass(frozen=True, slots=True)
class SweepResult:
    """单个账号的一轮结论。不含任何 token。"""

    account_id: str
    display_name: str
    scope: str
    status: str
    detail: str = ""
    expires_at: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "accountId": self.account_id,
            "displayName": self.display_name,
            "scope": self.scope,
            "status": self.status,
            "detail": self.detail,
            "expiresAt": self.expires_at,
        }


# ---------------------------------------------------------------- 过期解析


def _as_datetime(value: Any) -> datetime | None:
    """epoch 秒 / 毫秒 / ISO 字符串 → 带时区的 datetime；解析不了返回 None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        stamp = float(value)
        if stamp > 10_000_000_000:
            stamp /= 1000.0
        try:
            return datetime.fromtimestamp(stamp, tz=UTC)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
        return moment if moment.tzinfo is not None else moment.astimezone()
    return None


def parse_expiry(value: Any) -> datetime | None:
    """把落库的过期值转成带时区的 datetime；解析不了返回 None。

    接口用它算「是否已过期」，不重复写一套 epoch / ISO 兼容逻辑。
    """
    return _as_datetime(value)


def _jwt_expiry(token: str) -> datetime | None:
    """解 JWT payload 的 `exp`。**不验签**，只读过期时间。"""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    segment = parts[1]
    padding = "=" * (-len(segment) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(segment + padding))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    return _as_datetime(payload.get("exp"))


def token_expiry(kind: str, credential: dict[str, Any]) -> datetime | None:
    """凭据的过期时间；**不知道就返回 None**，绝不猜。

    - `raw-file` 信封：本工具不解密 → None；
    - Trae `oauth-token`：优先 JWT `exp`，其次显式字段；
    - WorkBuddy `oauth-token`：`expiresAt` / `expires_at`（秒 / 毫秒 / ISO 都认）。
    """
    if credential.get("format") == "raw-file" or kind == "session":
        return None
    for key in ("expiresAt", "expires_at", "refreshExpiresAt", "refresh_expires_at"):
        moment = _as_datetime(credential.get(key))
        if moment is not None:
            return moment
    access = credential.get("accessToken") or credential.get("access_token")
    if isinstance(access, str) and access:
        return _jwt_expiry(access)
    return None


def refreshable(kind: str, credential: dict[str, Any]) -> bool:
    """这个号能不能自动续期：扫码明文 + 有 refreshToken。

    注意这里**不判断端点是否实测可用** —— 端点不存在时续期会返回 `unsupported`，
    界面据此显示「不支持自动续期」，不会让账号状态变坏。
    """
    if credential.get("format") != "oauth-token":
        return False
    if kind == "session":
        return False
    value = credential.get("refreshToken") or credential.get("refresh_token")
    return isinstance(value, str) and bool(value.strip())


def expiry_text(kind: str, credential: dict[str, Any]) -> str | None:
    """落库用的过期时间文本（UTC ISO）；不知道就返回 None。"""
    return _iso(token_expiry(kind, credential))


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat() if moment else None


def _platform_of(scope: str) -> str:
    try:
        return get_scope(scope).platform
    except UnknownScope:
        return scope


# ---------------------------------------------------------------- 服务


class CredentialLifecycleService:
    """凭据巡检 / 续期的唯一入口。

    ``client_factory`` 与 ``sleep`` 只为单测注入；生产走默认值（每次新建连接、
    真实 sleep），避免把测试用的假 client 泄漏到运行期。
    """

    def __init__(
        self,
        repository: AccountRepository,
        *,
        gap_seconds: float = ACCOUNT_GAP_SECONDS,
        cache_ttl: float = CACHE_TTL_SECONDS,
        now: Any = None,
    ) -> None:
        self._repo = repository
        self._gap = max(0.0, float(gap_seconds))
        self._cache_ttl = max(0.0, float(cache_ttl))
        self._now = now or (lambda: datetime.now(UTC))
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._cache: dict[str, tuple[float, SweepResult]] = {}
        self._cache_lock = threading.Lock()

    # ----- 公共入口 -----

    def sweep(
        self, accounts: list[Account], *, mode: str = MODE_DUE
    ) -> list[SweepResult]:
        """按模式巡检一批账号。账号之间串行 + 固定间隔，单号失败不影响其余。"""
        if mode not in MODES:
            raise ValueError(f"未知的巡检模式：{mode}")
        results: list[SweepResult] = []
        for index, account in enumerate(accounts):
            if index and mode != MODE_CHECK:
                time.sleep(self._gap)
            results.append(self._one(account, mode=mode))
        return results

    def summary(self, results: list[SweepResult]) -> dict[str, int]:
        return {
            "total": len(results),
            "refreshed": sum(1 for item in results if item.status == RESULT_REFRESHED),
            "ok": sum(1 for item in results if item.status == RESULT_OK),
            "noop": sum(1 for item in results if item.status == RESULT_NOOP),
            "reloginRequired": sum(
                1 for item in results if item.status == RESULT_RELOGIN_REQUIRED
            ),
            "unsupported": sum(1 for item in results if item.status == RESULT_UNSUPPORTED),
            "failed": sum(1 for item in results if item.status == RESULT_FAILED),
        }

    def describe(self, results: list[SweepResult], mode: str) -> str:
        """计划任务执行记录用的一行摘要。"""
        counts = self.summary(results)
        if mode == MODE_CHECK:
            return (
                f"验活 {counts['total']} 个：正常 {counts['ok']}，"
                f"需重登 {counts['reloginRequired']}，未判定 {counts['failed']}，"
                f"不支持 {counts['unsupported']}"
            )
        return (
            f"续期 {counts['total']} 个：已续 {counts['refreshed']}，"
            f"无需处理 {counts['noop']}，需重登 {counts['reloginRequired']}，"
            f"不支持 {counts['unsupported']}，失败 {counts['failed']}"
        )

    def invalidate(self, account_id: str | None = None) -> None:
        """丢掉验活缓存。手动续期 / 切换后调用，避免页面还显示旧结论。"""
        with self._cache_lock:
            if account_id is None:
                self._cache.clear()
            else:
                self._cache.pop(account_id, None)

    # ----- 单号处理 -----

    def _lock_for(self, account_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(account_id, threading.Lock())

    def _one(self, account: Account, *, mode: str) -> SweepResult:
        # 手动点击与定时任务可能同时命中同一个号：串行化，避免并发刷新把
        # refreshToken 旋转两次（第二次必然用已失效的旧 token）。
        with self._lock_for(account.id):
            try:
                kind, credential = self._repo.credential_record(account.id)
            except (AccountNotFound, ValueError):
                return self._result(
                    account, RESULT_UNSUPPORTED, "凭据不可读取，请重新导入或登录"
                )
            # 过期时间优先取凭据内嵌值，其次用入库时算好的副本：
            # 扫码拿到的 WB 有 expiresAt，Trae 只有 JWT exp，两者都已落库。
            stored = self._repo.credential_expiries([account.id]).get(account.id)
            if mode == MODE_CHECK:
                return self._check(account, kind, credential, stored)
            return self._refresh(
                account, kind, credential, force=mode == MODE_FORCE, stored=stored
            )

    def _refresh(
        self,
        account: Account,
        kind: str,
        credential: dict[str, Any],
        *,
        force: bool,
        stored: str | None = None,
    ) -> SweepResult:
        platform = _platform_of(account.scope)
        expiry = token_expiry(kind, credential) or parse_expiry(stored)
        if not refreshable(kind, credential):
            detail = (
                "客户端导入的信封凭据不可解密，无法自动续期；到期后请重新在客户端登录"
                if credential.get("format") == "raw-file"
                else "凭据里没有 refreshToken，无法自动续期"
            )
            return self._result(account, RESULT_UNSUPPORTED, detail, _iso(expiry))

        now = self._now()
        if not force:
            if expiry is None:
                # 不知道何时到期 → 不发请求。猜一个时间点去续期只会白白触发风控。
                return self._result(
                    account,
                    RESULT_NOOP,
                    "凭据没有可用的过期时间，跳过自动续期",
                    None,
                )
            remaining = expiry - now
            if remaining > REFRESH_WINDOW:
                return self._result(
                    account,
                    RESULT_NOOP,
                    f"距到期还有 {_human_delta(remaining)}，暂不续期",
                    _iso(expiry),
                )

        refresher = refresher_for(platform)
        if refresher is None:
            return self._result(account, RESULT_UNSUPPORTED, "该平台不支持自动续期", _iso(expiry))

        outcome = refresher.refresh(credential)
        if outcome.status == STATUS_REFRESHED and outcome.credential is not None:
            expires_at = outcome.expires_at or _iso(token_expiry(kind, outcome.credential))
            try:
                self._repo.replace_credential(
                    account.id,
                    credential=outcome.credential,
                    kind=kind,
                    expires_at=expires_at,
                )
            except AccountNotFound:
                return self._result(account, RESULT_FAILED, "账号已被删除", expires_at)
            self._set_state(account.id, CredentialState.USABLE)
            self.invalidate(account.id)
            logger.info("凭据已续期：%s（%s）", account.id, account.display_name)
            return self._result(account, RESULT_REFRESHED, "已自动续期", expires_at)

        if outcome.status == STATUS_RELOGIN_REQUIRED:
            self._set_state(account.id, CredentialState.RELOGIN_REQUIRED)
            self.invalidate(account.id)
            logger.info("凭据需重新登录：%s（%s）", account.id, account.display_name)
            return self._result(account, RESULT_RELOGIN_REQUIRED, outcome.detail, _iso(expiry))

        if outcome.status == STATUS_UNSUPPORTED:
            # 端点不存在 / 响应形状不认识：账号本身没坏，状态不动。
            return self._result(account, RESULT_UNSUPPORTED, outcome.detail, _iso(expiry))

        # transient：保持原状态，如实报失败。
        return self._result(account, RESULT_FAILED, outcome.detail, _iso(expiry))

    def _check(
        self,
        account: Account,
        kind: str,
        credential: dict[str, Any],
        stored: str | None = None,
    ) -> SweepResult:
        cached = self._cached(account.id)
        if cached is not None:
            return cached
        status, detail = self._verify(account, credential)
        expiry = _iso(token_expiry(kind, credential) or parse_expiry(stored))
        result = self._result(account, status, detail, expiry)
        with self._cache_lock:
            self._cache[account.id] = (time.monotonic(), result)
        return result

    def _cached(self, account_id: str) -> SweepResult | None:
        if self._cache_ttl <= 0:
            return None
        with self._cache_lock:
            item = self._cache.get(account_id)
            if item is None:
                return None
            stamped, result = item
            if time.monotonic() - stamped > self._cache_ttl:
                self._cache.pop(account_id, None)
                return None
            return result

    def _verify(
        self, account: Account, credential: dict[str, Any]
    ) -> tuple[str, str]:
        """打一次只读状态接口。返回 `(结果状态, 说明)`。"""
        platform = _platform_of(account.scope)
        try:
            if platform == "workbuddy":
                token, uid, domain = workbuddy_credentials(credential)
                url = f"{workbuddy_origin(domain)}{WB_CHECKIN_STATUS_PATH}"
                headers = workbuddy_headers(token, uid, domain)
            elif platform == "trae":
                jwt, device_id, host = trae_credentials(credential)
                url = f"{(host or TRAE_ACCOUNT_BASE).rstrip('/')}{TRAE_CHECKIN_STATUS_PATH}"
                headers = trae_headers(jwt, device_id)
            else:
                return RESULT_UNSUPPORTED, "该平台不支持验活"
        except CredentialError as exc:
            # 信封不可解密（WorkBuddy 的 DPAPI 信封）：不是「登录失效」，只是本工具
            # 没法在线验活。如实报「不支持」，**不改状态** —— 这类号在客户端里可能是
            # 完全正常的，擅自标成需重登会让用户白跑一趟。
            return RESULT_UNSUPPORTED, f"该账号无法在线验活：{exc}"

        try:
            with httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False) as client:
                resp = client.post(url, headers=headers, json={})
        except httpx.HTTPError as exc:
            return RESULT_FAILED, f"验活请求失败：{type(exc).__name__}"

        code = None
        try:
            payload = resp.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            code = payload.get("code")

        if resp.status_code in (401, 403):
            if code == 10085:
                # WAF 指纹拦截：不是令牌失效。
                return RESULT_FAILED, "上游 WAF 拦截（code=10085），本次未判定"
            self._set_state(account.id, CredentialState.RELOGIN_REQUIRED)
            self.invalidate(account.id)
            return RESULT_RELOGIN_REQUIRED, "验活返回 401/403，请重新登录"

        if resp.status_code == 200 and (code in (None, 0, 200)):
            self._set_state(account.id, CredentialState.USABLE)
            self.invalidate(account.id)
            return RESULT_OK, "登录态有效"

        return RESULT_FAILED, f"验活未通过（HTTP {resp.status_code}）"

    # ----- 小工具 -----

    def _result(
        self,
        account: Account,
        status: str,
        detail: str,
        expires_at: str | None = None,
    ) -> SweepResult:
        return SweepResult(
            account_id=account.id,
            display_name=account.display_name,
            scope=account.scope,
            status=status,
            detail=detail,
            expires_at=expires_at,
        )

    def _set_state(self, account_id: str, state: CredentialState) -> None:
        with contextlib.suppress(AccountNotFound):
            self._repo.set_credential_state(account_id, state)


def _human_delta(delta: timedelta) -> str:
    total = int(delta.total_seconds())
    if total >= 86400:
        return f"{total // 86400} 天"
    if total >= 3600:
        return f"{total // 3600} 小时"
    return f"{max(1, total // 60)} 分钟"
