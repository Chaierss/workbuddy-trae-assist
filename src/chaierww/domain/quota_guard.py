"""号池额度闭环：低额度预警、耗尽出池、默认号自动换成还有额度的那个。

## 它补的是哪一段

网关（`gateway_relay`）早就会跳过「积分耗尽 / 被排除 / 冷却中」的号，但**谁把
一个号判成耗尽并排除出去**，之前没有人负责：余额查到了只用于轮换评估，
转发出错才被动升级成持久排除。结果是某个号余额见底后，网关仍会继续选中它，
直到连续失败才被摘掉 —— 用户看到的是「明明还有别的号，却一直在 401/额度不足」。

本模块把这一段闭起来，并把每个动作写成可解释的台账：

- **预警**：余额低于 `LOW_THRESHOLD`（默认 100）→ 记一条低额度预警；
- **出池**：余额 ≤ 0 → 调网关的排除接口把它移出候选，`EMPTY_MINUTES` 后自动复活
  （到点会重新查余额，真的还是 0 就再排一次）；
- **换默认号**：当前默认号耗尽或查不到余额，且同作用域里有余额更高的可用号、
  客户端未运行、过了冷却期 → 切到那个号；切完写台账。

## 边界

1. **只查余额、不写平台**：余额由 `CreditsService.query_many` 查（缓存 TTL 内复用），
   查不到余额（≠ 0）的号**绝不**被判定为耗尽，也不会被拿去当切换目标。
2. **不热切**：客户端运行中一律不切，等下一轮。
3. **一次评估只切一次**：评估线程 10 分钟一轮，冷却 60 分钟，避免来回抖。
4. 台账落在 `schedules.json` 的 `quota` 段，与计划任务/轮换共用一把文件锁。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
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
SECTION = "quota"

#: 低额度预警线（积分）。低于它只警告，不摘号。
LOW_THRESHOLD = 100.0
#: 耗尽账号的自动排除时长：到点自动复活，下一轮重新查余额，避免一次抖动永久摘号。
EMPTY_MINUTES = 360
#: 后台评估间隔（秒）。余额缓存 TTL 是 60 秒，10 分钟一轮足够及时且不吵。
EVAL_INTERVAL_SECONDS = 600.0
#: 切换冷却：两次自动换号至少隔 60 分钟。
COOLDOWN_MINUTES = 60
HISTORY_KEEP = 20

#: 自动换号的作用域。与积分到期轮换保持一致：默认只动 WorkBuddy 号池。
DEFAULT_SCOPE = "workbuddy-cn"


@dataclass(frozen=True, slots=True)
class QuotaAccount:
    """参与额度判定的一行。``credits=None`` 表示查不到（≠ 0）。"""

    account_id: str
    display_name: str
    scope: str
    credits: float | None = None
    available: bool = False
    switchable: bool = True
    excluded: bool = False
    current: bool = False


@dataclass(frozen=True, slots=True)
class QuotaPlan:
    """一轮判定的结论：谁该出池、谁只是预警、要不要换默认号。"""

    exhausted: tuple[QuotaAccount, ...] = ()
    warnings: tuple[QuotaAccount, ...] = ()
    current: QuotaAccount | None = None
    target: QuotaAccount | None = None
    reason: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "exhausted": [item.account_id for item in self.exhausted],
            "warnings": [
                {"accountId": item.account_id, "credits": item.credits}
                for item in self.warnings
            ],
            "currentAccountId": self.current.account_id if self.current else None,
            "targetAccountId": self.target.account_id if self.target else None,
            "reason": self.reason,
        }


def plan_quota(
    accounts: Iterable[QuotaAccount],
    *,
    low_threshold: float = LOW_THRESHOLD,
) -> QuotaPlan:
    """纯函数：按余额给出「出池 / 预警 / 建议切到谁」。

    - 余额未知（``available=False`` 或 ``credits is None``）既不判耗尽，也不当目标；
    - 耗尽 = 余额 ≤ 0；
    - 低额度 = 0 < 余额 < 阈值；
    - 换号只在**当前号耗尽或余额未知**时发生，当前号只是「低额度」不换
      （额度低不等于不能用，来回切反而打断会话）。
    """
    pool = list(accounts)
    known = [item for item in pool if item.available and item.credits is not None]
    exhausted = tuple(item for item in known if (item.credits or 0) <= 0)
    warnings = tuple(
        item for item in known if 0 < (item.credits or 0) < low_threshold
    )
    current = next((item for item in pool if item.current), None)
    if current is None:
        return QuotaPlan(
            exhausted=exhausted,
            warnings=warnings,
            reason="没有当前默认号，不做自动切换",
        )

    need_switch = (
        (current.available and current.credits is not None and current.credits <= 0)
        or not current.available
        or current.credits is None
        or current.excluded
    )
    if not need_switch:
        return QuotaPlan(
            exhausted=exhausted,
            warnings=warnings,
            current=current,
            reason=f"当前号还有 {current.credits:g} 积分，无需换号"
            if current.credits is not None
            else "当前号状态正常，无需换号",
        )

    candidates = [
        item
        for item in known
        if item.switchable
        and not item.excluded
        and (item.credits or 0) >= low_threshold
        and item.account_id != current.account_id
    ]
    target = max(candidates, key=lambda item: item.credits or 0) if candidates else None
    reason = (
        f"当前号余额 {current.credits:g}，切到余额最高的 {target.display_name}"
        f"（{target.credits:g}）"
        if target is not None and current.credits is not None
        else (
            f"当前号余额查不到，切到余额最高的 {target.display_name}（{target.credits:g}）"
            if target is not None
            else "当前号不可用，但没有余额足够的替代号"
        )
    )
    return QuotaPlan(
        exhausted=exhausted,
        warnings=warnings,
        current=current,
        target=target,
        reason=reason,
    )


class QuotaStore:
    """`schedules.json` 的 `quota` 段：最近一次评估结论 + 动作台账。"""

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

    def payload(self) -> dict[str, Any]:
        section = self._section()
        return {
            "lastEvaluatedAt": section.get("lastEvaluatedAt"),
            "lastSwitchAt": section.get("lastSwitchAt"),
            "lowThreshold": section.get("lowThreshold", LOW_THRESHOLD),
            "last": section.get("last") if isinstance(section.get("last"), dict) else None,
            "history": section.get("history") if isinstance(section.get("history"), list) else [],
        }

    def record(
        self,
        *,
        at: str,
        plan: QuotaPlan,
        low_threshold: float,
        actions: list[str],
        switched: bool,
    ) -> None:
        section = self._section()
        history = [item for item in (section.get("history") or []) if isinstance(item, dict)]
        history = history[-(HISTORY_KEEP - 1) :]
        history.append(
            {
                "at": at,
                "actions": actions,
                "switched": switched,
                "reason": plan.reason,
                "exhausted": [item.account_id for item in plan.exhausted],
                "warnings": [
                    {"accountId": item.account_id, "credits": item.credits}
                    for item in plan.warnings
                ],
                "targetAccountId": plan.target.account_id if plan.target else None,
                "targetName": plan.target.display_name if plan.target else "",
            }
        )
        section["history"] = history
        section["lastEvaluatedAt"] = at
        section["lowThreshold"] = low_threshold
        section["last"] = {
            "at": at,
            "exhausted": [
                {
                    "accountId": item.account_id,
                    "displayName": item.display_name,
                    "credits": item.credits,
                }
                for item in plan.exhausted
            ],
            "warnings": [
                {
                    "accountId": item.account_id,
                    "displayName": item.display_name,
                    "credits": item.credits,
                }
                for item in plan.warnings
            ],
            "currentAccountId": plan.current.account_id if plan.current else None,
            "targetAccountId": plan.target.account_id if plan.target else None,
            "reason": plan.reason,
            "switched": switched,
        }
        if switched:
            section["lastSwitchAt"] = at
        self._commit(section)

    def forget_account(self, account_id: str) -> None:
        """永久删除账号后清掉台账里对它的引用（保留历史行）。"""
        with FILE_LOCK:
            root = read_schedules(self._file)
            section = root.get(SECTION)
            if not isinstance(section, dict):
                return
            changed = False
            history = section.get("history")
            if isinstance(history, list):
                for item in history:
                    if not isinstance(item, dict):
                        continue
                    if item.get("targetAccountId") == account_id:
                        item["targetAccountId"] = None
                        changed = True
                    for field in ("exhausted", "warnings"):
                        values = item.get(field)
                        if not isinstance(values, list):
                            continue
                        kept = [
                            value
                            for value in values
                            if not (
                                value == account_id
                                or (
                                    isinstance(value, dict)
                                    and value.get("accountId") == account_id
                                )
                            )
                        ]
                        if len(kept) != len(values):
                            item[field] = kept
                            changed = True
            last = section.get("last")
            if isinstance(last, dict):
                for field in ("exhausted", "warnings"):
                    values = last.get(field)
                    if not isinstance(values, list):
                        continue
                    kept = [
                        value
                        for value in values
                        if not (isinstance(value, dict) and value.get("accountId") == account_id)
                    ]
                    if len(kept) != len(values):
                        last[field] = kept
                        changed = True
                if last.get("targetAccountId") == account_id:
                    last["targetAccountId"] = None
                    changed = True
            if changed:
                root[SECTION] = section
                write_schedules(self._file, root)


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    """一轮判定所需的只读快照（由调用方从账号库 / 积分缓存 / 客户端状态拼出来）。"""

    accounts: tuple[QuotaAccount, ...] = ()
    client_running: bool = True


class QuotaService:
    """判定 → 出池 → （条件满足时）换默认号 → 落台账。"""

    def __init__(
        self,
        store: QuotaStore,
        *,
        snapshot: Callable[[], QuotaSnapshot],
        exclude: Callable[[str], None],
        switch: Callable[[str, str], tuple[bool, str]],
        scope: str = DEFAULT_SCOPE,
        low_threshold: float = LOW_THRESHOLD,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = store
        self._snapshot = snapshot
        self._exclude = exclude
        self._switch = switch
        self._scope = scope
        self._low = float(low_threshold)
        self._now = now or (lambda: datetime.now().astimezone())
        self._lock = threading.RLock()

    def state(self) -> dict[str, Any]:
        return self._store.payload()

    def evaluate(self, *, dry_run: bool = False) -> dict[str, Any]:
        with self._lock:
            snapshot = self._snapshot()
            plan = plan_quota(snapshot.accounts, low_threshold=self._low)
            moment = self._now()
            at = moment.isoformat(timespec="seconds")
            actions: list[str] = []

            if not dry_run:
                for item in plan.exhausted:
                    if item.excluded:
                        continue
                    try:
                        self._exclude(item.account_id)
                    except Exception:  # noqa: BLE001 - 单个号排除失败不影响其余
                        logger.exception("额度耗尽移出网关失败：%s", item.account_id)
                        continue
                    actions.append(f"移出网关：{item.display_name}（余额 {item.credits:g}）")

            switched = False
            detail = plan.reason
            if plan.target is not None:
                if dry_run:
                    detail = "只推演，未执行切换"
                elif snapshot.client_running:
                    detail = "客户端运行中，本轮不换号（不热切当前会话）"
                elif self._in_cooldown(moment):
                    detail = f"距上次自动换号不足 {COOLDOWN_MINUTES} 分钟，本轮不换"
                else:
                    ok, message = self._switch(self._scope, plan.target.account_id)
                    switched = ok
                    detail = message
                    actions.append(
                        f"换默认号：{plan.target.display_name} → {'成功' if ok else '失败'}"
                    )

            if not dry_run:
                self._store.record(
                    at=at,
                    plan=plan,
                    low_threshold=self._low,
                    actions=actions,
                    switched=switched,
                )
            if actions:
                logger.info("号池额度闭环：%s（%s）", "；".join(actions), detail)
            return {
                "plan": plan.payload(),
                "actions": actions,
                "executed": switched,
                "detail": detail,
                "clientRunning": snapshot.client_running,
                "state": self._store.payload(),
            }

    def _in_cooldown(self, moment: datetime) -> bool:
        last = self._store.payload().get("lastSwitchAt")
        if not isinstance(last, str) or not last:
            return False
        try:
            parsed = datetime.fromisoformat(last)
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return moment - parsed < timedelta(minutes=COOLDOWN_MINUTES)


class QuotaRunner:
    """后台线程：每 `interval_seconds` 评估一轮。"""

    def __init__(
        self,
        service: QuotaService,
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
            target=self._loop, name="chaierww-quota", daemon=True
        )
        self._thread.start()
        logger.info("号池额度评估线程已启动（间隔 %ss）", self._interval)

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
                logger.exception("号池额度评估异常")
            self._stop.wait(self._interval)
