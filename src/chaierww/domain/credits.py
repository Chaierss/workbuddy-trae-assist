"""积分余额与到期时间查询。

两个平台的协议出处（对照旧项目 2026-10-19 抓包固化）：

**Trae**（`modules/trae/credits.rs`）
    POST {account_base}/trae/api/v2/pay/ide_user_ent_usage
    头部 `authorization: Cloud-IDE-JWT <jwt>` + 一串设备头；
    响应 `user_entitlement_pack_list[]`，每包
    `entitlement_base_info.quota.credits_limit`（总额）与 `usage.credits_amount`（已用），
    剩余 = limit - used（下限 0）；`expire_time`（unix 秒）是该包到期时间。

**WorkBuddy**（`modules/credits.rs`）
    POST https://www.workbuddy.cn/billing/meter/get-user-resource-summary
    POST .../get-user-resource-paid-packages
    POST .../get-user-resource-free-packages
    响应里的 `Packages` / `Accounts` 数组，每项含
    `CycleCapacityRemainPrecise`（剩余）/ `CycleCapacitySizePrecise`（总额）/
    `DeductionEndTime`、`ExpiredTime`、`CycleEndTime`（到期，腾讯按 UTC+8 墙钟给）。

## 凭据可查性（诚实降级，三条不同的路）

| 账号形态 | 能否查积分 | 依据 |
|:--|:--|:--|
| Trae 扫码账号（oauth-token） | ✅ | 凭据里有明文 JWT |
| Trae 客户端导入（raw-file） | ✅ | `icube.cloudide` 是 tc 信封，pepper 公开可解 |
| WorkBuddy 扫码账号（oauth-token） | ✅ | 凭据里有明文 accessToken |
| WorkBuddy 客户端导入（raw-file） | ❌ | 认证文件里的 token 是 `$wbEncrypted` 信封，本工具不解密 |

最后一格**必须诚实报「不可查询」**，不能显示 0 —— 显示 0 会让用户以为账号没额度。
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import httpx

from chaierww.domain.accounts.models import Account
from chaierww.domain.accounts.repository import AccountNotFound
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

TRAE_ENTITLEMENT_PATH = "/trae/api/v2/pay/ide_user_ent_usage"

WB_SUMMARY_PATH = "/billing/meter/get-user-resource-summary"
WB_PAID_PATH = "/billing/meter/get-user-resource-paid-packages"
WB_FREE_PATH = "/billing/meter/get-user-resource-free-packages"

WORKBUDDY_PAID_CODES = [
    "TCACA_code_002_AkiJS3ZHF5",
    "TCACA_code_023_4xbGhMrE6q",
    "TCACA_code_026_BaESVICNoi",
    "TCACA_code_027_0FCGVA6vSa",
    "TCACA_code_009_0XmEQc2xOf",
    "TCACA_code_038_OhvqZtiPKr",
]
WORKBUDDY_FREE_CODES = [
    "TCACA_code_008_cfWoLwvjU4",
    "TCACA_code_007_nzdH5h4Nl0",
    "TCACA_code_028_NtpWi0jzXs",
    "TCACA_code_029_6wCGEWquYy",
    "TCACA_code_030_BjSt89qTvr",
]

EXPIRING_SOON_SECONDS = 7 * 24 * 3600
CACHE_TTL_SECONDS = 60.0

CN_TZ = timezone(timedelta(hours=8))


#: 凭据形态不支持查询（或凭据损坏）。消息面向用户，可直接展示。
#: 具体实现已上移到 `platform_api`（活动检测共用同一套解包逻辑）。
CreditUnavailable = CredentialError


@dataclass(frozen=True, slots=True)
class CreditPackage:
    """一个积分包 / 订阅项的明细。"""

    name: str
    remaining: float
    total: float | None = None
    expires_at: int | None = None
    kind: str = ""  # 例如「套餐基础积分 / 购买积分 / 会员积分 / 用户福利」

    def payload(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "remaining": self.remaining,
            "total": self.total,
            "expiresAt": self.expires_at,
            "kind": self.kind,
        }


@dataclass(frozen=True, slots=True)
class CreditInfo:
    """一个账号的积分摘要。``available=False`` 表示查不了（不是 0）。"""

    available: bool
    credits: float | None = None
    #: 总额度（官方字段）：Trae 的 usage_summary.total_amount，WorkBuddy 的
    #: summary 各 CreditGroups 的 CycleTotalCapacity 之和。查不到为 None。
    total: float | None = None
    expires_at: int | None = None
    expiring_soon: bool = False
    error: str | None = None
    updated_at: str | None = None
    #: 订阅 / 套餐档位（WorkBuddy 的 SubscriptionPackageName、Trae 的 display_desc 聚合）。
    subscription: str | None = None
    is_paid: bool | None = None
    #: 积分包明细（套餐基础 / 赠送 / 购买 / 会员等），供号池卡片展开展示。
    packages: tuple[CreditPackage, ...] = ()

    def payload(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "credits": self.credits,
            "total": self.total,
            "expiresAt": self.expires_at,
            "expiringSoon": self.expiring_soon,
            "error": self.error,
            "updatedAt": self.updated_at,
            "subscription": self.subscription,
            "isPaid": self.is_paid,
            "packages": [item.payload() for item in self.packages],
        }


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _round2(value: float) -> float:
    return round(value + 0.0, 2)


def parse_trae_packs(packs: list[Any], now_ts: int) -> tuple[float, int | None]:
    """由 ``user_entitlement_pack_list`` 算 (剩余总额, 最早有效到期)。"""
    total = 0.0
    earliest: int | None = None
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        base = pack.get("entitlement_base_info")
        if not isinstance(base, dict):
            continue
        quota = base.get("quota")
        limit = quota.get("credits_limit") if isinstance(quota, dict) else None
        if not isinstance(limit, (int, float)):
            continue
        usage = pack.get("usage")
        used = usage.get("credits_amount") if isinstance(usage, dict) else 0
        if not isinstance(used, (int, float)):
            used = 0.0
        remaining = max(float(limit) - float(used), 0.0)
        total += remaining
        # 只有还有剩余、且未过期的包才参与「最早到期」。
        if remaining <= 0:
            continue
        expire = pack.get("expire_time")
        if isinstance(expire, int) and expire > now_ts:
            earliest = expire if earliest is None else min(earliest, expire)
    return _round2(total), earliest


def parse_trae_packages(packs: list[Any], now_ts: int) -> tuple[list[CreditPackage], str | None]:
    """由 ``user_entitlement_pack_list`` 解析积分包明细与订阅档位。

    返回 ``(packages, subscription)``。``subscription`` 取第一个带订阅语义
    （``display_desc`` 含「连续包月 / 会员」或 ``group_type==0``）的展示名。
    """
    packages: list[CreditPackage] = []
    subscription: str | None = None
    for pack in packs:
        if not isinstance(pack, dict):
            continue
        base = pack.get("entitlement_base_info")
        if not isinstance(base, dict):
            continue
        quota = base.get("quota")
        limit = quota.get("credits_limit") if isinstance(quota, dict) else None
        if not isinstance(limit, (int, float)):
            continue
        usage = pack.get("usage")
        used = usage.get("credits_amount") if isinstance(usage, dict) else 0
        if not isinstance(used, (int, float)):
            used = 0.0
        remaining = max(float(limit) - float(used), 0.0)
        name = str(pack.get("display_desc") or pack.get("group_name") or "积分包").strip()
        kind = str(pack.get("group_name") or "").strip()
        expire = pack.get("expire_time")
        expires = expire if isinstance(expire, int) and expire > now_ts else None
        packages.append(
            CreditPackage(
                name=name or "积分包",
                remaining=_round2(remaining),
                total=_round2(float(limit)),
                expires_at=expires,
                kind=kind,
            )
        )
        # 订阅档位：连续包月 / 会员 之类；优先 group_type==0（会员积分）。
        is_sub = pack.get("group_type") == 0 or "连续包月" in name or "会员" in name
        if subscription is None and is_sub:
            subscription = name or None
    return packages, subscription


def fetch_trae_credits(
    client: httpx.Client, credential: dict[str, Any], now_ts: int
) -> CreditInfo:
    jwt, device_id, host = trae_credentials(credential)
    base = host or TRAE_ACCOUNT_BASE
    url = f"{base.rstrip('/')}{TRAE_ENTITLEMENT_PATH}"
    # 真实客户端恒发 full_data: true，否则可能拿不到完整包列表。
    body = {"require_usage": True, "full_data": True}
    try:
        resp = client.post(url, json=body, headers=trae_headers(jwt, device_id))
    except httpx.HTTPError as exc:
        return CreditInfo(available=True, error=f"查询失败：{exc}", updated_at=_now_iso())
    if resp.status_code == 401:
        return CreditInfo(
            available=True,
            error="令牌已失效，请在客户端重新登录或用扫码登录重新添加",
            updated_at=_now_iso(),
        )
    try:
        data = resp.json()
    except ValueError:
        return CreditInfo(
            available=True,
            error=f"响应不是 JSON（HTTP {resp.status_code}）",
            updated_at=_now_iso(),
        )
    packs = data.get("user_entitlement_pack_list") if isinstance(data, dict) else None
    if not isinstance(packs, list):
        message = ""
        if isinstance(data, dict):
            message = str(data.get("message") or data.get("msg") or "")
        return CreditInfo(
            available=True,
            error=f"响应中缺少积分包列表{'：' + message if message else ''}",
            updated_at=_now_iso(),
        )
    credits, expires_at = parse_trae_packs(packs, now_ts)
    packages, subscription = parse_trae_packages(packs, now_ts)
    # 官方 usage_summary 是「已用 / 总额」的权威口径，优先用它算剩余与总额，
    # 比把各积分包 remaining 简单相加更准（会员包 remaining=0 不代表没额度）。
    total: float | None = None
    if isinstance(data, dict):
        usage_summary = data.get("usage_summary")
        if isinstance(usage_summary, dict):
            total_raw = _first_number(usage_summary, ["total_amount"])
            consumed = _first_number(usage_summary, ["consumed_amount"])
            if total_raw is not None:
                total = _round2(total_raw)
                if consumed is not None:
                    credits = _round2(max(total_raw - consumed, 0.0))
    return CreditInfo(
        available=True,
        credits=credits,
        total=total,
        expires_at=expires_at,
        expiring_soon=bool(expires_at and expires_at - now_ts <= EXPIRING_SOON_SECONDS),
        updated_at=_now_iso(),
        subscription=subscription,
        packages=tuple(packages),
    )


# ---------- WorkBuddy ----------


def _wb_items(response: Any) -> list[Any]:
    """在响应里找 Packages / Accounts 数组（上游包裹层不统一）。"""
    paths = [
        ("data", "Packages"),
        ("data", "packages"),
        ("data", "Accounts"),
        ("data", "accounts"),
        ("data", "data", "Packages"),
        ("data", "data", "Accounts"),
        ("data", "Response", "Data", "Packages"),
        ("data", "Response", "Data", "Accounts"),
        ("data", "data", "Response", "Data", "Packages"),
        ("data", "data", "Response", "Data", "Accounts"),
    ]
    for path in paths:
        current = response
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        if isinstance(current, list):
            return current
    return []


def _first_number(raw: dict[str, Any], keys: list[str]) -> float | None:
    for key in keys:
        value = raw.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                continue
    return None


def _parse_cn_wallclock(value: Any) -> int | None:
    """腾讯给的是 UTC+8 墙钟（无时区）。按 +08:00 解释成 unix 秒。"""
    if isinstance(value, (int, float)):
        number = float(value)
        # 毫秒或秒
        return int(number / 1000) if number > 10_000_000_000 else int(number)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(text, fmt).replace(tzinfo=CN_TZ)
            return int(parsed.timestamp())
        except ValueError:
            continue
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        return None


def parse_wb_resources(items: list[Any], now_ts: int) -> tuple[float, int | None]:
    """由资源条目算 (剩余总额, 最早有效到期)。"""
    remaining_total = 0.0
    earliest: int | None = None
    for item in items:
        if not isinstance(item, dict):
            continue
        remaining = _first_number(
            item,
            [
                "CycleCapacityRemainPrecise",
                "CycleCapacityRemain",
                "CycleRemainCapacity",
                "CapacityRemainPrecise",
                "CapacityRemain",
            ],
        )
        total = _first_number(
            item,
            [
                "CycleCapacitySizePrecise",
                "CycleCapacitySize",
                "CycleTotalCapacity",
                "CapacitySizePrecise",
                "CapacitySize",
            ],
        )
        used = _first_number(
            item,
            [
                "CycleCapacityUsedPrecise",
                "CycleCapacityUsed",
                "CycleUsedCapacity",
                "CapacityUsedPrecise",
                "CapacityUsed",
            ],
        )
        if remaining is None:
            if total is not None and used is not None:
                remaining = max(total - used, 0.0)
            elif total is not None:
                remaining = total
            else:
                continue
        remaining = max(remaining, 0.0)
        remaining_total += remaining

        if remaining <= 0:
            continue
        expire = _parse_cn_wallclock(
            item.get("DeductionEndTime")
            or item.get("ExpiredTime")
            or item.get("CycleEndTime")
        )
        if expire and expire > now_ts:
            earliest = expire if earliest is None else min(earliest, expire)
    return _round2(remaining_total), earliest


def fetch_workbuddy_credits(
    client: httpx.Client, credential: dict[str, Any], now_ts: int
) -> CreditInfo:
    token, uid, domain = workbuddy_credentials(credential)
    # API 接口统一发 codebuddy.cn（workbuddy_origin 恒返回 API 域）。
    base = workbuddy_origin(domain)
    headers = workbuddy_headers(token, uid, domain)

    bodies = [
        (WB_SUMMARY_PATH, {}),
        (
            WB_PAID_PATH,
            {
                "PageNumber": 1,
                "PageSize": 200,
                "Status": [0, 3],
                "PackageCodes": WORKBUDDY_PAID_CODES,
                "NeedRenewInfo": True,
            },
        ),
        (
            WB_FREE_PATH,
            {
                "PageNumber": 1,
                "PageSize": 200,
                "Status": [0, 3],
                "PackageCodes": WORKBUDDY_FREE_CODES,
            },
        ),
    ]

    items: list[Any] = []
    subscription: str | None = None
    is_paid: bool | None = None
    packages: list[CreditPackage] = []
    last_error = ""
    #: summary 接口的官方口径：剩余（TotalCredits）与各分类组（CreditGroups）。
    #: 不再把 summary 的 Packages 与 paid/free 的 Accounts 混在一起做剩余相加——
    #: 二者是同一批积分在「汇总」与「明细」两个视图里的重复呈现，相加会翻倍。
    total_credits: float | None = None
    total_capacity: float | None = None
    for path, body in bodies:
        try:
            resp = client.post(f"{base}{path}", json=body, headers=headers)
        except httpx.HTTPError as exc:
            last_error = f"查询失败：{exc}"
            continue
        if resp.status_code == 401:
            return CreditInfo(
                available=True,
                error="令牌已失效，请重新扫码登录",
                updated_at=_now_iso(),
            )
        try:
            data = resp.json()
        except ValueError:
            last_error = f"响应不是 JSON（HTTP {resp.status_code}）"
            continue
        if resp.status_code == 403 and isinstance(data, dict):
            # code 10085 是网关 WAF 的「请求不合法」（缺客户端指纹头），不是 token 失效。
            code = data.get("code")
            if code == 10085:
                last_error = (
                    "计费接口拒绝请求（token 签发域不匹配）。"
                    "请用「扫码登录」重新添加该账号后再查"
                )
                continue
        # summary 里的订阅档位、官方剩余/总额、分类积分组。
        if path == WB_SUMMARY_PATH and isinstance(data, dict):
            inner = data.get("data") if isinstance(data.get("data"), dict) else {}
            sub_name = inner.get("SubscriptionPackageName")
            if isinstance(sub_name, str) and sub_name.strip():
                subscription = sub_name.strip()
            if isinstance(inner.get("IsPaidUser"), bool):
                is_paid = inner["IsPaidUser"]
            tc = _first_number(inner, ["TotalCredits"])
            if tc is not None:
                total_credits = tc
            for group in inner.get("CreditGroups") or []:
                if not isinstance(group, dict):
                    continue
                remaining = _first_number(group, ["CycleRemainCapacity"]) or 0.0
                total = _first_number(group, ["CycleTotalCapacity"])
                if total is not None:
                    total_capacity = (total_capacity or 0.0) + total
                packages.append(
                    CreditPackage(
                        name=str(group.get("Name") or "积分").strip(),
                        remaining=_round2(remaining),
                        total=_round2(total) if total is not None else None,
                        kind=str(group.get("Kind") or ""),
                    )
                )
        # paid/free 套餐包明细：只补「有到期时间的裂变包」明细，不参与剩余总额。
        if path in (WB_PAID_PATH, WB_FREE_PATH):
            for item in _wb_items(data):
                if not isinstance(item, dict):
                    continue
                remaining = _first_number(
                    item, ["CycleCapacityRemainPrecise", "CycleCapacityRemain"]
                )
                total = _first_number(item, ["CycleCapacitySizePrecise", "CycleCapacitySize"])
                if remaining is None and total is None:
                    continue
                name = (
                    str(item.get("PackageName") or item.get("SubProductName") or "积分包").strip()
                )
                expire = _parse_cn_wallclock(
                    item.get("DeductionEndTime")
                    or item.get("CycleEndTime")
                    or item.get("ExpiredTime")
                )
                packages.append(
                    CreditPackage(
                        name=name or "积分包",
                        remaining=_round2(remaining) if remaining is not None else 0.0,
                        total=_round2(total) if total is not None else None,
                        expires_at=expire if expire and expire > now_ts else None,
                        kind="",
                    )
                )
        items.extend(_wb_items(data))

    if total_credits is None and not items:
        return CreditInfo(
            available=True,
            error=last_error or "响应中没有积分资源",
            updated_at=_now_iso(),
        )

    # 剩余：优先官方 TotalCredits；否则退回对 items（仅 paid/free 明细）求和。
    if total_credits is not None:
        credits = _round2(total_credits)
    else:
        credits, _ = parse_wb_resources(items, now_ts)

    # 总额：优先 CreditGroups 的 CycleTotalCapacity 之和；否则对 items 求和。
    total: float | None = _round2(total_capacity) if total_capacity is not None else None
    if total is None and items:
        total = 0.0
        for item in items:
            if not isinstance(item, dict):
                continue
            size = _first_number(
                item, ["CycleCapacitySizePrecise", "CycleCapacitySize"]
            )
            if size is not None:
                total += size
        total = _round2(total) if total > 0 else None

    # 到期时间：从明细包里取最早的有效到期。
    expires_at: int | None = None
    for item in items:
        if not isinstance(item, dict):
            continue
        expire = _parse_cn_wallclock(
            item.get("DeductionEndTime") or item.get("CycleEndTime") or item.get("ExpiredTime")
        )
        if expire and expire > now_ts:
            expires_at = expire if expires_at is None else min(expires_at, expire)

    # 若 summary 的 CreditGroups 没给出明细（老客户端），退回 items 里的 Accounts 解析。
    if not packages:
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("PackageName") or item.get("SubProductName") or "积分包").strip()
            remaining = _first_number(item, ["CycleCapacityRemainPrecise", "CycleCapacityRemain"])
            total_item = _first_number(item, ["CycleCapacitySizePrecise", "CycleCapacitySize"])
            expire = _parse_cn_wallclock(
                item.get("DeductionEndTime") or item.get("CycleEndTime") or item.get("ExpiredTime")
            )
            packages.append(
                CreditPackage(
                    name=name or "积分包",
                    remaining=_round2(remaining) if remaining is not None else 0.0,
                    total=_round2(total_item) if total_item is not None else None,
                    expires_at=expire if expire and expire > now_ts else None,
                    kind="",
                )
            )
    return CreditInfo(
        available=True,
        credits=credits,
        total=total,
        expires_at=expires_at,
        expiring_soon=bool(expires_at and expires_at - now_ts <= EXPIRING_SOON_SECONDS),
        updated_at=_now_iso(),
        subscription=subscription,
        is_paid=is_paid,
        packages=tuple(packages),
    )


# ---------- 服务 ----------


class CreditsService:
    """按账号查询积分，带短期内存缓存与并发。"""

    def __init__(self, repository: Any, *, ttl: float = CACHE_TTL_SECONDS) -> None:
        self._repo = repository
        self._ttl = ttl
        self._cache: dict[str, tuple[float, CreditInfo]] = {}
        self._lock = threading.Lock()
        # trust_env=False：两个平台都是国内直连端点，不应走本机环境代理
        # （旧项目同样显式 no_proxy，理由是自建 MITM 代理会把请求引向死端口）。
        self._client = httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False)

    def query_many(
        self, accounts: list[Account], *, force: bool = False
    ) -> dict[str, CreditInfo]:
        now = time.time()
        result: dict[str, CreditInfo] = {}
        pending: list[Account] = []

        with self._lock:
            for account in accounts:
                cached = self._cache.get(account.id)
                if cached and not force and now - cached[0] < self._ttl:
                    result[account.id] = cached[1]
                else:
                    pending.append(account)

        if pending:
            workers = min(8, max(1, len(pending)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(self._query_one, a): a for a in pending}
                for future, account in futures.items():
                    info = future.result()
                    result[account.id] = info
                    with self._lock:
                        self._cache[account.id] = (time.time(), info)

        return result

    def cached(self, account_id: str) -> CreditInfo | None:
        """只读缓存：回「还新鲜」的那一条，**绝不触发新请求**。

        网关选号靠它判断「积分耗尽」，口径必须与 `query_many` 的缓存一致：
        过期即视为没有数据 —— 宁可多试一个账号，也不拿旧余额误杀账号。
        """
        with self._lock:
            entry = self._cache.get(account_id)
        if entry is None:
            return None
        at, info = entry
        if time.time() - at >= self._ttl:
            return None
        return info

    def invalidate(self, account_id: str | None = None) -> None:
        with self._lock:
            if account_id is None:
                self._cache.clear()
            else:
                self._cache.pop(account_id, None)

    def _query_one(self, account: Account) -> CreditInfo:
        try:
            credential = self._repo.current_credential(account.id)
        except AccountNotFound:
            return CreditInfo(available=False, error="无可用凭据")

        now_ts = int(time.time())
        try:
            platform = get_scope(account.scope).platform
        except UnknownScope:
            return CreditInfo(available=False, error="未知平台")
        try:
            if platform == "trae":
                return fetch_trae_credits(self._client, credential, now_ts)
            if platform == "workbuddy":
                return fetch_workbuddy_credits(self._client, credential, now_ts)
        except CreditUnavailable as exc:
            return CreditInfo(available=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001 - 单账号失败不拖垮整页
            return CreditInfo(available=True, error=f"查询异常：{type(exc).__name__}")
        return CreditInfo(available=False, error="不支持的平台")

    def close(self) -> None:
        self._client.close()
