"""计划任务调度器（P0 / G1）：让「保活 / 自动签到」偏好真正跑起来。

## 它解决什么

`preferences.json` 里的 `keepalive` / `autoCheckin` 在过去只是布尔字段，
全仓库没有任何循环消费它们 —— 开关点了没有效果。本模块提供**最小调度器**：

- 单后台线程 + 30 s tick，按**本地时区**的时点执行任务；
- 支持一天多时点、分钟粒度、随机抖动；
- 状态落盘 `schedules.json`（与 `preferences.json` 同目录、同风格）。

## 硬边界

1. **不引入第三方调度依赖**（apscheduler 之类）：需求只有「分钟粒度 + 多时点 + 抖动」，
   依赖越少越可控。
2. **抖动按 `(日期, key, 时点)` 派生** → 同一天同一时点的抖动**稳定**，
   不会每次 tick 重摇（否则「抖动」会退化成「随机漂移」，同一天可能来回跑或漏跑）。
3. **单次失败绝不退出线程**：异常写日志后继续下一 tick。
4. **错过只补一次**：应用关闭期间错过的时点，启动后只补跑「最近一个未执行的时点」，
   不堆积补全部 —— 与 `docs/FEATURES.md` F9 一致。

## 任务与执行体分离

调度器只负责「什么时候跑」，**不知道**任务具体做什么。执行体由调用方通过
`executor(key, account_ids) -> str` 注入（`main.py` 里接上活动/积分服务），
这样调度器本身可以脱离网络做单测。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

logger = logging.getLogger("chaierww")

FILENAME = "schedules.json"

#: 共享文件锁：`schedules.json` 现在有两处写入者 —— 计划任务（本模块）与自动轮换
#: （`domain/rotation.py`）。两边都必须「读整份 → 只改自己那一段 → 写整份」，
#: 共用同一把进程内锁（RLock，可重入）才不会互相把对方的段落抹掉。
FILE_LOCK = threading.RLock()


def read_schedules(path: Path) -> dict[str, Any]:
    """读整份 `schedules.json`；文件缺失或损坏时返回空 dict。"""
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def write_schedules(path: Path, payload: dict[str, Any]) -> None:
    """整份写回。调用方若要在「读 → 改 → 写」之间保持原子，请自行持有 `FILE_LOCK`。"""
    with FILE_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )


#: 任务键。语义见 `main.py` 的执行体：keepalive = 保活探测；checkin = 只签到；
#: activity_run = 全部可自动活动（含成长任务）；
#: travel_depart / travel_claim = 派猫猫旅行「只派出」/「只回领」两个独立动作。
#: 拆成两个任务是因为派出后猫要在路上待一段时间，同一次执行里做不完两件事。
TASK_KEYS: tuple[str, ...] = (
    "keepalive",
    "checkin",
    "activity_run",
    "travel_depart",
    "travel_claim",
)

#: 抖动的可选半径（分钟）。实际偏移在 `[-n, +n]` 内确定性派生。
JITTER_CHOICES: tuple[int, ...] = (0, 5, 15, 30)

DEFAULT_TICK_SECONDS = 30.0

#: 每个计划任务保留的执行日志条数（最近 N 次）。每跑一次追加一条（时间 + 结果摘要），
#: 超出只留最近的，避免 `schedules.json` 随运行天数无限增长。
HISTORY_KEEP = 7

_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")

#: 默认时点错开一点，避免同一时刻几个任务挤在一起打上游。
_DEFAULT_TIMES: dict[str, tuple[str, ...]] = {
    "keepalive": ("09:00",),
    "checkin": ("09:10",),
    "activity_run": ("09:20",),
    # 派出放在签到/活动之后；回领留到下午，给猫留出在路上的时间。
    "travel_depart": ("09:30",),
    "travel_claim": ("15:30",),
}


class SchedulerError(RuntimeError):
    """调度器相关错误。消息面向用户，可直接展示。"""


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """一个计划任务。``account_ids`` 为空 = 号池里全部账号。"""

    key: str
    enabled: bool = False
    times: tuple[str, ...] = ()
    jitter_minutes: int = 0
    account_ids: tuple[str, ...] = ()
    #: 最近一次执行的**实际时刻**。它同时充当「这一档时点今天跑过没有」的判据：
    #: 只要某个时点的计划时刻 ≤ 这个时刻，就认为该档今天已经跑过。
    last_run_at: str | None = None
    last_result: str = ""
    #: 最近 `HISTORY_KEEP` 次执行记录（`{"at": ..., "result": ...}`），旧的在前。
    history: tuple[dict[str, str], ...] = ()

    def payload(self, next_run_at: str | None = None) -> dict[str, Any]:
        return {
            "key": self.key,
            "enabled": self.enabled,
            "times": list(self.times),
            "jitterMinutes": self.jitter_minutes,
            "accountIds": list(self.account_ids),
            "lastRunAt": self.last_run_at,
            "lastResult": self.last_result,
            "history": [dict(item) for item in self.history],
            "nextRunAt": next_run_at,
        }


def _parse_hhmm(text: str) -> tuple[int, int]:
    matched = _TIME_RE.match(text.strip())
    if not matched:
        raise SchedulerError(f"时点格式应为 HH:MM，收到「{text}」")
    return int(matched.group(1)), int(matched.group(2))


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _jitter_offset(day: datetime, key: str, time_text: str, jitter: int) -> int:
    """同一天、同一任务、同一时点 → 恒定偏移，落于 ``[-jitter, +jitter]``。"""
    if jitter <= 0:
        return 0
    digest = sha256(f"{day.date().isoformat()}|{key}|{time_text}".encode()).digest()
    return int.from_bytes(digest[:4], "big") % (2 * jitter + 1) - jitter


#: 默认开启的任务。保活 / 签到是「默认就该在跑」的日常动作（原先由常规设置里的
#: 同名偏好开关给出同样的默认值），所以这里也默认开；全部活动是重动作，默认关。
_DEFAULT_ENABLED: tuple[str, ...] = ("keepalive", "checkin")


def _default_tasks() -> dict[str, TaskSpec]:
    return {
        key: TaskSpec(
            key=key,
            enabled=key in _DEFAULT_ENABLED,
            times=_DEFAULT_TIMES[key],
            jitter_minutes=15,
        )
        for key in TASK_KEYS
    }


class Scheduler:
    """单后台线程 + 固定 tick 的最小调度器。"""

    def __init__(
        self,
        home: Path,
        *,
        executor: Callable[[str, tuple[str, ...]], str],
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._file = home / FILENAME
        self._executor = executor
        self._tick = max(1.0, float(tick_seconds))
        self._now = now or (lambda: datetime.now().astimezone())
        self._lock = threading.RLock()
        self._tasks = self._read()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---------- 持久化 ----------

    def _read(self) -> dict[str, TaskSpec]:
        tasks = _default_tasks()
        raw = read_schedules(self._file)
        stored = raw.get("tasks")
        if not isinstance(stored, dict):
            return tasks
        for key, item in stored.items():
            if key not in tasks or not isinstance(item, dict):
                continue
            base = tasks[key]
            tasks[key] = replace(
                base,
                enabled=bool(item.get("enabled", base.enabled)),
                times=tuple(_string_list(item.get("times"))) or base.times,
                jitter_minutes=_sanitize_jitter(item.get("jitterMinutes"), base.jitter_minutes),
                account_ids=tuple(_string_list(item.get("accountIds"))),
                last_run_at=(
                    item.get("lastRunAt") if isinstance(item.get("lastRunAt"), str) else None
                ),
                last_result=(
                    item.get("lastResult") if isinstance(item.get("lastResult"), str) else ""
                ),
                history=_read_history(item.get("history")),
            )
            # 老版本的存档只留了「上次结果」一条。补齐成日志的第一条，
            # 免得升级后界面上明明跑过、日志区却是空的。
            migrated = tasks[key]
            if not migrated.history and migrated.last_result:
                tasks[key] = replace(
                    migrated,
                    history=(
                        {
                            "at": migrated.last_run_at or "",
                            "result": migrated.last_result,
                        },
                    ),
                )
            # 时点里若有非法项，读盘时直接丢弃，避免一条坏数据让整个调度器停摆。
            safe_times = _safe_times(tasks[key].times)
            tasks[key] = replace(tasks[key], times=safe_times or _DEFAULT_TIMES[key])
        return tasks

    def _write(self) -> None:
        # 读整份再写回：`rotation` 段由 `domain/rotation.py` 维护，不能被这里抹掉。
        with FILE_LOCK:
            payload = read_schedules(self._file)
            payload["tasks"] = {
                key: spec.payload() for key, spec in self._tasks.items()
            }
            write_schedules(self._file, payload)

    # ---------- 查询 ----------

    def list(self) -> list[TaskSpec]:
        with self._lock:
            return [self._tasks[key] for key in TASK_KEYS if key in self._tasks]

    def payloads(self) -> list[dict[str, Any]]:
        now = self._now()
        with self._lock:
            specs = [self._tasks[key] for key in TASK_KEYS if key in self._tasks]
        return [spec.payload(self._next_run_at(spec, now)) for spec in specs]

    def get(self, key: str) -> TaskSpec:
        with self._lock:
            spec = self._tasks.get(key)
        if spec is None:
            raise SchedulerError(f"未知的计划任务：{key}")
        return spec

    # ---------- 更新 ----------

    def update(
        self,
        key: str,
        *,
        enabled: bool | None = None,
        times: Iterable[str] | None = None,
        jitter_minutes: int | None = None,
        account_ids: Iterable[str] | None = None,
    ) -> TaskSpec:
        with self._lock:
            current = self._tasks.get(key)
            if current is None:
                raise SchedulerError(f"未知的计划任务：{key}")
            new_times = current.times
            if times is not None:
                new_times = _validate_times(times)
            new_jitter = current.jitter_minutes
            if jitter_minutes is not None:
                new_jitter = _validate_jitter(jitter_minutes)
            new_accounts = current.account_ids
            if account_ids is not None:
                new_accounts = tuple(_string_list(account_ids))
            updated = replace(
                current,
                enabled=current.enabled if enabled is None else bool(enabled),
                times=new_times,
                jitter_minutes=new_jitter,
                account_ids=new_accounts,
            )
            self._tasks[key] = updated
            self._write()
        return updated

    def set_enabled(self, key: str, enabled: bool) -> None:
        """启动/偏好同步时的静默启用：未知 key 直接忽略，不抛错。"""
        with self._lock:
            current = self._tasks.get(key)
            if current is None or current.enabled == bool(enabled):
                return
            self._tasks[key] = replace(current, enabled=bool(enabled))
            self._write()

    def is_enabled(self, key: str) -> bool:
        with self._lock:
            spec = self._tasks.get(key)
        return bool(spec and spec.enabled)

    # ---------- 到期判定 ----------

    def _scheduled_for(self, day: datetime, time_text: str, jitter: int, key: str) -> datetime:
        hour, minute = _parse_hhmm(time_text)
        base = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
        return base + timedelta(minutes=_jitter_offset(day, key, time_text, jitter))

    def due_occurrence(self, spec: TaskSpec, now: datetime | None = None) -> datetime | None:
        """返回该任务「今天尚未执行过的、最近的、已到时点」；没有则 None。

        - 同一时点只跑一次（靠 ``last_run_at`` 记住已跑过的时点）；
        - 多个时点都错过时，只取**最近一个** —— 启动补跑一次，不堆积补全部。
        """
        moment = now or self._now()
        if not spec.enabled or not spec.times:
            return None
        last = _parse_iso(spec.last_run_at)
        best: datetime | None = None
        for text in spec.times:
            scheduled = self._scheduled_for(moment, text, spec.jitter_minutes, spec.key)
            if scheduled > moment:
                continue
            if last is not None and scheduled <= last:
                continue
            if best is None or scheduled > best:
                best = scheduled
        return best

    def due_tasks(self, now: datetime | None = None) -> list[tuple[TaskSpec, datetime]]:
        moment = now or self._now()
        with self._lock:
            specs = [self._tasks[key] for key in TASK_KEYS if key in self._tasks]
        found: list[tuple[TaskSpec, datetime]] = []
        for spec in specs:
            occurrence = self.due_occurrence(spec, moment)
            if occurrence is not None:
                found.append((spec, occurrence))
        found.sort(key=lambda pair: pair[1])
        return found

    def _next_run_at(self, spec: TaskSpec, now: datetime) -> str | None:
        if not spec.enabled or not spec.times:
            return None
        best: datetime | None = None
        for text in spec.times:
            for day in (now, now + timedelta(days=1)):
                scheduled = self._scheduled_for(day, text, spec.jitter_minutes, spec.key)
                if scheduled > now and (best is None or scheduled < best):
                    best = scheduled
        return best.isoformat(timespec="seconds") if best else None

    # ---------- 执行 ----------

    def run_due(self, now: datetime | None = None) -> list[dict[str, Any]]:
        """跑掉所有到期任务，返回各自的执行结果。

        记录的是**实际执行时刻**而不是计划时刻 —— 补跑时两者可能差好几小时，
        界面上写「上次结果（时间）」就该是真正发生的那一刻。
        计划时刻只用于判断「这一档今天跑过没有」。
        """
        moment = now or self._now()
        outcomes: list[dict[str, Any]] = []
        for spec, _occurrence in self.due_tasks(moment):
            outcomes.append(self._execute(spec, at=moment))
        return outcomes

    def run_now(self, key: str) -> dict[str, Any]:
        spec = self.get(key)
        return self._execute(spec, at=self._now())

    def _execute(self, spec: TaskSpec, *, at: datetime) -> dict[str, Any]:
        try:
            result = self._executor(spec.key, spec.account_ids)
        except Exception as exc:  # noqa: BLE001 - 单次失败只记结果，绝不退出线程
            logger.exception("计划任务执行异常：%s", spec.key)
            result = f"执行异常：{type(exc).__name__}：{exc}"
        stamped = self._mark(spec.key, at=at, result=result)
        logger.info("计划任务 %s 执行完成：%s", spec.key, result)
        return {"key": spec.key, "result": result, "lastRunAt": stamped}

    def _mark(self, key: str, *, at: datetime, result: str) -> str | None:
        stamp = at.isoformat(timespec="seconds")
        with self._lock:
            current = self._tasks.get(key)
            if current is None:
                return None
            history = (*current.history, {"at": stamp, "result": result})[-HISTORY_KEEP:]
            self._tasks[key] = replace(
                current, last_run_at=stamp, last_result=result, history=history
            )
            self._write()
        return stamp

    # ---------- 生命周期 ----------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="chaierww-scheduler", daemon=True
        )
        self._thread.start()
        logger.info("计划任务调度器已启动（tick %ss）", self._tick)

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception:  # noqa: BLE001 - tick 级异常也不能让线程死掉
                logger.exception("计划任务调度 tick 异常")
            self._stop.wait(self._tick)


# ---------- 输入清洗 ----------


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _read_history(value: Any) -> tuple[dict[str, str], ...]:
    """读盘时清洗执行日志：坏项直接丢，只认 `at` 非空且 `result` 是字符串的条目。"""
    if not isinstance(value, list):
        return ()
    entries: list[dict[str, str]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        at = item.get("at")
        if not isinstance(at, str) or not at:
            continue
        result = item.get("result")
        entries.append({"at": at, "result": result if isinstance(result, str) else ""})
    return tuple(entries[-HISTORY_KEEP:])


def _validate_times(times: Iterable[str]) -> tuple[str, ...]:
    cleaned = [str(item).strip() for item in times if str(item).strip()]
    if not cleaned:
        raise SchedulerError("至少要保留一个执行时点")
    for text in cleaned:
        _parse_hhmm(text)
    # 去重 + 排序，避免同一时点写两遍。
    return tuple(sorted(dict.fromkeys(cleaned)))


def _safe_times(times: Iterable[str]) -> tuple[str, ...]:
    kept: list[str] = []
    for text in times:
        try:
            _parse_hhmm(text)
        except SchedulerError:
            continue
        kept.append(text)
    return tuple(sorted(dict.fromkeys(kept)))


def _sanitize_jitter(value: Any, fallback: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    return value if value in JITTER_CHOICES else fallback


def _validate_jitter(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value not in JITTER_CHOICES:
        raise SchedulerError(f"抖动只能是 {'/'.join(str(item) for item in JITTER_CHOICES)} 分钟")
    return value
