"""选号策略的纯函数测试：排序必须确定、可解释，且不误杀数据缺失的账号。"""

from __future__ import annotations

import pytest

from chaierww.domain.accounts.models import Account, CredentialState
from chaierww.domain.gateway import GatewayConfig, GatewayError
from chaierww.domain.gateway_routing import (
    ROUTING_BALANCED,
    ROUTING_QUALITY,
    ROUTING_QUOTA,
    ROUTING_SMART,
    AccountSignal,
    RoutingPolicy,
    rank_accounts,
)

SCOPE = "workbuddy-cn"


def _account(account_id: str, created_at: str = "2026-01-01T00:00:00+00:00") -> Account:
    return Account(
        id=account_id,
        scope=SCOPE,
        external_id=account_id,
        display_name=account_id,
        note="",
        group_name="",
        avatar_path=None,
        source="test",
        credential_state=CredentialState.USABLE,
        last_used_at=None,
        created_at=created_at,
        updated_at=created_at,
        deleted_at=None,
    )


def _ids(accounts: list[Account]) -> list[str]:
    return [account.id for account in accounts]


def test_quota_prefers_more_credits_and_keeps_unknown_after_known():
    accounts = [_account("low"), _account("high"), _account("unknown")]
    signals = {
        "low": AccountSignal(credits=100.0),
        "high": AccountSignal(credits=900.0),
        # unknown 没有任何信号：余额未知，排在已知余额之后但**不被排除**。
    }

    order = rank_accounts(
        accounts, [], signals, RoutingPolicy(mode=ROUTING_QUOTA), rotor=0
    )

    assert _ids(order) == ["high", "low", "unknown"]


def test_quality_prefers_high_success_then_low_latency():
    accounts = [_account("good"), _account("neutral"), _account("bad")]
    signals = {
        "good": AccountSignal(success=9, failed=1, avg_duration_ms=400),
        "neutral": AccountSignal(success=4, failed=0, avg_duration_ms=100),
        "bad": AccountSignal(success=0, failed=5, avg_duration_ms=80),
    }

    order = rank_accounts(
        accounts, [], signals, RoutingPolicy(mode=ROUTING_QUALITY), rotor=0
    )
    assert _ids(order) == ["good", "neutral", "bad"]

    # 成功率相同（同样 5/6）时，平均耗时短的先上。
    tie = rank_accounts(
        [_account("slow"), _account("fast")],
        [],
        {
            "slow": AccountSignal(success=5, failed=0, avg_duration_ms=300),
            "fast": AccountSignal(success=5, failed=0, avg_duration_ms=90),
        },
        RoutingPolicy(mode=ROUTING_QUALITY),
        rotor=0,
    )
    assert _ids(tie) == ["fast", "slow"]


def test_smart_balances_quality_and_credits_without_sticky_override():
    accounts = [_account("steady"), _account("rich")]
    signals = {
        "steady": AccountSignal(success=9, failed=1, credits=100.0, total=200.0),
        "rich": AccountSignal(success=0, failed=9, credits=10_000.0, total=10_100.0),
    }

    order = rank_accounts(
        accounts,
        [],
        signals,
        RoutingPolicy(mode=ROUTING_SMART),
        sticky_id="rich",
        rotor=0,
    )

    # 粘性只加 0.05，不足以让一个成功率 10% 的号压过 83% 的号。
    assert _ids(order) == ["steady", "rich"]


def test_min_credits_filters_low_balance_but_keeps_unknown():
    accounts = [_account("poor"), _account("unknown"), _account("fine")]
    signals = {
        "poor": AccountSignal(credits=10.0),
        "fine": AccountSignal(credits=500.0),
    }

    order = rank_accounts(
        accounts,
        [],
        signals,
        RoutingPolicy(mode=ROUTING_QUOTA, min_credits=50.0),
        rotor=0,
    )

    assert "poor" not in _ids(order)
    assert set(_ids(order)) == {"unknown", "fine"}


def test_cooling_accounts_are_appended_by_remaining_cooldown():
    healthy = [_account("ready")]
    cooling = [_account("cooling-slow"), _account("cooling-fast")]

    order = rank_accounts(
        healthy,
        cooling,
        {},
        RoutingPolicy(mode=ROUTING_BALANCED),
        rotor=0,
        cooldown_seconds={"cooling-slow": 90.0, "cooling-fast": 5.0},
    )

    assert _ids(order) == ["ready", "cooling-fast", "cooling-slow"]


def test_ties_fall_back_to_created_at_then_id():
    # 同分、同创建时间：按 id 升序，保证同一份数据每次结果一致。
    same = rank_accounts(
        [_account("b"), _account("a")],
        [],
        {"a": AccountSignal(credits=10.0), "b": AccountSignal(credits=10.0)},
        RoutingPolicy(mode=ROUTING_QUOTA),
        rotor=0,
    )
    assert _ids(same) == ["a", "b"]

    older = _account("z", "2025-12-01T00:00:00+00:00")
    newer = _account("a", "2026-02-01T00:00:00+00:00")
    by_time = rank_accounts(
        [newer, older],
        [],
        {"z": AccountSignal(credits=10.0), "a": AccountSignal(credits=10.0)},
        RoutingPolicy(mode=ROUTING_QUOTA),
        rotor=0,
    )
    assert _ids(by_time) == ["z", "a"]


def test_balanced_replicates_legacy_preferred_sticky_and_rotor():
    accounts = [_account("a"), _account("b"), _account("c")]
    policy = RoutingPolicy(mode=ROUTING_BALANCED)

    preferred = rank_accounts(
        accounts, [], {}, policy, preferred_id="c", rotor=0
    )
    assert _ids(preferred) == ["c", "a", "b"]

    sticky = rank_accounts(accounts, [], {}, policy, sticky_id="b", rotor=0)
    assert _ids(sticky) == ["b", "a", "c"]

    rotated = rank_accounts(accounts, [], {}, policy, rotor=1)
    assert _ids(rotated) == ["b", "c", "a"]


def test_config_validation_rejects_bad_routing_values():
    with pytest.raises(GatewayError):
        GatewayConfig(routing_mode="nope").validate()
    with pytest.raises(GatewayError):
        GatewayConfig(min_credits=-1).validate()
    with pytest.raises(GatewayError):
        GatewayConfig(min_credits=100_001).validate()
    with pytest.raises(GatewayError):
        GatewayConfig(max_inflight_per_account=9).validate()

    # 合法边界不报错。
    GatewayConfig(
        routing_mode=ROUTING_SMART, min_credits=100_000, max_inflight_per_account=8
    ).validate()


def test_policy_from_config_reads_new_fields():
    config = GatewayConfig(
        routing_mode=ROUTING_QUALITY, min_credits=12.5, max_inflight_per_account=3
    )
    policy = RoutingPolicy.from_config(config)
    assert policy.mode == ROUTING_QUALITY
    assert policy.min_credits == 12.5
    assert policy.max_inflight_per_account == 3
