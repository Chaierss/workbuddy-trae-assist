"""积分到期自动轮换（P1 / G6）：在积分快过期前，把默认账号轮换到「最紧迫」的那个。

## 判定顺序（对标参考实现的功能语义，不抄它的代码）

依次判定，任一不过就「本轮不切」，并把**是哪一条挡住**写进台账：

1. **有效账号**：凭据可切（不是「需重新登录」）、积分查得到、有剩余、且到期时间在未来；
2. **紧迫度**：最紧迫那个账号的剩余有效期超过 `min_urgency_hours`（默认 72 小时）→ 不切；
3. **已是目标**：当前账号就是那个「最紧迫」的账号 → 不切；
4. **冷却期**：距上次轮换不足 `cooldown_minutes`（默认 120 分钟）→ 不切；
5. **活跃保护**：当前账号在 `active_guard_minutes`（默认 30 分钟）内被用过 → 不切；
6. **价值过滤**：目标的剩余积分低于 `min_remaining_credits` → 不值得切；
7. **防抖动**：目标只比当前账号早 `min_gap_hours`（默认 24 小时）以内到期 → 不值得切。

## 生效边界（界面上必须写同一句）

只改「后续会话使用的默认账号」，**不热切当前会话**：客户端运行中一律不执行
（等下一轮），客户端未运行时才落一次切换；切换后需重启客户端才生效。
这也是本模块 `evaluate()` 里 `client_running` 那道闸门存在的唯一原因。

## 落位

- 决策是**纯函数** `decide()`：不碰文件、不发网络，单测直接调；
- 策略与台账落在 `schedules.json` 的 `rotation` 段，与计划任务**共用一个文件、一把锁**
  （`scheduler.FILE_LOCK`），两边都不会覆盖对方那一段；
- 周期执行由 `RotationRunner`（后台线程，15 分钟一轮）负责；「取快照」和「执行切换」
  都是从外面注入的回调 —— 本模块自己不发网络请求、不写客户端文件，所以能脱离网络单测。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from chaierww.domain.scheduler import (
    FILE_LOCK,
    read_schedules,
    write_schedules,
)
from chaierww.domain.scheduler import FILENAME as SCHEDULES_FILENAME

logger = logging.getLogger("chaierww")

#: `schedules.json` 里的段落名。
SECTION = "rotation"

#: 后台评估间隔（秒）。判定本身很便宜，但取快照要读客户端状态、查积分，不必跟着
#: 调度器的 30 s tick 跑。
EVAL_INTERVAL_SECONDS = 900.0

#: 台账保留条数（最近 N 条）。
HISTORY_KEEP = 20

#: 默认作用域。轮换语义来自 WorkBuddy 的「防积分过期」，故默认只作用于它。
DEFAULT_SCOPE = "workbuddy-cn"


class RotationError(RuntimeError):
    """自动轮换相关错误。消息面向用户，可直接展示。"""


@dataclass(frozen=True, slots=True)
class RotationPolicy:
    """轮换策略。默认**关闭**，须在设置里手动开。"""

    enabled: bool = False
    scope: str = DEFAULT_SCOPE
    min_urgency_hours: float = 72.0
    cooldown_minutes: int = 120
    active_guard_minutes: int = 30
    min_remaining_credits: float = 100.0
    min_gap_hours: float = 24.0

    def payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "scope": self.scope,
            "minUrgencyHours": self.min_urgency_hours,
            "cooldownMinutes": self.cooldown_minutes,
            "activeGuardMinutes": self.active_guard_minutes,
            "minRemainingCredits": self.min_remaining_credits,
            "minGapHours": self.min_gap_hours,
        }


@dataclass(frozen=True, slots=True)
class Candidate:
    """参与判定的一个账号。``remaining`` / ``expires_at`` 查不到时为 None（≠ 0）。"""

    account_id: str
    display_name: str
    scope: str
    remaining: float | None = None
    expires_at: int | None = None
    available: bool = False
    switchable: bool = True
    last_used_at: str | None = None

    @property
    def eligible(self) -> bool:
        """判定第 1 条只看「不依赖时间」的那半：凭据可切 + 积分查得到 + 有剩余。"""
        return (
            self.switchable
            and self.available
            and self.remaining is not None
            and self.remaining > 0
        )


@dataclass(frozen=True, slots=True)
class RotationDecision:
    """一轮判定的结论。``rule`` 标明是哪一条判定给出的结论（台账与单测都靠它）。"""

    action: str  # "switch" | "none"
    rule: str
    reason: str
    evaluated_at: str
    current_account_id: str | None = None
    target_account_id: str | None = None
    target_display_name: str = ""
    hours_to_expiry: float | None = None
    remaining: float | None = None

    @property
    def should_switch(self) -> bool:
        return self.action == "switch" and bool(self.target_account_id)

    def payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "rule": self.rule,
            "reason": self.reason,
            "evaluatedAt": self.evaluated_at,
            "currentAccountId": self.current_account_id,
            "targetAccountId": self.target_account_id,
            "targetName": self.target_display_name,
            "hoursToExpiry": self.hours_to_expiry,
            "remaining": self.remaining,
        }


@dataclass(frozen=True, slots=True)
class RotationLedgerEntry:
    """台账的一行：某轮判定的结论，以及「到底切了没有、为什么没切」。"""

    at: str
    action: str
    rule: str
    reason: str
    target_account_id: str | None = None
    target_name: str = ""
    executed: bool = False
    detail: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "at": self.at,
            "action": self.action,
            "rule": self.rule,
            "reason": self.reason,
            "targetAccountId": self.target_account_id,
            "targetName": self.target_name,
            "executed": self.executed,
            "detail": self.detail,
        }


def _parse_iso(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def decide(
    candidates: Iterable[Candidate],
    policy: RotationPolicy,
    *,
    current_account_id: str | None,
    last_switch_at: str | None = None,
    now: datetime | None = None,
) -> RotationDecision:
    """按七条判定给出结论。纯函数：不读文件、不发网络。"""
    moment = now or datetime.now().astimezone()
    stamp = moment.isoformat(timespec="seconds")
    now_ts = int(moment.timestamp())
    pool = list(candidates)

    def blocked(rule: str, reason: str, target: Candidate | None = None) -> RotationDecision:
        return RotationDecision(
            action="none",
            rule=rule,
            reason=reason,
            evaluated_at=stamp,
            current_account_id=current_account_id,
            target_account_id=target.account_id if target is not None else None,
            target_display_name=target.display_name if target is not None else "",
            hours_to_expiry=(
                (target.expires_at - now_ts) / 3600
                if target is not None and target.expires_at
                else None
            ),
            remaining=target.remaining if target is not None else None,
        )

    # 1 有效账号：凭据可切 + 积分查得到 + 有剩余 + 未过期。
    valid = [
        item
        for item in pool
        if item.eligible and item.expires_at is not None and item.expires_at > now_ts
    ]
    if not valid:
        return blocked(
            "valid_accounts",
            "没有可参与轮换的有效账号（凭据可切、积分查得到、有剩余、未过期）",
        )

    # 2 紧迫度：最紧迫的那个还在阈值之外，就没必要动。
    target = min(valid, key=lambda item: item.expires_at or 0)
    hours = (target.expires_at - now_ts) / 3600
    if hours > policy.min_urgency_hours:
        return blocked(
            "urgency",
            f"最紧迫的账号也要 {hours:.1f} 小时才到期"
            f"（阈值 {policy.min_urgency_hours:g} 小时），无需轮换",
            target,
        )

    # 3 已经是目标账号。
    if current_account_id and current_account_id == target.account_id:
        return blocked("already_target", "当前用的就是最紧迫的账号，不动", target)

    # 4 冷却期。
    last = _parse_iso(last_switch_at)
    if last is not None and policy.cooldown_minutes > 0:
        elapsed = (moment - last).total_seconds() / 60
        if elapsed < policy.cooldown_minutes:
            return blocked(
                "cooldown",
                f"距上次轮换 {elapsed:.0f} 分钟，"
                f"冷却期 {policy.cooldown_minutes} 分钟未到",
                target,
            )

    # 5 活跃保护：刚刚还在用这个账号，先别把默认账号抽走。
    current = next(
        (item for item in pool if item.account_id == current_account_id), None
    )
    used = _parse_iso(current.last_used_at) if current is not None else None
    if used is not None and policy.active_guard_minutes > 0:
        idle = (moment - used).total_seconds() / 60
        if idle < policy.active_guard_minutes:
            return blocked(
                "active_guard",
                f"当前账号 {idle:.0f} 分钟前还在用"
                f"（保护期 {policy.active_guard_minutes} 分钟），先不动",
                target,
            )

    # 6 价值过滤：目标剩得太少，切过去也换不来多少额度。
    if target.remaining is not None and target.remaining < policy.min_remaining_credits:
        return blocked(
            "value",
            f"目标账号只剩 {target.remaining:g} 积分"
            f"（下限 {policy.min_remaining_credits:g}），不值得切",
            target,
        )

    # 7 防抖动：与当前账号比，只早一点点到期不值得来回切。
    #    当前账号没有「未来的到期时间」（查不到 / 已过期）时不设这个约束 ——
    #    此时任何更紧迫的目标都是净收益。
    current_expiry = (
        current.expires_at
        if current is not None and current.expires_at and current.expires_at > now_ts
        else None
    )
    if current_expiry is not None and policy.min_gap_hours > 0:
        gap = (current_expiry - (target.expires_at or now_ts)) / 3600
        if gap < policy.min_gap_hours:
            return blocked(
                "gap",
                f"目标与当前账号的到期时间只差 {gap:.1f} 小时"
                f"（阈值 {policy.min_gap_hours:g} 小时），不值得切",
                target,
            )

    return RotationDecision(
        action="switch",
        rule="ok",
        reason=(
            f"切到 {target.display_name}：剩余 {target.remaining:g} 积分，"
            f"{hours:.1f} 小时后到期"
        ),
        evaluated_at=stamp,
        current_account_id=current_account_id,
        target_account_id=target.account_id,
        target_display_name=target.display_name,
        hours_to_expiry=hours,
        remaining=target.remaining,
    )


# ---------- 策略解析 ----------


def _number(value: Any, fallback: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    return float(value)


def _non_negative(raw: Mapping[str, Any], key: str, fallback: float) -> float:
    value = _number(raw.get(key, fallback), fallback)
    if value < 0:
        raise RotationError(f"{key} 不能为负数")
    return value


def parse_policy(raw: Mapping[str, Any], *, strict: bool = False) -> RotationPolicy:
    """把 JSON 段或请求体解析成策略。

    ``strict=True``（用户输入）时非法值直接报错；读盘时用宽松模式，
    一条坏数据不应该让整个轮换停摆（退回默认值即可）。
    """
    enabled = raw.get("enabled", False)
    if strict and not isinstance(enabled, bool):
        raise RotationError("enabled 必须是布尔值")
    scope = raw.get("scope", DEFAULT_SCOPE)
    if not isinstance(scope, str) or not scope.strip():
        if strict:
            raise RotationError("scope 不能为空")
        scope = DEFAULT_SCOPE

    def integer(key: str, fallback: int) -> int:
        value = _non_negative(raw, key, float(fallback))
        return int(value)

    try:
        return RotationPolicy(
            enabled=bool(enabled),
            scope=scope.strip(),
            min_urgency_hours=_non_negative(raw, "minUrgencyHours", 72.0),
            cooldown_minutes=integer("cooldownMinutes", 120),
            active_guard_minutes=integer("activeGuardMinutes", 30),
            min_remaining_credits=_non_negative(raw, "minRemainingCredits", 100.0),
            min_gap_hours=_non_negative(raw, "minGapHours", 24.0),
        )
    except RotationError:
        if strict:
            raise
        return RotationPolicy()


# ---------- 策略与台账（schedules.json 的 rotation 段） ----------


class RotationStore:
    """`schedules.json` 的 `rotation` 段：策略 + 台账。

    与计划任务共用同一个文件，所以「读整份 → 改自己那段 → 写整份」全程持有
    `FILE_LOCK`，保证两边都不会把对方的段落覆盖掉。
    """

    def __init__(self, home: Path) -> None:
        self._file = home / SCHEDULES_FILENAME

    def _section(self) -> dict[str, Any]:
        raw = read_schedules(self._file).get(SECTION)
        return dict(raw) if isinstance(raw, dict) else {}

    def _commit(self, section: dict[str, Any]) -> None:
        with FILE_LOCK:
            root = read_schedules(self._file)
            root[SECTION] = section
            write_schedules(self._file, root)

    def load(self) -> RotationPolicy:
        raw = self._section().get("policy")
        return parse_policy(raw) if isinstance(raw, dict) else RotationPolicy()

    def save(self, policy: RotationPolicy) -> RotationPolicy:
        section = self._section()
        section["policy"] = policy.payload()
        self._commit(section)
        return policy

    def last_switch_at(self) -> str | None:
        value = self._section().get("lastSwitchAt")
        return value if isinstance(value, str) else None

    def history(self) -> list[RotationLedgerEntry]:
        raw = self._section().get("history")
        if not isinstance(raw, list):
            return []
        entries: list[RotationLedgerEntry] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            entries.append(
                RotationLedgerEntry(
                    at=str(item.get("at") or ""),
                    action=str(item.get("action") or ""),
                    rule=str(item.get("rule") or ""),
                    reason=str(item.get("reason") or ""),
                    target_account_id=(
                        item.get("targetAccountId")
                        if isinstance(item.get("targetAccountId"), str)
                        else None
                    ),
                    target_name=str(item.get("targetName") or ""),
                    executed=bool(item.get("executed")),
                    detail=str(item.get("detail") or ""),
                )
            )
        return entries

    def payload(self) -> dict[str, Any]:
        section = self._section()
        last_evaluated = section.get("lastEvaluatedAt")
        return {
            "policy": self.load().payload(),
            "lastEvaluatedAt": last_evaluated if isinstance(last_evaluated, str) else None,
            "lastSwitchAt": self.last_switch_at(),
            "history": [entry.payload() for entry in self.history()],
        }

    def record(self, entry: RotationLedgerEntry, *, switched: bool) -> None:
        section = self._section()
        history = [
            item for item in (section.get("history") or []) if isinstance(item, dict)
        ][-(HISTORY_KEEP - 1) :]
        history.append(entry.payload())
        section["history"] = history
        section["lastEvaluatedAt"] = entry.at
        if switched:
            section["lastSwitchAt"] = entry.at
        self._commit(section)


# ---------- 执行 ----------


@dataclass(frozen=True, slots=True)
class RotationSnapshot:
    """一轮判定所需的只读快照（由调用方从账号库 / 积分缓存 / 客户端状态拼出来）。"""

    candidates: tuple[Candidate, ...] = ()
    current_account_id: str | None = None
    client_running: bool = False


class RotationService:
    """把「判定」与「真的切」串起来，并落台账。

    - `snapshot(scope)`：取一轮快照（这里会查积分、读客户端状态，允许有网络）；
    - `switch(scope, account_id)`：执行一次切换，返回 ``(是否成功, 说明)``。

    两个回调都由外部注入，本服务自己不决定「怎么查、怎么切」。
    """

    def __init__(
        self,
        store: RotationStore,
        *,
        snapshot: Callable[[str], RotationSnapshot],
        switch: Callable[[str, str], tuple[bool, str]],
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._snapshot = snapshot
        self._switch = switch
        self._now = now or (lambda: datetime.now().astimezone())
        # 串行化整轮评估：两轮并发会各自算出同一个目标并各切一次。
        self._lock = threading.RLock()

    def state(self) -> dict[str, Any]:
        return self._store.payload()

    def update(self, patch: Mapping[str, Any]) -> RotationPolicy:
        """部分更新策略：未给的字段保持原值，非法的直接报错。"""
        current = self._store.load()
        merged = {**current.payload(), **dict(patch)}
        return self._store.save(parse_policy(merged, strict=True))

    def evaluate(self, *, dry_run: bool = False, manual: bool = False) -> dict[str, Any]:
        """跑一轮：判定 → 落台账 → （条件满足时）执行切换。

        - `manual=True`：设置页的「立即评估」按钮，未启用也能跑（用户显式要求）；
        - `dry_run=True`：只推演，绝不写客户端。
        """
        with self._lock:
            policy = self._store.load()
            if not (policy.enabled or manual):
                return {
                    "skipped": "自动轮换未启用",
                    "decision": None,
                    "executed": False,
                    "detail": "",
                    "clientRunning": False,
                    "state": self._store.payload(),
                }
            snapshot = self._snapshot(policy.scope)
            decision = decide(
                snapshot.candidates,
                policy,
                current_account_id=snapshot.current_account_id,
                last_switch_at=self._store.last_switch_at(),
                now=self._now(),
            )
            executed = False
            detail = decision.reason
            if decision.should_switch:
                if dry_run:
                    detail = "只推演，未执行切换"
                elif snapshot.client_running:
                    # 生效边界：不热切当前会话 —— 客户端运行中这一轮就不切。
                    detail = "客户端运行中，本轮不执行（不热切当前会话）"
                else:
                    ok, message = self._switch(policy.scope, decision.target_account_id or "")
                    executed = ok
                    detail = message
            self._store.record(
                RotationLedgerEntry(
                    at=decision.evaluated_at,
                    action=decision.action,
                    rule=decision.rule,
                    reason=decision.reason,
                    target_account_id=decision.target_account_id,
                    target_name=decision.target_display_name,
                    executed=executed,
                    detail=detail,
                ),
                switched=executed,
            )
            if decision.should_switch:
                logger.info(
                    "自动轮换判定：%s → %s（%s）",
                    decision.action,
                    decision.target_display_name or "无",
                    detail,
                )
            return {
                "skipped": None,
                "decision": decision.payload(),
                "executed": executed,
                "detail": detail,
                "clientRunning": snapshot.client_running,
                "state": self._store.payload(),
            }


class RotationRunner:
    """后台线程：每 `interval_seconds` 按策略评估一轮；未启用时读完策略就返回。"""

    def __init__(
        self,
        service: RotationService,
        *,
        interval_seconds: float = EVAL_INTERVAL_SECONDS,
    ) -> None:
        self._service = service
        self._interval = max(1.0, float(interval_seconds))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="chaierww-rotation", daemon=True
        )
        self._thread.start()
        logger.info("自动轮换评估线程已启动（间隔 %ss）", self._interval)

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
                self._service.evaluate()
            except Exception:  # noqa: BLE001 - 单轮失败绝不退出线程
                logger.exception("自动轮换评估异常")
            self._stop.wait(self._interval)
