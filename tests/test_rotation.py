"""积分到期自动轮换单测（P1 / G6）。

判定是纯函数，所以七条规则各用一条用例把「是哪一条挡住」钉死；
策略/台账那张表用真文件测，服务与线程用注入的假回调测 —— 全程不发网络。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from chaierww.domain.rotation import (
    Candidate,
    RotationError,
    RotationLedgerEntry,
    RotationPolicy,
    RotationRunner,
    RotationService,
    RotationSnapshot,
    RotationStore,
    decide,
    parse_policy,
)
from chaierww.domain.scheduler import Scheduler

TZ = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=TZ)


def _expires_in(hours: float) -> int:
    return int(NOW.timestamp() + hours * 3600)


def candidate(
    account_id: str,
    *,
    hours: float | None = 48.0,
    remaining: float | None = 1000.0,
    available: bool = True,
    switchable: bool = True,
    used_minutes_ago: float | None = None,
) -> Candidate:
    return Candidate(
        account_id=account_id,
        display_name=f"账号 {account_id}",
        scope="workbuddy-cn",
        remaining=remaining,
        expires_at=_expires_in(hours) if hours is not None else None,
        available=available,
        switchable=switchable,
        last_used_at=(
            (NOW - timedelta(minutes=used_minutes_ago)).isoformat(timespec="seconds")
            if used_minutes_ago is not None
            else None
        ),
    )


def _switchable_pool() -> list[Candidate]:
    """默认场景：当前账号 200 小时后到期，另一个账号 10 小时后到期。

    七条判定全部满足 → 应切到「更紧迫」的那个。
    """
    return [
        candidate("cur", hours=200.0, remaining=5000.0, used_minutes_ago=600.0),
        candidate("soon", hours=10.0, remaining=1000.0),
    ]


def _run(pool: list[Candidate], policy: RotationPolicy | None = None, **kwargs):
    return decide(
        pool,
        policy or RotationPolicy(),
        current_account_id=kwargs.pop("current", "cur"),
        now=kwargs.pop("now", NOW),
        **kwargs,
    )


# ---------- 七条判定 ----------


def test_rule_1_needs_a_usable_candidate():
    """有效账号：无剩余 / 查不到 / 不可切 / 已过期，都不参与轮换。"""
    # 完全没有可参与轮换的账号时才落到这一条（有一个有效账号就会继续往下判）。
    assert _run([candidate("soon", hours=10.0, remaining=0.0)]).rule == "valid_accounts"

    decision = _run([candidate("soon", hours=10.0, available=False, remaining=None)])
    assert (decision.action, decision.rule) == ("none", "valid_accounts")

    decision = _run([candidate("soon", hours=10.0, switchable=False)])
    assert (decision.action, decision.rule) == ("none", "valid_accounts")

    decision = _run([candidate("soon", hours=-1.0)])
    assert (decision.action, decision.rule) == ("none", "valid_accounts")


def test_rule_2_urgency_blocks_when_nothing_expires_soon():
    """最紧迫的也要 100 小时才到期，超过 72 小时阈值 → 不切。"""
    decision = _run(
        [candidate("cur", hours=400.0), candidate("soon", hours=100.0)], current="none"
    )
    assert (decision.action, decision.rule) == ("none", "urgency")

    # 阈值可调：放到 120 小时就认为该切了。
    decision = _run(
        [candidate("cur", hours=400.0), candidate("soon", hours=100.0)],
        RotationPolicy(min_urgency_hours=120.0),
        current="none",
    )
    assert decision.action == "switch"


def test_rule_3_already_target_does_not_move():
    decision = _run(_switchable_pool(), current="soon")
    assert (decision.action, decision.rule) == ("none", "already_target")


def test_rule_4_cooldown_blocks_recent_rotation():
    decision = _run(
        _switchable_pool(),
        last_switch_at=(NOW - timedelta(minutes=10)).isoformat(timespec="seconds"),
    )
    assert (decision.action, decision.rule) == ("none", "cooldown")

    # 冷却期已过 → 放行。
    decision = _run(
        _switchable_pool(),
        last_switch_at=(NOW - timedelta(minutes=121)).isoformat(timespec="seconds"),
    )
    assert decision.action == "switch"


def test_rule_5_active_guard_blocks_recently_used_account():
    pool = [
        candidate("cur", hours=200.0, remaining=5000.0, used_minutes_ago=5.0),
        candidate("soon", hours=10.0),
    ]
    decision = _run(pool)
    assert (decision.action, decision.rule) == ("none", "active_guard")

    # 已闲置 31 分钟（> 30 分钟保护期）→ 放行。
    pool[0] = candidate("cur", hours=200.0, remaining=5000.0, used_minutes_ago=31.0)
    assert _run(pool).action == "switch"


def test_rule_6_value_filter_blocks_low_balance_target():
    pool = [
        candidate("cur", hours=200.0, remaining=5000.0),
        candidate("soon", hours=10.0, remaining=50.0),
    ]
    decision = _run(pool)
    assert (decision.action, decision.rule) == ("none", "value")


def test_rule_7_gap_blocks_when_target_is_only_slightly_earlier():
    pool = [candidate("cur", hours=20.0, remaining=5000.0), candidate("soon", hours=10.0)]
    decision = _run(pool)
    assert (decision.action, decision.rule) == ("none", "gap")

    # 差 30 小时（≥ 24 小时阈值）→ 放行。
    pool[0] = candidate("cur", hours=40.0, remaining=5000.0)
    assert _run(pool).action == "switch"


def test_rule_7_gap_is_skipped_when_current_account_has_no_future_expiry():
    """当前账号查不到到期时间（或已过期）时没有差异约束：任何更紧迫目标都值得切。"""
    pool = [
        candidate("cur", hours=None, remaining=None, available=False),
        candidate("soon", hours=10.0),
    ]
    decision = _run(pool)
    assert decision.action == "switch"
    assert decision.target_account_id == "soon"

    pool[0] = candidate("cur", hours=-1.0, remaining=0.0)
    assert _run(pool).action == "switch"


def test_all_rules_pass_and_picks_the_most_urgent():
    pool = [
        candidate("cur", hours=200.0, remaining=5000.0, used_minutes_ago=60.0),
        candidate("mid", hours=30.0, remaining=8000.0),
        candidate("soon", hours=9.0, remaining=1000.0),
    ]
    decision = _run(pool)
    assert decision.action == "switch"
    assert decision.rule == "ok"
    assert decision.target_account_id == "soon"
    assert decision.hours_to_expiry == pytest.approx(9.0, abs=0.01)
    assert "账号 soon" in decision.reason
    assert decision.should_switch is True
    payload = decision.payload()
    assert payload["targetAccountId"] == "soon"
    assert payload["evaluatedAt"] == NOW.isoformat(timespec="seconds")


# ---------- 策略解析 ----------


def test_policy_round_trip_through_json_keys():
    policy = RotationPolicy(enabled=True, min_urgency_hours=6.5, min_gap_hours=1)
    restored = parse_policy(policy.payload())
    assert restored == policy


def test_parse_policy_strict_rejects_bad_values():
    with pytest.raises(RotationError):
        parse_policy({"enabled": "yes"}, strict=True)
    with pytest.raises(RotationError):
        parse_policy({"enabled": True, "cooldownMinutes": -1}, strict=True)


def test_parse_policy_is_lenient_when_reading_from_disk():
    """一条坏数据不应该让整个轮换停摆：读盘时退回默认值。"""
    assert parse_policy({"cooldownMinutes": -5}) == RotationPolicy()


# ---------- 策略与台账落盘 ----------


def test_store_round_trips_policy_and_ledger(tmp_path):
    store = RotationStore(tmp_path)
    assert store.load() == RotationPolicy()  # 文件不存在时给默认（关闭）
    assert store.last_switch_at() is None

    store.save(RotationPolicy(enabled=True, min_urgency_hours=12.0))
    assert RotationStore(tmp_path).load().min_urgency_hours == 12.0

    store.record(
        RotationLedgerEntry(
            at=NOW.isoformat(timespec="seconds"),
            action="switch",
            rule="ok",
            reason="切到账号 soon",
            target_account_id="soon",
            target_name="账号 soon",
            executed=True,
            detail="已切换（备份点 abc）",
        ),
        switched=True,
    )
    state = RotationStore(tmp_path).payload()
    assert state["lastEvaluatedAt"] == NOW.isoformat(timespec="seconds")
    assert state["lastSwitchAt"] == NOW.isoformat(timespec="seconds")
    assert state["history"][-1]["targetAccountId"] == "soon"
    assert state["history"][-1]["executed"] is True


def test_ledger_keeps_only_the_latest_entries(tmp_path):
    store = RotationStore(tmp_path)
    for index in range(25):
        store.record(
            RotationLedgerEntry(
                at=f"2026-10-20T12:{index:02d}:00",
                action="none",
                rule="urgency",
                reason=f"第 {index} 轮",
            ),
            switched=False,
        )
    history = store.history()
    assert len(history) == 20
    assert history[0].reason == "第 5 轮"


def test_rotation_section_and_tasks_do_not_clobber_each_other(tmp_path):
    """`schedules.json` 两处写入者共用一把锁：各写各的段，互不覆盖。"""
    executor = lambda key, ids: "ok"  # noqa: E731 - 单测里只为满足构造签名
    scheduler = Scheduler(tmp_path, executor=executor)
    scheduler.update("checkin", enabled=True, times=["07:30"], jitter_minutes=0)

    store = RotationStore(tmp_path)
    store.save(RotationPolicy(enabled=True, cooldown_minutes=30))
    store.record(
        RotationLedgerEntry(
            at="2026-10-20T12:00:00", action="none", rule="urgency", reason="不紧迫"
        ),
        switched=False,
    )

    # 计划任务再写一次：rotation 段必须还在。
    scheduler.update("keepalive", enabled=True)
    reloaded = RotationStore(tmp_path)
    assert reloaded.load() == RotationPolicy(enabled=True, cooldown_minutes=30)
    assert reloaded.history()[-1].reason == "不紧迫"

    # 反过来：轮换再写一次，tasks 段也还在。
    store.save(RotationPolicy(enabled=False))
    again = Scheduler(tmp_path, executor=executor)
    assert again.get("checkin").times == ("07:30",)


# ---------- 服务：判定 → 执行 → 台账 ----------


class _SwitchRecorder:
    def __init__(self, ok: bool = True, message: str = "已切换（备份点 snap-1）") -> None:
        self.calls: list[tuple[str, str]] = []
        self._ok = ok
        self._message = message

    def __call__(self, scope: str, account_id: str) -> tuple[bool, str]:
        self.calls.append((scope, account_id))
        return self._ok, self._message


def _service(
    tmp_path,
    *,
    running: bool,
    recorder: _SwitchRecorder,
    enabled: bool = True,
) -> RotationService:
    store = RotationStore(tmp_path)
    store.save(RotationPolicy(enabled=enabled))
    snapshot = RotationSnapshot(
        candidates=tuple(_switchable_pool()),
        current_account_id="cur",
        client_running=running,
    )
    return RotationService(
        store, snapshot=lambda scope: snapshot, switch=recorder, now=lambda: NOW
    )


def test_service_skips_while_disabled_and_writes_no_ledger(tmp_path):
    recorder = _SwitchRecorder()
    service = _service(tmp_path, running=False, recorder=recorder, enabled=False)

    result = service.evaluate()
    assert result["skipped"] == "自动轮换未启用"
    assert result["decision"] is None
    assert recorder.calls == []
    assert service.state()["history"] == []


def test_service_does_not_hot_switch_while_client_runs(tmp_path):
    recorder = _SwitchRecorder()
    service = _service(tmp_path, running=True, recorder=recorder)

    result = service.evaluate()
    assert result["decision"]["action"] == "switch"
    assert result["executed"] is False
    assert "客户端运行中" in result["detail"]
    assert recorder.calls == []
    assert service.state()["lastSwitchAt"] is None
    assert service.state()["history"][-1]["executed"] is False


def test_service_dry_run_never_writes_client(tmp_path):
    recorder = _SwitchRecorder()
    service = _service(tmp_path, running=False, recorder=recorder)

    result = service.evaluate(dry_run=True)
    assert result["decision"]["action"] == "switch"
    assert result["detail"] == "只推演，未执行切换"
    assert recorder.calls == []


def test_service_switches_when_client_is_closed(tmp_path):
    recorder = _SwitchRecorder()
    service = _service(tmp_path, running=False, recorder=recorder)

    result = service.evaluate()
    assert result["executed"] is True
    assert recorder.calls == [("workbuddy-cn", "soon")]
    state = service.state()
    assert state["lastSwitchAt"] == NOW.isoformat(timespec="seconds")
    assert state["history"][-1]["executed"] is True


def test_service_records_failed_switch_without_stamping_last_switch(tmp_path):
    recorder = _SwitchRecorder(ok=False, message="切换未成功：客户端被占用")
    service = _service(tmp_path, running=False, recorder=recorder)

    result = service.evaluate()
    assert result["executed"] is False
    assert "客户端被占用" in result["detail"]
    assert service.state()["lastSwitchAt"] is None


def test_service_manual_run_works_while_disabled(tmp_path):
    recorder = _SwitchRecorder()
    service = _service(tmp_path, running=False, recorder=recorder, enabled=False)

    result = service.evaluate(dry_run=True, manual=True)
    assert result["skipped"] is None
    assert result["decision"]["action"] == "switch"


def test_service_update_keeps_unspecified_thresholds(tmp_path):
    service = _service(tmp_path, running=False, recorder=_SwitchRecorder())

    service.update({"minUrgencyHours": 6.0})
    policy = service.state()["policy"]
    assert policy["minUrgencyHours"] == 6.0
    assert policy["cooldownMinutes"] == 120  # 没给的字段保持原值
    with pytest.raises(RotationError):
        service.update({"minRemainingCredits": -1})


def test_runner_evaluates_on_its_own_thread():
    class _Stub:
        def __init__(self) -> None:
            self.calls = 0

        def evaluate(self, **_kwargs) -> None:
            self.calls += 1

    stub = _Stub()
    runner = RotationRunner(stub, interval_seconds=0.01)  # type: ignore[arg-type]
    runner.start()
    deadline = time.monotonic() + 2.0
    while stub.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    runner.stop()
    assert stub.calls >= 1
    assert runner.running is False


def test_forget_account_nulls_ledger_target_but_keeps_entry(tmp_path):
    """永久删除账号只清 id，不抹台账。

    台账是「曾经切过谁」的审计记录，删行等于伪造历史；把 targetAccountId 置空、
    留着 targetName 与条数，既说明账号已不存在，又保住历史。
    """
    store = RotationStore(tmp_path)
    store.record(
        RotationLedgerEntry(
            at="2026-10-20T09:00:00+00:00",
            action="switch",
            rule="urgency",
            reason="积分即将到期",
            target_account_id="acc-gone",
            target_name="要删的号",
            executed=True,
            detail="",
        ),
        switched=True,
    )
    store.record(
        RotationLedgerEntry(
            at="2026-10-21T09:00:00+00:00",
            action="switch",
            rule="urgency",
            reason="积分即将到期",
            target_account_id="acc-keep",
            target_name="留着的号",
            executed=True,
            detail="",
        ),
        switched=True,
    )

    assert store.forget_account("acc-gone") == 1

    entries = RotationStore(tmp_path).history()
    assert len(entries) == 2  # 条数不变
    assert entries[0].target_account_id is None
    assert entries[0].target_name == "要删的号"  # 名字保留
    assert entries[1].target_account_id == "acc-keep"
    # 已经不在了，再摘一次返回 0
    assert store.forget_account("acc-gone") == 0
