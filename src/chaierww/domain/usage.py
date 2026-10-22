"""用量页数据：请求/积分消耗的合并视图。

## 两个平台能做多少（诚实边界）

| 平台 | 逐请求明细 | 按模型聚合 | 按日趋势 | 积分消耗 |
|:--|:--|:--|:--|:--|
| WorkBuddy | ✅ `get-user-request-usage` | ✅ | ✅ | ✅（另走 credits） |
| Trae | ❌ 无逐请求接口 | ❌ | ❌ | ✅ `usage_summary.consumed_amount` |

Trae 的 `ide_user_ent_usage` 只回积分包与 `usage_summary`（consumed/total），
**没有**逐请求记录。所以合并视图里「请求/模型/趋势」三块只有 WorkBuddy 的数据，
Trae 只能出现在「积分消耗」那一块。这点必须写在界面上，不能让用户以为
「Trae 没请求量」=「Trae 没用过」。

## 协议出处（2026-10-19 真实账号验证）

WorkBuddy：`POST {origin}/billing/meter/get-user-request-usage`，body
`{startTime, endTime, pageNum, pageSize}`；响应 `data.data[]` 每行
`{requestId, credit, model, client, requestTime, input, inputTrunc, agentPurpose, appType}`，
`data.total` 是总数。时间范围取最近 31 个自然日。分页 pageSize=3000，
按 requestId+requestTime 去重，安全上限 100 页。

Trae：`POST {account_base}/trae/api/v2/pay/ide_user_ent_usage`（body
`{require_usage:true, full_data:true}`），`usage_summary.consumed_amount` 是已消耗，
`usage_summary.total_amount` 是总额。

## 诚实性纪律

1. 积分**不可互换**：WorkBuddy 和 Trae 的积分是两套独立账本，合并相加必须在
   可见处标注「不可互换、不可相加使用」。
2. 读不到 ≠ 0：信封账号（WB DPAPI）或查不到的，标「不可查」+ 原因，绝不显示 0。
3. 请求量只画「消耗」侧，不做「入账」（上游拿不到入账明细）。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

import httpx

from chaierww.domain.accounts.models import Account
from chaierww.domain.accounts.repository import AccountNotFound
from chaierww.domain.platform_api import (
    TIMEOUT_SECONDS,
    CredentialError,
    trae_credentials,
    trae_headers,
    workbuddy_credentials,
    workbuddy_headers,
    workbuddy_origin,
)
from chaierww.domain.scopes import UnknownScope, get_scope
from chaierww.domain.sessions import conversations_for_user
from chaierww.domain.usage_turns import lookup_many as lookup_turns

WB_USAGE_PATH = "/billing/meter/get-user-request-usage"
TRAE_ENTITLEMENT_PATH = "/trae/api/v2/pay/ide_user_ent_usage"

RANGE_DAYS = 31
PAGE_SIZE = 3000
MAX_PAGES = 100

CN_TZ = timezone(timedelta(hours=8))
CACHE_TTL_SECONDS = 300.0


# ---------- 数据结构 ----------


@dataclass(frozen=True, slots=True)
class RequestRow:
    """一条 WorkBuddy 请求明细（已脱敏，只留统计需要的字段）。"""

    request_id: str
    credit: float
    model: str
    client: str
    request_time: str
    date: str
    #: 本机能关联到的会话 id（`requestId` == jsonl 的 `conversationRequestId`）；
    #: 关联不到为 None —— 界面据此决定「能不能点开看对话」。
    conversation_id: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "credit": self.credit,
            "model": self.model,
            "client": self.client,
            "requestTime": self.request_time,
            "date": self.date,
            "conversationId": self.conversation_id,
        }


@dataclass(frozen=True, slots=True)
class ModelUsage:
    model: str
    requests: int
    credit: float


@dataclass(frozen=True, slots=True)
class DailyPoint:
    date: str
    requests: int
    credit: float
    models: list[ModelUsage]


@dataclass(frozen=True, slots=True)
class ConversationUsage:
    """一次会话（客户端 cid）在这段时间里花掉的积分与请求数。"""

    conversation_id: str
    title: str
    cwd: str
    requests: int
    credit: float
    last_time: str

    def payload(self) -> dict[str, Any]:
        return {
            "conversationId": self.conversation_id,
            "title": self.title,
            "cwd": self.cwd,
            "requests": self.requests,
            "credit": self.credit,
            "lastTime": self.last_time,
        }


@dataclass(frozen=True, slots=True)
class AccountUsage:
    """一个账号的用量。``detectable=False`` 表示查不了（不是没用量）。"""

    account_id: str
    display_name: str
    platform: str
    scope: str
    detectable: bool
    # 请求侧（只有 WorkBuddy 有）
    requests: int = 0
    credit_consumed: float = 0.0
    today_requests: int = 0
    today_credit: float = 0.0
    week_requests: int = 0
    week_credit: float = 0.0
    month_requests: int = 0
    month_credit: float = 0.0
    models: tuple[ModelUsage, ...] = ()
    daily: tuple[DailyPoint, ...] = ()
    detail: tuple[RequestRow, ...] = ()
    detail_truncated: bool = False
    #: 按会话聚合的消耗（只在能关联到本机会话时才有；界面用它排「最烧积分的对话」）。
    conversations: tuple[ConversationUsage, ...] = ()
    #: 明细里能关联到本机会话的行数（界面上说明关联率，读不到就说读不到）。
    linked_requests: int = 0
    # 积分侧（Trae 只有这个）
    credits_consumed: float | None = None
    credits_total: float | None = None
    error: str | None = None
    updated_at: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "accountId": self.account_id,
            "displayName": self.display_name,
            "platform": self.platform,
            "scope": self.scope,
            "detectable": self.detectable,
            "requests": self.requests,
            "creditConsumed": self.credit_consumed,
            "todayRequests": self.today_requests,
            "todayCredit": self.today_credit,
            "weekRequests": self.week_requests,
            "weekCredit": self.week_credit,
            "monthRequests": self.month_requests,
            "monthCredit": self.month_credit,
            "models": [m.payload() if hasattr(m, "payload") else m for m in self.models],
            "daily": [d.payload() if hasattr(d, "payload") else d for d in self.daily],
            "detail": [r.payload() if hasattr(r, "payload") else r for r in self.detail],
            "detailTruncated": self.detail_truncated,
            "conversations": [
                c.payload() if hasattr(c, "payload") else c for c in self.conversations
            ],
            "linkedRequests": self.linked_requests,
            "creditsConsumed": self.credits_consumed,
            "creditsTotal": self.credits_total,
            "error": self.error,
            "updatedAt": self.updated_at,
        }


# ---------- 小工具 ----------


def _now_iso() -> str:
    return datetime.now(CN_TZ).isoformat(timespec="seconds")


def _round(value: float) -> float:
    return round(value, 2)


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _parse_request_time(value: Any) -> tuple[str, str] | None:
    """返回 ``(date_str, request_time_str)``；解析失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # 秒是 ~10 位（1.7e9），毫秒是 ~13 位（1.7e12）。超过 1e11 视为毫秒。
        seconds = value / 1000 if value >= 100_000_000_000 else value
        try:
            parsed = datetime.fromtimestamp(seconds, tz=CN_TZ)
        except (OverflowError, OSError, ValueError):
            return None
        return parsed.strftime("%Y-%m-%d"), parsed.strftime("%Y-%m-%d %H:%M:%S")

    text = _text(value)
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            parsed = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return parsed.strftime("%Y-%m-%d"), text
    if len(text) >= 10:
        return text[:10], text
    return None


def normalize_row(raw: dict[str, Any]) -> RequestRow | None:
    """一条原始明细 → 规范化行；credit 非法或时间解析失败的丢弃。"""
    credit = _num(raw.get("credit"))
    if credit is None or credit < 0:
        return None
    parsed = _parse_request_time(raw.get("requestTime") or raw.get("request_time"))
    if parsed is None:
        return None
    date_str, request_time = parsed
    return RequestRow(
        request_id=_text(raw.get("requestId") or raw.get("request_id")) or "unknown",
        credit=credit,
        model=_text(raw.get("model")) or "—",
        client=_text(raw.get("client")) or "—",
        request_time=request_time,
        date=date_str,
    )


def parse_wb_usage_page(response: Any) -> tuple[list[RequestRow], int]:
    """一个分页响应 → (规范化行列表, 总数)。code 非 0 抛 ValueError。"""
    if not isinstance(response, dict):
        raise ValueError("官方响应格式无效")
    code = response.get("code")
    if code not in (0, 200):
        message = _text(response.get("message") or response.get("msg"))
        raise ValueError(f"官方请求失败{('：' + message) if message else ''}")
    data = response.get("data")
    if not isinstance(data, dict):
        raise ValueError("官方响应格式无效")
    raw_rows = data.get("data")
    if not isinstance(raw_rows, list):
        raise ValueError("官方响应格式无效")
    total = _num(data.get("total"))
    rows = [row for raw in raw_rows if isinstance(raw, dict) and (row := normalize_row(raw))]
    return rows, int(total) if total is not None else len(rows)


def aggregate_rows(rows: list[RequestRow], today: date) -> dict[str, Any]:
    """把明细聚合成 today/week/month 计数、按模型、按日序列。"""
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    today_req = today_credit = 0
    week_req = week_credit = 0
    month_req = month_credit = 0
    model_map: dict[str, list[float]] = {}
    daily_map: dict[str, list[float]] = {}

    for row in rows:
        try:
            row_date = date.fromisoformat(row.date)
        except ValueError:
            continue
        credit = row.credit
        if row_date == today:
            today_req += 1
            today_credit += credit
        if week_start <= row_date <= today:
            week_req += 1
            week_credit += credit
        if month_start <= row_date <= today:
            month_req += 1
            month_credit += credit
        model_map.setdefault(row.model, []).append(credit)
        daily_map.setdefault(row.date, []).append(credit)

    models = tuple(
        ModelUsage(model=model, requests=len(values), credit=_round(sum(values)))
        for model, values in sorted(
            model_map.items(), key=lambda item: sum(item[1]), reverse=True
        )
    )
    daily = tuple(
        DailyPoint(
            date=key,
            requests=len(values),
            credit=_round(sum(values)),
            models=(),
        )
        for key, values in sorted(daily_map.items())
    )

    return {
        "today_requests": today_req,
        "today_credit": _round(today_credit),
        "week_requests": week_req,
        "week_credit": _round(week_credit),
        "month_requests": month_req,
        "month_credit": _round(month_credit),
        "models": models,
        "daily": daily,
        "requests": len(rows),
        "credit_consumed": _round(sum(row.credit for row in rows)),
    }


def attach_conversations(
    rows: list[RequestRow], external_id: str
) -> tuple[list[RequestRow], tuple[ConversationUsage, ...], int]:
    """把每条明细关联回本机会话，并按会话聚合消耗。

    关联依据：云端 ``requestId`` == 本机 jsonl 的 ``providerData.conversationRequestId``。
    关联不到就保持 ``None``（界面如实说「本机无对话记录」），不猜。
    """
    meta = conversations_for_user(external_id)
    allowed = set(meta)
    located = lookup_turns([row.request_id for row in rows], allowed=allowed)
    linked: list[RequestRow] = []
    grouped: dict[str, list[RequestRow]] = {}

    for row in rows:
        found = located.get(row.request_id)
        if found is None:
            linked.append(row)
            continue
        updated = RequestRow(
            request_id=row.request_id,
            credit=row.credit,
            model=row.model,
            client=row.client,
            request_time=row.request_time,
            date=row.date,
            conversation_id=found.conversation_id,
        )
        linked.append(updated)
        grouped.setdefault(found.conversation_id, []).append(updated)

    conversations = tuple(
        ConversationUsage(
            conversation_id=cid,
            title=meta.get(cid, ("(未知会话)", ""))[0],
            cwd=meta.get(cid, ("(未知会话)", ""))[1],
            requests=len(items),
            credit=_round(sum(item.credit for item in items)),
            last_time=max(item.request_time for item in items),
        )
        for cid, items in grouped.items()
    )
    ordered = tuple(sorted(conversations, key=lambda item: item.credit, reverse=True))
    return linked, ordered, sum(len(items) for items in grouped.values())


# ---------- 抓取 ----------


def fetch_wb_usage(
    client: httpx.Client, credential: dict[str, Any]
) -> tuple[list[RequestRow], bool]:
    """抓取一个 WB 账号最近 31 天的全部请求明细，返回 (行, 是否截断)。"""
    token, uid, domain = workbuddy_credentials(credential)
    origin = workbuddy_origin(domain)
    headers = workbuddy_headers(token, uid, domain)
    end = datetime.now(CN_TZ)
    start = end - timedelta(days=RANGE_DAYS - 1)
    start_time = start.strftime("%Y-%m-%d 00:00:00")
    end_time = end.strftime("%Y-%m-%d 23:59:59")

    rows: list[RequestRow] = []
    seen: set[tuple[str, str]] = set()
    fetched_raw = 0
    reported_total = 0
    truncated = False

    for page_number in range(1, MAX_PAGES + 1):
        body = {
            "startTime": start_time,
            "endTime": end_time,
            "pageNum": page_number,
            "pageSize": PAGE_SIZE,
        }
        try:
            response = client.post(
                f"{origin}{WB_USAGE_PATH}", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"查询失败：{exc}") from exc
        if response.status_code == 401:
            raise CredentialError("令牌已失效，请重新扫码登录或重新导入")
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(f"响应不是 JSON（HTTP {response.status_code}）") from exc
        page_rows, total = parse_wb_usage_page(data)
        reported_total = max(reported_total, total)
        fetched_raw += len(page_rows)
        for row in page_rows:
            key = (row.request_id, row.request_time)
            if key not in seen:
                seen.add(key)
                rows.append(row)

        if not page_rows or fetched_raw >= reported_total:
            break
        if page_number == MAX_PAGES and fetched_raw < reported_total:
            truncated = True

    return rows, truncated


def fetch_trae_consumption(
    client: httpx.Client, credential: dict[str, Any]
) -> tuple[float | None, float | None]:
    """抓 Trae 的积分消耗摘要，返回 (consumed, total)。"""
    jwt, device_id, host = trae_credentials(credential)
    base = (host or "https://api.trae.cn").rstrip("/")
    body = {"require_usage": True, "full_data": True}
    try:
        response = client.post(
            f"{base}{TRAE_ENTITLEMENT_PATH}", json=body, headers=trae_headers(jwt, device_id)
        )
    except httpx.HTTPError as exc:
        raise RuntimeError(f"查询失败：{exc}") from exc
    if response.status_code == 401:
        raise CredentialError("令牌已失效，请在客户端重新登录")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"响应不是 JSON（HTTP {response.status_code}）") from exc
    summary = data.get("usage_summary") if isinstance(data, dict) else None
    if not isinstance(summary, dict):
        raise RuntimeError("响应中缺少 usage_summary")
    return _num(summary.get("consumed_amount")), _num(summary.get("total_amount"))


# ---------- 服务 ----------


class UsageService:
    """按账号查用量，带内存缓存。请求侧只有 WorkBuddy，积分侧两端都有。"""

    def __init__(self, repository: Any, *, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._repo = repository
        self._ttl = ttl
        self._cache: dict[str, tuple[float, AccountUsage]] = {}
        self._lock = threading.Lock()
        self._client = httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False)

    def query_many(self, accounts: list[Account], *, force: bool = False) -> list[AccountUsage]:
        now = time.time()
        result: list[AccountUsage] = []
        for account in accounts:
            with self._lock:
                cached = self._cache.get(account.id)
            if cached and not force and now - cached[0] < self._ttl:
                result.append(cached[1])
            else:
                usage = self._query_one(account)
                with self._lock:
                    self._cache[account.id] = (time.time(), usage)
                result.append(usage)
        return result

    def invalidate(self, account_id: str | None = None) -> None:
        with self._lock:
            if account_id is None:
                self._cache.clear()
            else:
                self._cache.pop(account_id, None)

    def _query_one(self, account: Account) -> AccountUsage:
        try:
            platform = get_scope(account.scope).platform
        except UnknownScope:
            return self._blank(account, "unknown", "未知平台")
        try:
            credential = self._repo.current_credential(account.id)
        except AccountNotFound:
            return self._blank(account, platform, "没有可用凭据")

        if platform == "workbuddy":
            return self._query_workbuddy(account, credential)
        if platform == "trae":
            return self._query_trae(account, credential)
        return self._blank(account, platform, "不支持的平台")

    def _blank(self, account: Account, platform: str, error: str) -> AccountUsage:
        return AccountUsage(
            account_id=account.id,
            display_name=account.display_name,
            platform=platform,
            scope=account.scope,
            detectable=False,
            error=error,
            updated_at=_now_iso(),
        )

    def _query_workbuddy(self, account: Account, credential: dict[str, Any]) -> AccountUsage:
        try:
            rows, truncated = fetch_wb_usage(self._client, credential)
        except CredentialError as exc:
            return self._blank(account, "workbuddy", str(exc))
        except (RuntimeError, ValueError) as exc:
            return AccountUsage(
                account_id=account.id,
                display_name=account.display_name,
                platform="workbuddy",
                scope=account.scope,
                detectable=True,
                error=str(exc),
                updated_at=_now_iso(),
            )

        today = datetime.now(CN_TZ).date()
        rows, conversations, linked = attach_conversations(rows, account.external_id or "")
        agg = aggregate_rows(rows, today)
        detail = tuple(sorted(rows, key=lambda row: row.request_time, reverse=True))
        return AccountUsage(
            account_id=account.id,
            display_name=account.display_name,
            platform="workbuddy",
            scope=account.scope,
            detectable=True,
            requests=agg["requests"],
            credit_consumed=agg["credit_consumed"],
            today_requests=agg["today_requests"],
            today_credit=agg["today_credit"],
            week_requests=agg["week_requests"],
            week_credit=agg["week_credit"],
            month_requests=agg["month_requests"],
            month_credit=agg["month_credit"],
            models=agg["models"],
            daily=agg["daily"],
            detail=detail,
            detail_truncated=truncated,
            conversations=conversations,
            linked_requests=linked,
            updated_at=_now_iso(),
        )

    def _query_trae(self, account: Account, credential: dict[str, Any]) -> AccountUsage:
        try:
            consumed, total = fetch_trae_consumption(self._client, credential)
        except CredentialError as exc:
            return self._blank(account, "trae", str(exc))
        except (RuntimeError, ValueError) as exc:
            return AccountUsage(
                account_id=account.id,
                display_name=account.display_name,
                platform="trae",
                scope=account.scope,
                detectable=True,
                error=str(exc),
                updated_at=_now_iso(),
            )
        return AccountUsage(
            account_id=account.id,
            display_name=account.display_name,
            platform="trae",
            scope=account.scope,
            detectable=True,
            credits_consumed=consumed,
            credits_total=total,
            updated_at=_now_iso(),
        )

    def close(self) -> None:
        self._client.close()


def summarize(accounts: list[AccountUsage]) -> dict[str, Any]:
    """合并视图的顶部汇总。积分**不可互换**，只做展示侧合计并标注。"""
    detectable = [a for a in accounts if a.detectable]
    total_requests = sum(a.requests for a in detectable)
    total_credit = _round(sum(a.credit_consumed for a in detectable))
    wb_credits = _round(sum(a.credit_consumed for a in detectable if a.platform == "workbuddy"))
    trae_consumed = sum(
        a.credits_consumed for a in detectable if a.platform == "trae" and a.credits_consumed
    )
    return {
        "accounts": len(accounts),
        "detectable": len(detectable),
        "totalRequests": total_requests,
        "totalCreditConsumed": total_credit,
        "workbuddyCreditConsumed": wb_credits,
        "traeCreditsConsumed": _round(trae_consumed) if trae_consumed else None,
    }
