"""号池额度闭环测试。

两层：
1. `plan_quota` 是纯函数：只按余额与当前号给结论，不碰线程/文件；
2. `QuotaService` 用假快照 + 假回调动验证「出池 / 不热切 / 冷却 / 落台账」。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from chaierww.domain.quota_guard import (
    COOLDOWN_MINUTES,
    QuotaAccount,
    QuotaService,
    QuotaSnapshot,
    QuotaStore,
    plan_quota,
)

TZ = timezone(timedelta(hours=8))


def _at(minute: int = 0) -> datetime:
    return datetime(2026, 11, 11, 10, minute, tzinfo=TZ)


def _account(
    account_id: str,
    credits=None,
    *,
    available: bool = True,
    current: bool = False,
    excluded: bool = False,
    switchable: bool = True,
) -> QuotaAccount:
    return QuotaAccount(
        account_id=account_id,
        display_name=f"账号{account_id}",
        scope="workbuddy-cn",
        credits=credits,
        available=available,
        switchable=switchable,
        excluded=excluded,
        current=current,
    )


def test_unknown_credits_are_neither_exhausted_nor_target():
    plan = plan_quota(
        [
            _account("a", 0, current=True),
            _account("b", None, available=False),
        ]
    )
    assert [item.account_id for item in plan.exhausted] == ["a"]
    # b 余额未知，不能当切换目标（宁可不动，也不赌）。
    assert plan.target is None
    assert "没有余额足够" in plan.reason


def test_exhausted_current_switches_to_highest_available():
    plan = plan_quota(
        [
            _account("a", 0, current=True),
            _account("b", 120),
            _account("c", 900),
            _account("d", 50),  # 低额度也不该被选中
        ]
    )
    assert plan.current is not None and plan.current.account_id == "a"
    assert plan.target is not None and plan.target.account_id == "c"
    assert [item.account_id for item in plan.exhausted] == ["a"]
    assert [item.account_id for item in plan.warnings] == ["d"]


def test_low_current_is_warned_but_not_switched():
    plan = plan_quota([_account("a", 50, current=True), _account("b", 900)])
    assert [item.account_id for item in plan.warnings] == ["a"]
    assert plan.target is None
    assert "无需换号" in plan.reason


def test_excluded_or_unknown_current_switches_away():
    plan = plan_quota(
        [
            _account("a", None, available=False, current=True),
            _account("b", 800),
        ]
    )
    assert plan.target is not None and plan.target.account_id == "b"
    assert "查不到" in plan.reason


def test_relogin_required_account_is_not_a_target():
    plan = plan_quota(
        [
            _account("a", 0, current=True),
            _account("b", 900, switchable=False),
        ]
    )
    assert plan.target is None


def _service(tmp_path, accounts, *, client_running: bool = False, now=_at):
    store = QuotaStore(tmp_path)
    excluded: list[str] = []
    switched: list[tuple[str, str]] = []

    def switch(scope: str, account_id: str):
        switched.append((scope, account_id))
        return True, f"已切到 {account_id}"

    service = QuotaService(
        store,
        snapshot=lambda: QuotaSnapshot(accounts=tuple(accounts), client_running=client_running),
        exclude=excluded.append,
        switch=switch,
        now=now,
    )
    return service, store, excluded, switched


def test_service_excludes_exhausted_and_switches_when_client_idle(tmp_path):
    service, store, excluded, switched = _service(
        tmp_path,
        [_account("a", 0, current=True), _account("b", 700)],
    )

    result = service.evaluate()

    assert excluded == ["a"]
    assert switched == [("workbuddy-cn", "b")]
    assert result["executed"] is True
    state = store.payload()
    assert state["last"]["switched"] is True
    assert state["last"]["targetAccountId"] == "b"
    assert state["history"][-1]["actions"]


def test_service_does_not_hot_switch_while_client_running(tmp_path):
    service, _store, excluded, switched = _service(
        tmp_path,
        [_account("a", 0, current=True), _account("b", 700)],
        client_running=True,
    )

    result = service.evaluate()

    assert excluded == ["a"]  # 出池照做
    assert switched == []  # 但不热切
    assert "客户端运行中" in result["detail"]


def test_service_honours_cooldown_between_switches(tmp_path):
    accounts = [_account("a", 0, current=True), _account("b", 700)]
    service, store, _excluded, switched = _service(tmp_path, accounts, now=_at)
    service.evaluate()
    assert len(switched) == 1

    # 30 分钟后再次评估：仍满足换号条件，但冷却未过。
    later = QuotaService(
        store,
        snapshot=lambda: QuotaSnapshot(accounts=tuple(accounts), client_running=False),
        exclude=lambda _id: None,
        switch=lambda scope, account_id: switched.append((scope, account_id)) or (True, "切了"),
        now=lambda: _at(30),
    )
    result = later.evaluate()
    assert len(switched) == 1
    assert f"不足 {COOLDOWN_MINUTES} 分钟" in result["detail"]


def test_dry_run_touches_nothing(tmp_path):
    service, store, excluded, switched = _service(
        tmp_path,
        [_account("a", 0, current=True), _account("b", 700)],
    )

    result = service.evaluate(dry_run=True)

    assert excluded == []
    assert switched == []
    assert result["plan"]["exhausted"] == ["a"]
    assert store.payload()["last"] is None
