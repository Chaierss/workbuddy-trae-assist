"""网关选号的纯逻辑：把「谁先上」拆出来，便于单测与解释。

`GatewayRelay` 只负责过滤（排除 / 积分耗尽 / 冷却）与内存状态；这里只回答
「剩下的这些号按什么顺序排」。排序必须是确定性的：分数相同就看创建时间与 id，
否则同一份数据在不同进程里会给出不同结果，测试也没法复现。

四种模式：

- `balanced`：默认，完整复刻 1.0.0 的「偏好 → 粘性 → 轮转」；
- `quality`：看最近采样里的成功率，再看平均耗时；
- `quota`：看剩余积分，余额多的先上；
- `smart`：成功率与余额加权，粘性账号只加一点点分。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from chaierww.domain.accounts.models import Account

ROUTING_BALANCED = "balanced"
ROUTING_QUALITY = "quality"
ROUTING_QUOTA = "quota"
ROUTING_SMART = "smart"
ROUTING_MODES = (ROUTING_BALANCED, ROUTING_QUALITY, ROUTING_QUOTA, ROUTING_SMART)

#: 质量信号至少要有这么多次采样才当真；否则按中性 0.5 处理。
QUALITY_MIN_SAMPLES = 5
#: `smart` 里成功率与余额的权重。
SMART_QUALITY_WEIGHT = 0.6
SMART_QUOTA_WEIGHT = 0.4
SMART_STICKY_BONUS = 0.05


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    mode: str = ROUTING_BALANCED
    min_credits: float = 0.0
    max_inflight_per_account: int = 0

    @classmethod
    def from_config(cls, config: Any) -> RoutingPolicy:
        return cls(
            mode=str(getattr(config, "routing_mode", ROUTING_BALANCED)),
            min_credits=float(getattr(config, "min_credits", 0.0) or 0.0),
            max_inflight_per_account=int(
                getattr(config, "max_inflight_per_account", 0) or 0
            ),
        )


@dataclass(frozen=True, slots=True)
class AccountSignal:
    """最近采样窗口内的账号信号；`credits` 是缓存快照，不触发新请求。"""

    success: int = 0
    failed: int = 0
    avg_duration_ms: int | None = None
    credits: float | None = None
    total: float | None = None

    @property
    def samples(self) -> int:
        return self.success + self.failed

    @property
    def success_rate(self) -> float:
        return (self.success + 1) / (self.samples + 2)


def passes_min_credits(account: Account, signal: AccountSignal, policy: RoutingPolicy) -> bool:
    """积分门槛：查不到余额时视为未知，**不排除**（宁可多试，也不误杀）。"""
    if policy.min_credits <= 0:
        return True
    if signal.credits is None:
        return True
    return signal.credits >= policy.min_credits


def _quality_score(signal: AccountSignal) -> float:
    if signal.samples < QUALITY_MIN_SAMPLES:
        return 0.5
    return signal.success_rate


def _quota_score(signal: AccountSignal) -> float:
    # 未知余额排在有数据之后；余额不会为负，所以 -1 是安全的哨兵值。
    return -1.0 if signal.credits is None else float(signal.credits)


def _normalized_quota(signal: AccountSignal) -> float:
    if signal.credits is None:
        return 0.5
    if signal.total is not None and signal.total > 0:
        return max(0.0, min(1.0, signal.credits / signal.total))
    # 没有总额时给一个单调的软归一化：余额越多越接近 1。
    return max(0.0, min(1.0, signal.credits / (signal.credits + 100.0)))


def _score(
    account: Account,
    signal: AccountSignal,
    policy: RoutingPolicy,
    sticky_id: str | None,
) -> float:
    if policy.mode == ROUTING_QUOTA:
        return _quota_score(signal)
    if policy.mode == ROUTING_QUALITY:
        return _quality_score(signal)
    if policy.mode == ROUTING_SMART:
        score = (
            SMART_QUALITY_WEIGHT * _quality_score(signal)
            + SMART_QUOTA_WEIGHT * _normalized_quota(signal)
        )
        if rank_key(account)[1] == sticky_id:
            score += SMART_STICKY_BONUS
        return score
    return 0.0


def rank_key(account: Account) -> tuple[str, str]:
    """确定性兜底：创建时间升序，再按 id。"""
    return (str(getattr(account, "created_at", "") or ""), str(account.id))


def rank_accounts(
    healthy: list[Account],
    cooling: list[Account],
    signals: dict[str, AccountSignal],
    policy: RoutingPolicy,
    *,
    preferred_id: str | None = None,
    sticky_id: str | None = None,
    rotor: int = 0,
    cooldown_seconds: dict[str, float] | None = None,
) -> list[Account]:
    """给可用的号排序，冷却中的号统一追加到队尾。"""
    cooldown_seconds = cooldown_seconds or {}

    eligible = [
        account
        for account in healthy
        if passes_min_credits(account, signals.get(account.id, AccountSignal()), policy)
    ]
    if not eligible:
        ordered: list[Account] = []
    elif policy.mode == ROUTING_BALANCED:
        preferred = (
            next((item for item in eligible if item.id == preferred_id), None)
            if preferred_id
            else None
        )
        if preferred is not None:
            ordered = [preferred, *(item for item in eligible if item.id != preferred.id)]
        else:
            ordered = _legacy_order(eligible, sticky_id, rotor)
    else:
        ordered = sorted(
            eligible,
            key=lambda account: (
                -_score(account, signals.get(account.id, AccountSignal()), policy, sticky_id),
                rank_key(account),
            ),
        )
        if preferred_id:
            preferred = next((item for item in ordered if item.id == preferred_id), None)
            if preferred is not None:
                ordered = [preferred, *(item for item in ordered if item.id != preferred.id)]

    cooling_sorted = sorted(
        cooling,
        key=lambda account: (cooldown_seconds.get(account.id, 0.0), rank_key(account)),
    )
    return (ordered if eligible else []) + cooling_sorted


def _legacy_order(accounts: list[Account], sticky_id: str | None, rotor: int) -> list[Account]:
    if sticky_id:
        return sorted(accounts, key=lambda account: 0 if account.id == sticky_id else 1)
    if len(accounts) > 1:
        start = rotor % len(accounts)
        return accounts[start:] + accounts[:start]
    return list(accounts)
