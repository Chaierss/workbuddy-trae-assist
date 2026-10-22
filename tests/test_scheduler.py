"""计划任务调度器单测（P0 / G1）。

调度器把「什么时候跑」与「跑什么」分开：这里注入一个假的执行体，
所以全部用例都不发网络请求。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from chaierww.domain.scheduler import Scheduler, SchedulerError, _jitter_offset

TZ = timezone(timedelta(hours=8))


def _at(hour: int, minute: int = 0, *, day: int = 20) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


class _Recorder:
    """假的执行体：记录每次被调到时的 (key, account_ids)。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def __call__(self, key: str, account_ids: tuple[str, ...]) -> str:
        self.calls.append((key, account_ids))
        return f"已执行 {key}"


def _build(tmp_path, recorder: _Recorder | None = None, *, now=None) -> Scheduler:
    return Scheduler(
        tmp_path,
        executor=recorder or _Recorder(),
        now=(lambda: now) if now is not None else None,
    )


def test_same_time_point_runs_only_once(tmp_path):
    recorder = _Recorder()
    scheduler = _build(tmp_path, recorder)
    scheduler.update("keepalive", enabled=True, times=["09:00"], jitter_minutes=0)
    # 只留 keepalive 一个任务在跑：签到与保活默认都开着，别的任务会干扰调用计数。
    scheduler.update("checkin", enabled=False)
    scheduler.update("activity_run", enabled=False)

    scheduler.run_due(_at(9, 5))
    assert len(recorder.calls) == 1

    # 同一时点在当天再 tick 几次，不应重复执行。
    scheduler.run_due(_at(9, 30))
    scheduler.run_due(_at(10, 0))
    assert len(recorder.calls) == 1

    # 第二天到点后又该跑一次。
    scheduler.run_due(_at(9, 5, day=21))
    assert len(recorder.calls) == 2


def test_not_yet_due_does_not_run(tmp_path):
    recorder = _Recorder()
    scheduler = _build(tmp_path, recorder)
    scheduler.update("checkin", enabled=True, times=["09:00"], jitter_minutes=0)
    scheduler.update("keepalive", enabled=False)

    scheduler.run_due(_at(8, 59))
    assert recorder.calls == []


def test_disabled_task_never_runs(tmp_path):
    recorder = _Recorder()
    scheduler = _build(tmp_path, recorder)
    scheduler.update("keepalive", enabled=False)
    scheduler.update("checkin", enabled=False, times=["09:00"], jitter_minutes=0)

    scheduler.run_due(_at(12, 0))
    assert recorder.calls == []
    assert scheduler.due_occurrence(scheduler.get("checkin"), _at(12, 0)) is None


def test_multi_times_backfills_only_the_latest_missed(tmp_path):
    """一天多个时点都错过时，只补跑最近一个，不堆积补全部。"""
    recorder = _Recorder()
    scheduler = _build(tmp_path, recorder)
    scheduler.update("keepalive", enabled=True, times=["06:00", "12:00", "18:00"], jitter_minutes=0)
    scheduler.update("checkin", enabled=False)

    # 18:05 才启动：应该只跑 18:00 这一档。
    scheduler.run_due(_at(18, 5))
    assert len(recorder.calls) == 1
    # 记录的是实际执行时刻（18:05），不是计划时刻（18:00）—— 补跑时两者会差很远。
    assert scheduler.get("keepalive").last_run_at == _at(18, 5).isoformat(timespec="seconds")

    occurrence = scheduler.due_occurrence(scheduler.get("keepalive"), _at(18, 6))
    assert occurrence is None  # 更早的 06:00 / 12:00 不会被翻出来补跑

    # 次日 12:05，12:00 是最近一个已到时点。
    scheduler.run_due(_at(12, 5, day=21))
    assert len(recorder.calls) == 2


def test_jitter_is_stable_within_the_same_day(tmp_path):
    scheduler = _build(tmp_path)
    scheduler.update("keepalive", enabled=True, times=["09:00"], jitter_minutes=30)

    # 同一天用两个不同的「现在」算下次时点，结果必须一致（不是每次重摇）。
    early = scheduler._next_run_at(scheduler.get("keepalive"), _at(0, 1))
    late = scheduler._next_run_at(scheduler.get("keepalive"), _at(8, 0))
    assert early == late
    assert early is not None

    # 换个日期，抖动可以不同，但同样稳定。
    other = scheduler._next_run_at(scheduler.get("keepalive"), _at(0, 1, day=21))
    again = scheduler._next_run_at(scheduler.get("keepalive"), _at(8, 0, day=21))
    assert other == again


def test_jitter_stays_inside_the_window(tmp_path):
    for jitter in (0, 5, 15, 30):
        offsets = {
            _jitter_offset(_at(0, 0), "keepalive", f"{hour:02d}:00", jitter) for hour in range(24)
        }
        assert all(-jitter <= value <= jitter for value in offsets)
        if jitter == 0:
            assert offsets == {0}


def test_jitter_is_deterministic_across_instances(tmp_path):
    first = _build(tmp_path)
    second = _build(tmp_path)
    first.update("keepalive", enabled=True, times=["09:00"], jitter_minutes=15)
    second.update("keepalive", enabled=True, times=["09:00"], jitter_minutes=15)

    scheduled_a = first._next_run_at(first.get("keepalive"), _at(0, 1))
    scheduled_b = second._next_run_at(second.get("keepalive"), _at(0, 1))
    assert scheduled_a == scheduled_b


def test_run_now_records_result_and_time(tmp_path):
    recorder = _Recorder()
    scheduler = _build(tmp_path, recorder, now=_at(10, 0))

    outcome = scheduler.run_now("activity_run")
    assert outcome["result"] == "已执行 activity_run"
    assert outcome["lastRunAt"] == _at(10, 0).isoformat(timespec="seconds")

    task = scheduler.get("activity_run")
    assert task.last_result == "已执行 activity_run"
    assert recorder.calls == [("activity_run", ())]


def test_executor_failure_does_not_raise(tmp_path):
    def broken(key: str, account_ids: tuple[str, ...]) -> str:
        raise RuntimeError("上游炸了")

    scheduler = Scheduler(tmp_path, executor=broken, now=lambda: _at(10, 0))
    outcome = scheduler.run_now("keepalive")
    assert "执行异常" in outcome["result"]
    assert scheduler.get("keepalive").last_run_at is not None


def test_history_keeps_only_the_latest_runs(tmp_path):
    """执行日志只留最近 HISTORY_KEEP 条：多的丢最旧的，不是无限长。"""
    scheduler = _build(tmp_path)
    keep = 7
    for index in range(keep + 3):
        moment = _at(0, 0) + timedelta(minutes=index)
        scheduler._execute(scheduler.get("checkin"), at=moment)

    task = scheduler.get("checkin")
    assert len(task.history) == keep
    # 留下的是最后 7 条，最新的在末尾
    expected = [( _at(0, 0) + timedelta(minutes=i)).isoformat(timespec="seconds") for i in range(3, keep + 3)]
    assert [entry["at"] for entry in task.history] == expected
    assert task.history[-1]["result"] == "已执行 checkin"


def test_history_survives_reload_and_drops_broken_entries(tmp_path):
    """落盘再读回来要保住日志；脏条目（缺 at、类型不对）直接丢，不能让调度器起不来。"""
    scheduler = _build(tmp_path)
    scheduler._execute(scheduler.get("checkin"), at=_at(10, 0))

    raw = json.loads((tmp_path / "schedules.json").read_text(encoding="utf-8"))
    raw["tasks"]["checkin"]["history"] = [
        {"at": "2026-10-19T09:00:00", "result": "旧的一次"},
        {"result": "没有时间戳"},
        "不是对象",
        {"at": "", "result": "空时间戳"},
        {"at": "2026-10-20T10:00:00", "result": "新的一次"},
    ]
    (tmp_path / "schedules.json").write_text(
        json.dumps(raw, ensure_ascii=False), encoding="utf-8"
    )

    reloaded = _build(tmp_path).get("checkin")
    assert [entry["result"] for entry in reloaded.history] == ["旧的一次", "新的一次"]


def test_state_persists_across_instances(tmp_path):
    first = _build(tmp_path)
    first.update("checkin", enabled=True, times=["07:30", "19:00"], jitter_minutes=5)

    second = _build(tmp_path)
    task = second.get("checkin")
    assert task.enabled is True
    assert task.times == ("07:30", "19:00")
    assert task.jitter_minutes == 5


def test_update_validates_times_and_jitter(tmp_path):
    scheduler = _build(tmp_path)
    with pytest.raises(SchedulerError):
        scheduler.update("keepalive", times=["25:00"])
    with pytest.raises(SchedulerError):
        scheduler.update("keepalive", times=[])
    with pytest.raises(SchedulerError):
        scheduler.update("keepalive", jitter_minutes=7)
    with pytest.raises(SchedulerError):
        scheduler.get("does-not-exist")


def test_update_dedupes_and_sorts_times(tmp_path):
    scheduler = _build(tmp_path)
    task = scheduler.update("keepalive", times=["19:00", "07:30", "07:30"])
    assert task.times == ("07:30", "19:00")


def test_set_enabled_ignores_unknown_key(tmp_path):
    scheduler = _build(tmp_path)
    scheduler.set_enabled("nope", True)  # 不抛错
    assert scheduler.is_enabled("nope") is False


def test_account_scope_is_passed_to_executor(tmp_path):
    recorder = _Recorder()
    scheduler = _build(tmp_path, recorder, now=_at(10, 0))
    scheduler.update("checkin", account_ids=["a-1", "a-2"])

    scheduler.run_now("checkin")
    assert recorder.calls == [("checkin", ("a-1", "a-2"))]


def test_keepalive_and_checkin_are_enabled_by_default(tmp_path):
    """保活 / 签到默认就开（原先由常规设置的偏好给出同样的默认值），全部活动默认关。"""
    scheduler = _build(tmp_path)
    assert scheduler.get("keepalive").enabled is True
    assert scheduler.get("checkin").enabled is True
    assert scheduler.get("activity_run").enabled is False
