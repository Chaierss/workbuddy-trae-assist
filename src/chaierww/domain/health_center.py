"""健康中心：把分散在兼容雷达 / 登录健康 / 号池供给 / 活动流水 / 计划任务里的
异常收成一条**可处置**的事件流。

设计边界：

- 信号全部来自**本地已落盘的数据**，刷新页面不触发任何上游请求；
- 事件按 ``dedupe_key`` 去重，同一个异常只更新「最后见到」时间，不刷屏；
- 信号消失时自动置为「已解决」，不需要用户手动关闭；
- 处置动作只认事件里登记的白名单，接口不会变成任意命令通道；
- 自动处置默认关闭，顺序固定为「先续期、再补跑」，每轮续期有数量上限。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from chaierww.domain.accounts.models import CredentialState
from chaierww.domain.activities import ACTIVITY_CATALOG, RESULT_FAILED
from chaierww.domain.credential_lifecycle import (
    AUTH_FAILURE_LIMIT,
    MODE_FORCE,
    refreshable,
)
from chaierww.domain.deadlines import deadlines as deadline_report
from chaierww.domain.login_health import (
    EXPIRING_WINDOW,
    LOGIN_BLOCKED,
    LOGIN_EXPIRING,
    LOGIN_INVALID,
)
from chaierww.domain.scopes import UnknownScope, get_scope
from chaierww.infra.db import transaction

logger = logging.getLogger("chaierww")

STATE_OPEN = "open"
STATE_ACKED = "acked"
STATE_SNOOZED = "snoozed"
STATE_RESOLVED = "resolved"

ACTIVE_STATES: tuple[str, ...] = (STATE_OPEN, STATE_ACKED, STATE_SNOOZED)

SEVERITY_ORDER = {"high": 0, "warn": 1, "info": 2}

#: 同一个事件连续点两次处置的最小间隔，避免重复点击连打上游。
ACTION_COOLDOWN_SECONDS = 5.0
#: 稍后提醒的可选时长上限（小时）。
MAX_SNOOZE_HOURS = 168
#: 已解决事件保留天数：够回看，又不会让表无限增长。
RESOLVED_KEEP_DAYS = 30
#: 一轮自动续期最多处理多少个号：失败时不能把整池都打一遍。
AUTO_REFRESH_LIMIT = 3
#: 一轮自动补跑最多处理多少个失败组合。
AUTO_RERUN_LIMIT = 20

#: 事件类别 → 采集失败时不能据此判定「已解决」的 kind 集合。
_FAMILY_KINDS: dict[str, tuple[str, ...]] = {
    "compat": ("compat_changed",),
    "login": ("login_invalid", "login_blocked", "login_expiring"),
    "credential": (
        "credential_expired",
        "credential_expiring",
        "credential_stalled",
        "credential_relogin",
    ),
    "supply": ("pool_supply",),
    "activity": ("activity_failed", "activity_deadline"),
    "schedule": ("schedule_failed",),
    "isolation": ("account_isolated",),
}

_ACTIVITY_TITLES = {entry["key"]: entry["title"] for entry in ACTIVITY_CATALOG}

#: 计划任务失败换成事件标题。``activity_run`` 与 ``credential_refresh`` 不列：
#: 它们的问题已经由活动 / 凭据事件逐号覆盖，再报一条只会重复。
_SCHEDULE_TITLES = {
    "keepalive": "保活探测失败",
    "checkin": "自动签到失败",
    "travel_depart": "派猫猫旅行失败",
    "travel_claim": "领取猫猫旅行奖励失败",
    "session_sync": "会话镜像采集失败",
    "upstream_watch": "上游结构探测失败",
    "pool_supply": "号池供给聚合失败",
    "health_auto": "健康中心自动处置失败",
}


class HealthCenterError(RuntimeError):
    """健康中心可展示给用户的错误。"""


@dataclass(frozen=True, slots=True)
class Signal:
    kind: str
    severity: str
    scope: str
    account_id: str | None
    title: str
    detail: str
    evidence: dict[str, Any]
    actions: tuple[dict[str, Any], ...]
    dedupe_key: str


def _action(action_id: str, kind: str, label: str, **extra: Any) -> dict[str, Any]:
    return {"id": action_id, "kind": kind, "label": label, **extra}


def _parse_iso(text: Any) -> datetime | None:
    if not isinstance(text, str) or not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _scope_label(key: str) -> str:
    try:
        return get_scope(key).label
    except UnknownScope:
        return key


def _json_loads(text: Any, fallback: Any) -> Any:
    if not isinstance(text, str) or not text:
        return fallback
    try:
        value = json.loads(text)
    except ValueError:
        return fallback
    return value if isinstance(value, type(fallback)) else fallback


class HealthCenterService:
    """健康事件的采集、状态机与处置执行。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        repository: Any,
        *,
        compat: Any | None = None,
        pool_health: Any | None = None,
        login_probes: Any | None = None,
        lifecycle: Any | None = None,
        activities: Any | None = None,
        scheduler: Any | None = None,
        isolation: Any | None = None,
        preferences: Any | None = None,
        scopes: tuple[str, ...] = ("workbuddy-cn", "trae-cn"),
        lock: threading.RLock | None = None,
        clock: Any = time.monotonic,
        now: Any = None,
    ) -> None:
        self._conn = conn
        self._repo = repository
        self._compat = compat
        self._pool_health = pool_health
        self._login_probes = login_probes
        self._lifecycle = lifecycle
        self._activities = activities
        self._scheduler = scheduler
        self._isolation = isolation
        self._preferences = preferences
        self._scopes = scopes
        self._lock = lock or threading.RLock()
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._last_action: dict[str, float] = {}

    # ---------- 采集 ----------

    def _accounts(self) -> list[Any]:
        if not self._scopes:
            return []
        return self._repo.list_all(self._scopes)

    def _collect(self) -> tuple[list[Signal], set[str]]:
        signals: list[Signal] = []
        degraded: set[str] = set()
        collectors = (
            ("compat", self._compat_signals),
            ("login", self._login_signals),
            ("credential", self._credential_signals),
            ("supply", self._supply_signals),
            ("activity", self._activity_signals),
            ("schedule", self._schedule_signals),
            ("deadline", self._deadline_signals),
            ("isolation", self._isolation_signals),
        )
        for family, collector in collectors:
            try:
                signals.extend(collector())
            except Exception:  # noqa: BLE001 - 单类采集失败不能把其它事件一起判「已解决」
                logger.exception("健康中心采集失败：%s", family)
                degraded.update(_FAMILY_KINDS[family])
        return signals, degraded

    def _compat_signals(self) -> list[Signal]:
        if self._compat is None:
            return []
        signals: list[Signal] = []
        for item in self._compat.unconfirmed():
            scope = str(item["scope"])
            version = item.get("clientVersion") or "未知版本"
            signals.append(
                Signal(
                    kind="compat_changed",
                    severity="warn",
                    scope=scope,
                    account_id=None,
                    title="上游结构待确认",
                    detail=(
                        f"{_scope_label(scope)} 客户端 {version} 的登录态结构与历史不同，"
                        "自动续期与切换仍会照常执行；确认可用后点「确认已适配」就不再提示"
                    ),
                    evidence={
                        "fingerprint": item.get("fingerprint") or "",
                        "clientVersion": version,
                        "firstSeenAt": item.get("firstSeenAt") or "",
                        "platform": item.get("platform") or "",
                    },
                    actions=(
                        _action("confirm_compat", "compat_confirm", "确认已适配"),
                        _action("probe_compat", "compat_probe", "重新探测"),
                        _action("open_compat", "navigate", "查看兼容详情", to="/overview"),
                    ),
                    dedupe_key=f"compat:{item.get('platform') or scope}:{scope}",
                )
            )
        return signals

    def _login_signals(self) -> list[Signal]:
        accounts = self._accounts()
        ids = [account.id for account in accounts]
        logins = self._repo.login_probes(ids) if ids else {}
        expiries = self._repo.credential_expiries(ids) if ids else {}
        now = self._now()
        signals: list[Signal] = []
        for account in accounts:
            record = logins.get(account.id) or {}
            state = str(record.get("state") or "unknown")
            detail = str(record.get("detail") or "")
            if state == LOGIN_INVALID:
                signals.append(
                    self._login_signal(
                        account,
                        kind="login_invalid",
                        severity="high",
                        title="登录态已失效",
                        detail=detail or "登录态已失效，重新登录后才能继续使用",
                    )
                )
            elif state == LOGIN_BLOCKED:
                signals.append(
                    self._login_signal(
                        account,
                        kind="login_blocked",
                        severity="high",
                        title="登录态绑定在另一台设备",
                        detail=detail or "令牌绑定在别的设备，需要在本机重新登录",
                    )
                )
            elif state == LOGIN_EXPIRING or self._soon(
                expiries.get(account.id), now, EXPIRING_WINDOW
            ):
                actions = [
                    _action("recheck_login", "login_probe", "立即复检"),
                    _action("open_accounts", "navigate", "前往号池", to="/accounts"),
                ]
                if self._can_refresh(account.id):
                    actions.insert(
                        0,
                        _action("refresh_credential", "credential_refresh", "立即续期"),
                    )
                signals.append(
                    Signal(
                        kind="login_expiring",
                        severity="warn",
                        scope=account.scope,
                        account_id=account.id,
                        title="登录态 24 小时内到期",
                        detail=f"{self._name(account)}：到期前巡检会自动续期，也可以现在手动续期",
                        evidence={"accountName": self._name(account), "detail": detail},
                        actions=tuple(actions),
                        dedupe_key=f"login:{account.id}",
                    )
                )
        return signals

    def _login_signal(
        self, account: Any, *, kind: str, severity: str, title: str, detail: str
    ) -> Signal:
        return Signal(
            kind=kind,
            severity=severity,
            scope=account.scope,
            account_id=account.id,
            title=title,
            detail=f"{self._name(account)}（{_scope_label(account.scope)}）：{detail}",
            evidence={"accountName": self._name(account), "detail": detail},
            actions=(
                _action("recheck_login", "login_probe", "重新复检"),
                _action("open_accounts", "navigate", "前往号池", to="/accounts"),
            ),
            dedupe_key=f"login:{account.id}",
        )

    def _credential_signals(self) -> list[Signal]:
        accounts = self._accounts()
        ids = [account.id for account in accounts]
        logins = self._repo.login_probes(ids) if ids else {}
        expiries = self._repo.credential_expiries(ids) if ids else {}
        probes = self._repo.probe_records(ids) if ids else {}
        now = self._now()
        expire_warn = now + timedelta(days=7)
        signals: list[Signal] = []
        for account in accounts:
            login_state = str((logins.get(account.id) or {}).get("state") or "unknown")
            if login_state in (LOGIN_INVALID, LOGIN_BLOCKED):
                continue  # 登录事件已经覆盖同一个号
            moment = _parse_iso(expiries.get(account.id))
            record = probes.get(account.id) or {}
            failures = int(record.get("failures") or 0)
            if account.credential_state is CredentialState.RELOGIN_REQUIRED:
                signal = Signal(
                    kind="credential_relogin",
                    severity="high",
                    scope=account.scope,
                    account_id=account.id,
                    title="需要重新登录",
                    detail=(
                        f"{self._name(account)}（{_scope_label(account.scope)}）："
                        "凭据状态已失效，重新导入或扫码登录后才能继续使用"
                    ),
                    evidence={"accountName": self._name(account)},
                    actions=self._credential_actions(False),
                    dedupe_key=f"credential:{account.id}",
                )
            elif moment is not None and moment <= now:
                can_refresh = self._can_refresh(account.id)
                signal = Signal(
                    kind="credential_expired",
                    severity="high",
                    scope=account.scope,
                    account_id=account.id,
                    title="凭据已过期",
                    detail=(
                        f"{self._name(account)}（{_scope_label(account.scope)}）："
                        f"已于 {moment.astimezone().strftime('%Y-%m-%d %H:%M')} 过期"
                        + ("，可立即续期" if can_refresh else "，需要重新登录")
                    ),
                    evidence={
                        "accountName": self._name(account),
                        "expiresAt": expiries.get(account.id),
                    },
                    actions=self._credential_actions(can_refresh),
                    dedupe_key=f"credential:{account.id}",
                )
            elif failures > 0:
                can_refresh = self._can_refresh(account.id)
                signal = Signal(
                    kind="credential_stalled",
                    severity="high" if failures >= AUTH_FAILURE_LIMIT else "warn",
                    scope=account.scope,
                    account_id=account.id,
                    title="凭据续期连续失败",
                    detail=(
                        f"{self._name(account)}（{_scope_label(account.scope)}）："
                        f"已连续 {failures} 次续期失败"
                        + (f"（{record.get('detail')}）" if record.get("detail") else "")
                    ),
                    evidence={
                        "accountName": self._name(account),
                        "failures": failures,
                        "nextRetryAt": record.get("nextRetryAt"),
                    },
                    actions=self._credential_actions(can_refresh),
                    dedupe_key=f"credential:{account.id}",
                )
            elif moment is not None and moment <= expire_warn:
                within = moment - now <= EXPIRING_WINDOW
                if within and login_state == LOGIN_EXPIRING:
                    continue  # 与登录事件同义，只报一条
                can_refresh = self._can_refresh(account.id)
                signal = Signal(
                    kind="credential_expiring",
                    severity="warn",
                    scope=account.scope,
                    account_id=account.id,
                    title="凭据即将过期",
                    detail=(
                        f"{self._name(account)}（{_scope_label(account.scope)}）："
                        f"{'24 小时内' if within else '7 天内'}到期"
                        + ("，到期前巡检会自动续期" if can_refresh else "，需要重新登录")
                    ),
                    evidence={
                        "accountName": self._name(account),
                        "expiresAt": expiries.get(account.id),
                    },
                    actions=self._credential_actions(can_refresh),
                    dedupe_key=f"credential:{account.id}",
                )
            else:
                continue
            signals.append(signal)
        return signals

    def _credential_actions(self, can_refresh: bool) -> tuple[dict[str, Any], ...]:
        actions: list[dict[str, Any]] = []
        if can_refresh:
            actions.append(_action("refresh_credential", "credential_refresh", "立即续期"))
        actions.append(_action("open_accounts", "navigate", "前往号池", to="/accounts"))
        return tuple(actions)

    def _supply_signals(self) -> list[Signal]:
        if self._pool_health is None:
            return []
        health = self._pool_health.health()
        signals: list[Signal] = []
        for alert in health.get("alerts") or []:
            code = str(alert.get("code") or "unknown")
            signals.append(
                Signal(
                    kind="pool_supply",
                    severity=str(alert.get("severity") or "warn"),
                    scope="",
                    account_id=None,
                    title="号池供给不足",
                    detail=str(alert.get("message") or ""),
                    evidence={
                        "code": code,
                        "healthyAccounts": health.get("healthyAccounts"),
                        "creditsDays": health.get("creditsDays"),
                        "thresholds": health.get("thresholds"),
                    },
                    actions=(
                        _action("open_accounts", "navigate", "前往号池补号", to="/accounts"),
                        _action("recheck_pool", "navigate", "查看号池健康", to="/accounts"),
                    ),
                    dedupe_key=f"pool_supply:{code}",
                )
            )
        return signals

    def _activity_signals(self) -> list[Signal]:
        today = self._now().date().isoformat()
        rows = self._repo.activity_runs(today, limit=500)
        names = {account.id: self._name(account) for account in self._accounts()}
        seen: set[tuple[str, str]] = set()
        signals: list[Signal] = []
        for row in rows:
            account_id = str(row.get("accountId") or "")
            key = str(row.get("key") or "")
            pair = (account_id, key)
            if pair in seen:
                continue  # 流水新的在前：第一次出现就是这一对的最新结论
            seen.add(pair)
            if str(row.get("status") or "") != RESULT_FAILED:
                continue
            title = _ACTIVITY_TITLES.get(key, key)
            name = names.get(account_id) or f"账号 {account_id[-8:]}"
            signals.append(
                Signal(
                    kind="activity_failed",
                    severity="warn",
                    scope=str(row.get("scope") or ""),
                    account_id=account_id or None,
                    title="活动执行失败",
                    detail=f"{name}：{title} 执行失败（{row.get('message') or '未知原因'}）",
                    evidence={
                        "accountName": name,
                        "key": key,
                        "title": title,
                        "day": today,
                        "message": row.get("message") or "",
                    },
                    actions=(
                        _action("rerun_activity", "activity_rerun", "重跑这一项"),
                        _action("open_activity", "navigate", "查看活动记录", to="/activity"),
                    ),
                    dedupe_key=f"activity:{account_id}:{key}:{today}",
                )
            )
        return signals

    def _schedule_signals(self) -> list[Signal]:
        if self._scheduler is None:
            return []
        signals: list[Signal] = []
        for task in self._scheduler.list():
            title = _SCHEDULE_TITLES.get(task.key)
            if title is None or not task.enabled or not task.history:
                continue
            last = task.history[-1]
            failed = int((last.get("meta") or {}).get("failed") or 0)
            if failed <= 0:
                continue
            streak = 0
            for entry in reversed(task.history):
                if int((entry.get("meta") or {}).get("failed") or 0) <= 0:
                    break
                streak += 1
            signals.append(
                Signal(
                    kind="schedule_failed",
                    severity="high" if streak >= 2 else "warn",
                    scope="",
                    account_id=None,
                    title=title,
                    detail=f"最近一次 {last.get('at', '-')}：{last.get('result', '')}",
                    evidence={
                        "taskKey": task.key,
                        "streak": streak,
                        "lastRunAt": last.get("at"),
                    },
                    actions=(
                        _action("run_schedule", "schedule_run", "立即重试", taskKey=task.key),
                        _action(
                            "open_schedule",
                            "navigate",
                            "查看计划任务",
                            to="/settings?tab=schedule",
                        ),
                    ),
                    dedupe_key=f"schedule:{task.key}",
                )
            )
        return signals

    # ---------- 状态机 ----------

    def _deadline_days(self) -> int:
        if self._preferences is None:
            return 3
        try:
            return max(1, int(getattr(self._preferences.load(), "activity_deadline_days", 3)))
        except Exception:  # noqa: BLE001 - 读偏好失败就用默认 3 天
            return 3

    def _deadline_signals(self) -> list[Signal]:
        """活动周期快结束但仍有账号没做完；快照过期只提示重新检测，不告警。"""
        report = deadline_report(self._conn, days=self._deadline_days(), now=self._now())
        signals: list[Signal] = []
        for item in report.get("items") or []:
            if item.get("stale"):
                continue
            pending = item.get("pendingAccounts") or []
            names = "、".join(str(row.get("displayName") or "") for row in pending[:3])
            if len(pending) > 3:
                names += f" 等 {len(pending)} 个账号"
            days = int(item.get("daysLeft") or 0)
            signals.append(
                Signal(
                    kind="activity_deadline",
                    severity="warn",
                    scope="",
                    account_id=None,
                    title="活动即将截止",
                    detail=(
                        f"{item.get('title') or item.get('key')} 还有 {days} 天截止，"
                        f"{item.get('pending') or len(pending)} 个账号未完成"
                        + (f"（{names}）" if names else "")
                    ),
                    evidence={
                        "key": item.get("key"),
                        "periodEnd": item.get("periodEnd"),
                        "daysLeft": days,
                        "pending": item.get("pending"),
                    },
                    actions=(
                        _action(
                            "open_deadline",
                            "navigate",
                            "去待办处理",
                            to=f"/activity?tab=todo&key={item.get('key')}",
                        ),
                    ),
                    dedupe_key=f"deadline:{item.get('key')}:{item.get('periodEnd')}",
                )
            )
        return signals

    def _isolation_signals(self) -> list[Signal]:
        if self._isolation is None:
            return []
        names = {account.id: self._name(account) for account in self._accounts()}
        signals: list[Signal] = []
        for item in self._isolation.items():
            if not item.get("active"):
                continue
            account_id = str(item.get("accountId") or "")
            name = names.get(account_id) or f"账号 {account_id[-8:]}"
            until = str(item.get("isolatedUntil") or "")
            signals.append(
                Signal(
                    kind="account_isolated",
                    severity="warn",
                    scope="",
                    account_id=account_id or None,
                    title="账号已被网关隔离",
                    detail=(
                        f"{name}：网关连续 {item.get('failureCount')} 次认证失败，"
                        "已暂时不参与转发（只影响网关，不影响切号与数据）"
                        + (f"，预计 {until} 自动恢复" if until else "")
                    ),
                    evidence={
                        "accountName": name,
                        "failureCount": item.get("failureCount"),
                        "isolatedUntil": until,
                    },
                    actions=(
                        _action("release_isolation", "isolation_release", "解除隔离"),
                        _action("open_accounts", "navigate", "前往号池", to="/accounts"),
                    ),
                    dedupe_key=f"isolation:{account_id}:{until}",
                )
            )
        return signals

    # ---------- 状态机 ----------

    def snapshot(self, *, state: str = "active", limit: int = 200) -> dict[str, Any]:
        signals, degraded = self._collect()
        self._persist(signals, degraded)
        items = self.items(state=state, limit=limit)
        summary = self.summary()
        return {
            "items": items,
            "summary": summary,
            "checkedAt": self._stamp(),
        }

    def _stamp(self) -> str:
        """统一用注入的时钟生成时间文本；单测里才能稳定推进「稍后提醒」。"""
        return self._now().replace(microsecond=0).isoformat()

    def _persist(self, signals: list[Signal], degraded: set[str]) -> None:
        stamp = self._stamp()
        with self._lock, transaction(self._conn):
            for signal in signals:
                row = self._conn.execute(
                    "SELECT id, state FROM health_events WHERE dedupe_key = ?",
                    (signal.dedupe_key,),
                ).fetchone()
                payload = (
                    signal.kind,
                    signal.severity,
                    signal.scope,
                    signal.account_id,
                    signal.title,
                    signal.detail,
                    json.dumps(signal.evidence, ensure_ascii=False),
                    json.dumps(list(signal.actions), ensure_ascii=False),
                )
                if row is None:
                    self._conn.execute(
                        """
                        INSERT INTO health_events (
                            id, dedupe_key, kind, severity, scope, account_id,
                            title, detail, evidence, actions, state,
                            first_seen_at, last_seen_at, state_changed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            uuid.uuid4().hex,
                            signal.dedupe_key,
                            *payload,
                            STATE_OPEN,
                            stamp,
                            stamp,
                            stamp,
                        ),
                    )
                    continue
                reopened = row["state"] == STATE_RESOLVED
                snooze_expired = (
                    row["state"] == STATE_SNOOZED
                    and (self._snooze_until(row["id"]) or stamp) <= stamp
                )
                next_state = row["state"]
                if reopened or snooze_expired:
                    next_state = STATE_OPEN
                self._conn.execute(
                    """
                    UPDATE health_events
                       SET kind = ?, severity = ?, scope = ?, account_id = ?,
                           title = ?, detail = ?, evidence = ?, actions = ?,
                           last_seen_at = ?,
                           state = ?,
                           state_changed_at = CASE WHEN ? != state THEN ? ELSE state_changed_at END,
                           first_seen_at = CASE WHEN ? THEN ? ELSE first_seen_at END,
                           resolved_at = CASE WHEN ? THEN NULL ELSE resolved_at END,
                           snoozed_until = CASE WHEN ? THEN NULL ELSE snoozed_until END
                     WHERE id = ?
                    """,
                    (
                        *payload,
                        stamp,
                        next_state,
                        next_state,
                        stamp,
                        int(reopened),
                        stamp,
                        int(reopened),
                        int(reopened or snooze_expired),
                        row["id"],
                    ),
                )

            active = self._conn.execute(
                "SELECT id, dedupe_key, kind FROM health_events WHERE state != ?",
                (STATE_RESOLVED,),
            ).fetchall()
            seen = {signal.dedupe_key for signal in signals}
            for row in active:
                if row["dedupe_key"] in seen or row["kind"] in degraded:
                    continue
                self._conn.execute(
                    """
                    UPDATE health_events
                       SET state = ?, resolved_at = ?, state_changed_at = ?
                     WHERE id = ?
                    """,
                    (STATE_RESOLVED, stamp, stamp, row["id"]),
                )

            cutoff = (self._now() - timedelta(days=RESOLVED_KEEP_DAYS)).isoformat()
            self._conn.execute(
                "DELETE FROM health_events WHERE state = ? AND COALESCE(resolved_at, '') < ?",
                (STATE_RESOLVED, cutoff),
            )

    def _snooze_until(self, event_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT snoozed_until FROM health_events WHERE id = ?", (event_id,)
        ).fetchone()
        return str(row["snoozed_until"]) if row and row["snoozed_until"] else None

    # ---------- 查询 ----------

    def items(self, *, state: str = "active", limit: int = 200) -> list[dict[str, Any]]:
        where = ""
        params: list[Any] = []
        if state == "active":
            where = "WHERE state IN (?, ?, ?)"
            params.extend(ACTIVE_STATES)
        elif state == "resolved":
            where = "WHERE state = ?"
            params.append(STATE_RESOLVED)
        rows = self._conn.execute(
            f"""
            SELECT * FROM health_events {where}
             ORDER BY last_seen_at DESC
             LIMIT ?
            """,
            (*params, max(1, min(int(limit), 1000))),
        ).fetchall()
        items = [self._row_payload(row) for row in rows]
        # 先按时间倒序，再按严重度稳定排序：同级内新的在前，高级别永远压在上面。
        items.sort(key=lambda item: str(item["lastSeenAt"]), reverse=True)
        items.sort(key=lambda item: SEVERITY_ORDER.get(str(item["severity"]), 3))
        return items

    def summary(self) -> dict[str, Any]:
        rows = self._conn.execute(
            "SELECT state, severity, COUNT(*) AS count FROM health_events GROUP BY state, severity"
        ).fetchall()
        payload = {
            "open": 0,
            "acked": 0,
            "snoozed": 0,
            "resolved": 0,
            "high": 0,
            "warn": 0,
            "info": 0,
            "unhandled": 0,
        }
        for row in rows:
            state = str(row["state"])
            severity = str(row["severity"])
            count = int(row["count"])
            if state in payload:
                payload[state] += count
            if state in ACTIVE_STATES:
                if severity in payload:
                    payload[severity] += count
                if state == STATE_OPEN:
                    payload["unhandled"] += count
        return payload

    def _event_row(self, event_id: str) -> sqlite3.Row:
        row = self._conn.execute(
            "SELECT * FROM health_events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise HealthCenterError("事件不存在或已被清理")
        return row

    def _row_payload(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "kind": row["kind"],
            "severity": row["severity"],
            "state": row["state"],
            "scope": row["scope"],
            "accountId": row["account_id"],
            "title": row["title"],
            "detail": row["detail"],
            "evidence": _json_loads(row["evidence"], {}),
            "actions": _json_loads(row["actions"], []),
            "firstSeenAt": row["first_seen_at"],
            "lastSeenAt": row["last_seen_at"],
            "stateChangedAt": row["state_changed_at"],
            "snoozedUntil": row["snoozed_until"],
            "resolvedAt": row["resolved_at"],
        }

    # ---------- 处置 ----------

    def ack(self, event_id: str) -> dict[str, Any]:
        row = self._event_row(event_id)
        if row["state"] == STATE_RESOLVED:
            raise HealthCenterError("事件已经解决，无需确认")
        self._set_state(event_id, STATE_ACKED)
        return self._row_payload(self._event_row(event_id))

    def snooze(self, event_id: str, hours: int = 24) -> dict[str, Any]:
        row = self._event_row(event_id)
        if row["state"] == STATE_RESOLVED:
            raise HealthCenterError("事件已经解决，无需稍后提醒")
        span = max(1, min(int(hours), MAX_SNOOZE_HOURS))
        until = (self._now() + timedelta(hours=span)).replace(microsecond=0).isoformat()
        with self._lock, transaction(self._conn):
            self._conn.execute(
                """
                UPDATE health_events
                   SET state = ?, snoozed_until = ?, state_changed_at = ?
                 WHERE id = ?
                """,
                (STATE_SNOOZED, until, self._stamp(), event_id),
            )
        return self._row_payload(self._event_row(event_id))

    def bulk(self, event_ids: list[str], *, op: str, hours: int = 24) -> dict[str, Any]:
        done = 0
        skipped = 0
        for event_id in dict.fromkeys(event_ids):
            try:
                if op == "ack":
                    self.ack(event_id)
                elif op == "snooze":
                    self.snooze(event_id, hours)
                else:
                    raise HealthCenterError(f"未知操作：{op}")
                done += 1
            except HealthCenterError:
                skipped += 1
        return {"ok": True, "done": done, "skipped": skipped}

    def _set_state(self, event_id: str, state: str) -> None:
        with self._lock, transaction(self._conn):
            self._conn.execute(
                "UPDATE health_events SET state = ?, state_changed_at = ? WHERE id = ?",
                (state, self._stamp(), event_id),
            )

    def act(self, event_id: str, action_id: str) -> dict[str, Any]:
        row = self._event_row(event_id)
        if row["state"] == STATE_RESOLVED:
            raise HealthCenterError("事件已经解决，不能再执行处置")
        actions = _json_loads(row["actions"], [])
        chosen = next(
            (item for item in actions if isinstance(item, dict) and item.get("id") == action_id),
            None,
        )
        if chosen is None:
            raise HealthCenterError("这个事件不允许该操作")
        key = f"{event_id}:{action_id}"
        last = self._last_action.get(key)
        if last is not None and self._clock() - last < ACTION_COOLDOWN_SECONDS:
            raise HealthCenterError("刚刚执行过，请稍候再试")
        kind = str(chosen.get("kind") or "")
        try:
            message = self._dispatch(kind, row, chosen)
        except HealthCenterError:
            raise
        except Exception as exc:  # noqa: BLE001 - 失败要留审计，同时给用户可读消息
            logger.exception("健康事件处置失败：%s / %s", event_id, action_id)
            self._audit(event_id, kind, ok=False, message=f"{type(exc).__name__}：{exc}")
            raise HealthCenterError(f"处置失败：{exc}") from exc
        self._last_action[key] = self._clock()
        self._audit(event_id, kind, ok=True, message=message)
        return {
            "ok": True,
            "message": message,
            "event": self._row_payload(self._event_row(event_id)),
        }

    def _dispatch(self, kind: str, row: sqlite3.Row, action: dict[str, Any]) -> str:
        evidence = _json_loads(row["evidence"], {})
        if kind == "navigate":
            # 跳页由前端处理，服务端不接受把它当动作执行。
            raise HealthCenterError("跳转类动作不需要服务端执行")
        if kind == "login_probe":
            if self._login_probes is None or not row["account_id"]:
                raise HealthCenterError("登录复检服务不可用")
            result = self._login_probes.probe([row["account_id"]])
            item = (result.get("items") or [{}])[0]
            state = str(item.get("state") or "unknown")
            return f"复检完成：{state}"
        if kind == "credential_refresh":
            if self._lifecycle is None or not row["account_id"]:
                raise HealthCenterError("续期服务不可用")
            account = self._repo.get(row["account_id"])
            results = self._lifecycle.sweep([account], mode=MODE_FORCE)
            return self._lifecycle.describe(results, MODE_FORCE)
        if kind == "activity_rerun":
            if self._activities is None:
                raise HealthCenterError("活动服务不可用")
            pair = (
                str(evidence.get("accountId") or row["account_id"] or ""),
                str(evidence.get("key") or ""),
            )
            report = self._activities.rerun_failed(self._accounts(), [pair])
            summary = report.get("summary") or {}
            return f"重跑完成：成功 {summary.get('ok', 0)}，失败 {summary.get('failed', 0)}"
        if kind == "schedule_run":
            if self._scheduler is None:
                raise HealthCenterError("调度器不可用")
            task_key = str(action.get("taskKey") or evidence.get("taskKey") or "")
            outcome = self._scheduler.run_now(task_key)
            return str(outcome.get("result") or "已执行")
        if kind == "compat_confirm":
            if self._compat is None:
                raise HealthCenterError("兼容雷达不可用")
            self._compat.confirm(str(row["scope"]))
            return "已确认当前结构兼容"
        if kind == "compat_probe":
            if self._compat is None:
                raise HealthCenterError("兼容雷达不可用")
            payload = self._compat.probe(manual=True)
            changed = payload.get("changed") or []
            if not changed:
                return "探测完成，结构未变化"
            return f"探测完成，{len(changed)} 个作用域结构仍有变化"
        if kind == "isolation_release":
            if self._isolation is None or not row["account_id"]:
                raise HealthCenterError("隔离服务不可用")
            try:
                self._isolation.release(str(row["account_id"]))
            except LookupError as exc:
                raise HealthCenterError(str(exc)) from exc
            return "已解除隔离，账号重新参与网关选号"
        raise HealthCenterError(f"未知的处置动作：{kind}")

    def _audit(self, event_id: str, action: str, *, ok: bool, message: str) -> None:
        with self._lock, transaction(self._conn):
            self._conn.execute(
                """
                INSERT INTO health_actions (event_id, action, ok, message, at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (event_id, action, int(ok), message, self._stamp()),
            )

    def actions(self, event_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """
            SELECT action, ok, message, at FROM health_actions
             WHERE event_id = ? ORDER BY seq DESC LIMIT ?
            """,
            (event_id, max(1, min(int(limit), 200))),
        ).fetchall()
        return [
            {
                "action": row["action"],
                "ok": bool(row["ok"]),
                "message": row["message"],
                "at": row["at"],
            }
            for row in rows
        ]

    # ---------- 自动处置 ----------

    def auto_dispose(self) -> dict[str, Any]:
        if self._preferences is None:
            return {"enabled": False, "message": "自动处置未开启"}
        prefs = self._preferences.load()
        if not (prefs.health_auto_refresh or prefs.health_auto_rerun):
            return {"enabled": False, "message": "自动处置未开启"}

        self.snapshot(state="active")
        rows = self._conn.execute(
            "SELECT * FROM health_events WHERE state IN (?, ?, ?) ORDER BY last_seen_at DESC",
            ACTIVE_STATES,
        ).fetchall()
        refreshed = 0
        rerun_pairs: list[tuple[str, str]] = []
        rerun_events: list[str] = []
        for row in rows:
            actions = _json_loads(row["actions"], [])
            kinds = {str(item.get("kind")) for item in actions if isinstance(item, dict)}
            if prefs.health_auto_refresh and "credential_refresh" in kinds:
                if (
                    refreshed >= AUTO_REFRESH_LIMIT
                    or not row["account_id"]
                    or self._lifecycle is None
                    or not self._auto_refresh_due(row)
                    or self._recent_auto_action(row["id"], "auto:credential_refresh")
                ):
                    continue
                try:
                    account = self._repo.get(row["account_id"])
                    results = self._lifecycle.sweep([account], mode=MODE_FORCE)
                    message = self._lifecycle.describe(results, MODE_FORCE)
                    self._audit(row["id"], "auto:credential_refresh", ok=True, message=message)
                    refreshed += 1
                except Exception as exc:  # noqa: BLE001 - 单号失败不影响其它处置
                    self._audit(
                        row["id"],
                        "auto:credential_refresh",
                        ok=False,
                        message=f"{type(exc).__name__}：{exc}",
                    )
            if (
                prefs.health_auto_rerun
                and row["kind"] == "activity_failed"
                and not self._recent_auto_action(row["id"], "auto:activity_rerun", hours=24)
            ):
                evidence = _json_loads(row["evidence"], {})
                pair = (
                    str(evidence.get("accountId") or row["account_id"] or ""),
                    str(evidence.get("key") or ""),
                )
                if all(pair) and len(rerun_pairs) < AUTO_RERUN_LIMIT:
                    rerun_pairs.append(pair)
                    rerun_events.append(row["id"])

        rerun_result: dict[str, Any] | None = None
        if rerun_pairs and self._activities is not None:
            try:
                report = self._activities.rerun_failed(self._accounts(), rerun_pairs)
                rerun_result = report.get("summary") or {}
                message = (
                    f"自动补跑 {len(rerun_pairs)} 项：成功 {rerun_result.get('ok', 0)}，"
                    f"失败 {rerun_result.get('failed', 0)}"
                )
                for event_id in rerun_events:
                    self._audit(event_id, "auto:activity_rerun", ok=True, message=message)
            except Exception as exc:  # noqa: BLE001
                for event_id in rerun_events:
                    self._audit(
                        event_id,
                        "auto:activity_rerun",
                        ok=False,
                        message=f"{type(exc).__name__}：{exc}",
                    )

        return {
            "enabled": True,
            "refreshed": refreshed,
            "rerunPairs": len(rerun_pairs),
            "rerunSummary": rerun_result,
        }

    # ---------- 小工具 ----------

    def _recent_auto_action(self, event_id: str, action: str, *, hours: int = 12) -> bool:
        """同一事件在窗口内已经自动处置过就不再重复，避免一天续期两次 / 补跑两次。"""
        cutoff = (self._now() - timedelta(hours=hours)).replace(microsecond=0).isoformat()
        row = self._conn.execute(
            """
            SELECT 1 FROM health_actions
             WHERE event_id = ? AND action = ? AND ok = 1 AND at >= ?
             LIMIT 1
            """,
            (event_id, action, cutoff),
        ).fetchone()
        return row is not None

    def _auto_refresh_due(self, row: sqlite3.Row) -> bool:
        """自动续期只处理「已过期」或「24 小时内到期」，不碰续期连败与 7 天窗口。

        连续失败的号再自动打一次，大概率还是失败，还会放大风控；7 天窗口留给
        日常 ``due`` 巡检，不必在健康中心里提前动手。
        """
        if row["kind"] in ("credential_expired", "login_expiring"):
            return True
        if row["kind"] != "credential_expiring":
            return False
        expires_at = _json_loads(row["evidence"], {}).get("expiresAt")
        moment = _parse_iso(expires_at)
        return moment is not None and moment <= self._now() + EXPIRING_WINDOW

    def _name(self, account: Any) -> str:
        return str(getattr(account, "display_name", "") or f"账号 {account.id[-8:]}")

    def _soon(self, text: Any, now: datetime, window: timedelta) -> bool:
        """到期时间是否落在「现在之后、窗口之内」；已过期交给凭据事件。"""
        moment = _parse_iso(text)
        return moment is not None and now < moment <= now + window

    def _can_refresh(self, account_id: str) -> bool:
        try:
            kind, credential = self._repo.credential_record(account_id)
            return refreshable(kind, credential)
        except Exception:  # noqa: BLE001 - 读不到凭据就不提供续期按钮
            return False
