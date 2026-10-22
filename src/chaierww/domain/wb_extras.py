"""WorkBuddy 成长中心里那些「还能纯接口做完」的活动。

本模块只负责**协议**：读状态、判定能不能做、把动作发出去；把它翻译成
`ActivityItem` / `RunResult` 是 `activities.py` 的事。这样活动页的目录、
状态语义与执行结果仍然只有一套。

## 覆盖范围（对照 `NextAgentX/qoder-trae-workbuddy-switch`）

- 连签奖励链：每日礼包 / 补偿、昨日补签、档位兑换、抽奖（`activity.rs`）
- 派猫猫旅行：idle → depart → traveling → arrived → claim（`travel.rs`）
- 夜猫子：23:00–08:00 窗口内补 1 次 GLM-5.2 夜间对话并领奖（`cat.rs`）
- 开学季：小程序任务 viewed → 触发判据 → claim，收尾抽转盘（`school.rs`）

## 诚实性纪律

1. 上游「正常态」不报失败：已领取（duplicate/409）、无次数（400）、未达标（403）、
   不在活动期（`in_period=false`）都是 **skipped**，不是 failed。
2. 查不到就说查不到：接口失败返回 `state="unavailable"` + 原因，不猜「已完成」。
3. 不代做人工环节：开学季的学生认证、成长任务里的专家/技能族仍标「需手动」。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

# 固定 +08:00，不依赖系统 tzdata（Windows 上 zoneinfo 可能没有数据）。
CST = timezone(timedelta(hours=8))

STREAK_PATH = "/activity/growth/streak"
HEATMAP_PATH = "/activity/growth/heatmap"
MAKEUP_PATH = "/activity/growth/makeup-cards/use"
REDEEM_PATH = "/activity/growth/redeem"
LOTTERY_CHANCES_PATH = "/activity/growth/lottery/chances"
LOTTERY_DRAW_PATH = "/activity/growth/lottery/draw"
CLAIM_GIFT_PATH = "/billing/meter/claim-gift"
CLAIM_COMPENSATION_PATH = "/billing/meter/claim-compensation"
REPORT_PATH = "/v2/report"
TASKS_PATH = "/v2/activity/growth/tasks"
ACCEPT_PATH = "/v2/activity/growth/tasks/accept"
CAT_CLAIM_PATH = "/activity/growth/tasks/black_cat/claim"
TRAVEL_PREFIX = "/activity/growth/buddy/travel"
SCHOOL_TASKS_PATH = "/portal/activity/school/tasks"
SCHOOL_CONFIG_PATH = "/portal/activity/school/config"
SCHOOL_SHARE_PATH = "/portal/activity/school/tasks/share-complete"
SCHOOL_WHEEL_PATH = "/portal/activity/school/wheel/draw"

SCHOOL_ACTIVITY_ID = "school_open_day_2026"
FALLBACK_EXPERT_ID = "ex_jB0dyFIQJEWa"
FALLBACK_EXPERT_NAME = "论小舟"
MINI_UA = (
    "Mozilla/5.0 (Linux; Android 14; MicroMessenger/8.0.49 WeChat/0.8.0 "
    "MiniProgramEnv/android; wkbrowser xweb)"
)

CAT_TARGET = 3
CAT_CAP = 1
#: 单次执行里最多抽几次，防止上游余额字段异常时无限抽。
LOTTERY_MAX_DRAWS = 10
#: 写动作之间的间隔（与参考实现 1.5s 对齐，避免触发风控）。
WRITE_GAP_SECONDS = 1.5

GIFT_STEP = "workbuddy.streak.gift"
COMPENSATION_STEP = "workbuddy.streak.compensation"
MAKEUP_STEP = "workbuddy.streak.makeup"
REDEEM_STEP = "workbuddy.streak.redeem"
LOTTERY_STEP = "workbuddy.streak.lottery"

#: 领取类动作的当日闸：同一账号同一天只打一次上游（重启即失效，上游还有幂等兜底）。
_GATE: set[tuple[str, str, str]] = set()
_GATE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class ExtraActivity:
    """一项额外活动的检测结论（由 activities 翻译成 ActivityItem）。"""

    state: str
    note: str = ""
    automatable: bool = False
    progress_current: int | None = None
    progress_target: int | None = None
    progress_text: str = ""
    reward: float | None = None
    reward_text: str = ""


@dataclass(frozen=True, slots=True)
class ExtraStep:
    """一项额外活动的执行结果（status 取 activities 的 RESULT_* 常量）。"""

    key: str
    title: str
    status: str
    message: str
    reward: float | None = None


# ---------- 基础工具 ----------


def _now_ms() -> int:
    return int(datetime.now(tz=CST).timestamp() * 1000)


def _today() -> str:
    return datetime.now(tz=CST).strftime("%Y-%m-%d")


def _yesterday() -> str:
    return (datetime.now(tz=CST) - timedelta(days=1)).strftime("%Y-%m-%d")


def _gate_allow(uid: str, action: str) -> bool:
    with _GATE_LOCK:
        return (uid, action, _today()) not in _GATE


def _gate_mark(uid: str, action: str) -> None:
    with _GATE_LOCK:
        _GATE.add((uid, action, _today()))


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.strip()))
        except ValueError:
            return None
    return None


def _num(value: Any) -> float | None:
    number = _int(value)
    return float(number) if number is not None else None


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _data(body: Any) -> dict[str, Any]:
    if isinstance(body, dict) and isinstance(body.get("data"), dict):
        return body["data"]
    return {}


def _ok(body: Any, status_code: int) -> bool:
    return status_code == 200 and isinstance(body, dict) and body.get("code") in (0, 200, None)


def _message(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    return _text(body.get("msg")) or _text(body.get("message"))


def _call(
    client: httpx.Client,
    method: str,
    url: str,
    headers: dict[str, str],
    body: Any | None = None,
) -> tuple[int, Any]:
    try:
        response = client.request(method, url, headers=headers, json=body)
    except httpx.HTTPError:
        return 0, None
    try:
        return response.status_code, response.json()
    except ValueError:
        return response.status_code, None


#: 传输层签名：`(method, url, headers, body) -> (status_code, json)`。
#: 由 `activities.py` 注入，保证它的 `_call` 补丁/桩在测试里同样作用于本模块。
Call = Callable[[str, str, dict[str, str], Any | None], tuple[int, Any]]


def growth_headers(headers: dict[str, str], origin: str) -> dict[str, str]:
    """成长中心请求头：在标准头基础上把 Referer 指向活动页。"""
    return {**headers, "Referer": f"{origin}/profile/growth-center"}


def school_headers(token: str, uid: str, domain: str, origin: str) -> dict[str, str]:
    """小程序域请求头（UA / Origin / Referer 与微信小程序一致）。"""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "X-Client-Platform": "web",
        "Origin": origin,
        "Referer": f"{origin}/",
        "User-Agent": MINI_UA,
    }
    if uid:
        headers["X-User-Id"] = uid
    if domain:
        headers["X-Domain"] = domain
    return headers


# ---------- 连签奖励链 ----------


def _streak_payload(call: Call, origin: str, headers: dict[str, str]) -> dict[str, Any]:
    status_code, body = call("GET", f"{origin}{STREAK_PATH}", headers, None)
    return _data(body) if _ok(body, status_code) else {}


def _eligible_tier(
    days: int, redemption: dict[str, Any], tiers: list[Any]
) -> tuple[str, float | None] | None:
    """挑「天数够、还没领」里最高的一档；返回 ``(tier, credit)``。"""
    best: tuple[int, str, float | None] | None = None
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        name = _text(tier.get("tier"))
        need = _int(tier.get("days"))
        if not name:
            continue
        if need is None:
            digits = "".join(char for char in name if char.isdigit())
            need = int(digits) if digits else None
        if need is None or days < need:
            continue
        if _text(redemption.get(f"tier_{name}_status")) in ("claimed", "done"):
            continue
        candidate = (need, name, _num(tier.get("credit")))
        if best is None or candidate[0] > best[0]:
            best = candidate
    return (best[1], best[2]) if best is not None else None


def _heatmap_missed_yesterday(
    call: Call, origin: str, headers: dict[str, str]
) -> bool | None:
    """昨日热力格是否为 0；``None`` = 查不到（无判据，不补签）。"""
    status_code, body = call("GET", f"{origin}{HEATMAP_PATH}", headers, None)
    if not _ok(body, status_code):
        return None
    cells = _data(body).get("cells")
    if not isinstance(cells, list):
        return None
    yesterday = _yesterday()
    for cell in cells:
        if not isinstance(cell, dict):
            continue
        date = _text(cell.get("date"))
        if date[:10] == yesterday:
            return (_int(cell.get("score")) or 0) == 0
    return None


def run_streak_rewards(
    call: Call,
    origin: str,
    headers: dict[str, str],
    uid: str,
) -> list[ExtraStep]:
    """连签奖励链：礼包/补偿 → 补签 → 兑换 → 抽奖。"""
    steps: list[ExtraStep] = []
    growth = growth_headers(headers, origin)

    # 1) 每日礼包 / 补偿：有则领，业务错误静默（大多数号早就领过）。
    for path, key, title in (
        (CLAIM_GIFT_PATH, GIFT_STEP, "每日礼包"),
        (CLAIM_COMPENSATION_PATH, COMPENSATION_STEP, "补偿礼包"),
    ):
        if not _gate_allow(uid, key):
            steps.append(ExtraStep(key, title, "skipped", "今日已处理过"))
            continue
        status_code, body = call("POST", f"{origin}{path}", growth, {})
        _gate_mark(uid, key)
        if _ok(body, status_code):
            steps.append(ExtraStep(key, title, "ok", "领取成功"))
        else:
            steps.append(
                ExtraStep(key, title, "skipped", _message(body) or "当前没有可领的礼包")
            )

    # 2) 补签：只补「昨日漏签且手里有补签卡」的情况。
    if _gate_allow(uid, MAKEUP_STEP):
        missed = _heatmap_missed_yesterday(call, origin, growth)
        state = _streak_payload(call, origin, growth)
        cards = state.get("makeup_cards") if isinstance(state.get("makeup_cards"), dict) else {}
        balance = _int(cards.get("balance")) or 0
        if missed is None:
            steps.append(ExtraStep(MAKEUP_STEP, "昨日补签", "skipped", "热力图不可用，未补签"))
        elif not missed:
            steps.append(ExtraStep(MAKEUP_STEP, "昨日补签", "skipped", "昨日没有漏签"))
        elif balance <= 0:
            steps.append(ExtraStep(MAKEUP_STEP, "昨日补签", "skipped", "补签卡不足"))
        else:
            status_code, body = call(
                "POST", f"{origin}{MAKEUP_PATH}", growth, {"target_date": _yesterday()}
            )
            _gate_mark(uid, MAKEUP_STEP)
            if _ok(body, status_code):
                steps.append(ExtraStep(MAKEUP_STEP, "昨日补签", "ok", "补签成功"))
            elif status_code in (0, 404, 405, 409):
                steps.append(
                    ExtraStep(MAKEUP_STEP, "昨日补签", "skipped", _message(body) or "补签未成功")
                )
            else:
                steps.append(
                    ExtraStep(MAKEUP_STEP, "昨日补签", "failed", _message(body) or "补签失败")
                )
    else:
        steps.append(ExtraStep(MAKEUP_STEP, "昨日补签", "skipped", "今日已处理过"))

    # 3) 兑换：补签可能刚刚把天数补回来了，这里重读一次。
    if _gate_allow(uid, REDEEM_STEP):
        state = _streak_payload(call, origin, growth)
        streak = state.get("streak") if isinstance(state.get("streak"), dict) else {}
        days = _int(streak.get("days")) or 0
        redemption = (
            state.get("redemption_status")
            if isinstance(state.get("redemption_status"), dict)
            else {}
        )
        tiers = redemption.get("tiers") if isinstance(redemption.get("tiers"), list) else []
        choice = _eligible_tier(days, redemption, tiers)
        if choice is None:
            steps.append(ExtraStep(REDEEM_STEP, "连签兑换", "skipped", "没有可兑换的档位"))
        else:
            tier, credit = choice
            payload = {"tier": tier, "client_token": f"redeem-{tier}-{uuid.uuid4().hex}"}
            status_code, body = call("POST", f"{origin}{REDEEM_PATH}", growth, payload)
            _gate_mark(uid, REDEEM_STEP)
            message = _message(body)
            if _ok(body, status_code):
                detail = f"已兑换 {tier} 档" + (f" +{int(credit)}" if credit else "")
                steps.append(ExtraStep(REDEEM_STEP, "连签兑换", "ok", detail, credit))
            elif status_code in (0, 403, 404, 405, 409):
                steps.append(
                    ExtraStep(REDEEM_STEP, "连签兑换", "skipped", message or "没有可兑换的档位")
                )
            else:
                steps.append(
                    ExtraStep(REDEEM_STEP, "连签兑换", "failed", message or "兑换失败")
                )
    else:
        steps.append(ExtraStep(REDEEM_STEP, "连签兑换", "skipped", "今日已处理过"))

    # 4) 抽奖：只消耗已有次数。
    status_code, body = call("GET", f"{origin}{LOTTERY_CHANCES_PATH}", growth, None)
    balance = (_int(_data(body).get("balance")) or 0) if _ok(body, status_code) else 0
    if balance <= 0:
        steps.append(ExtraStep(LOTTERY_STEP, "连签抽奖", "skipped", "没有抽奖次数"))
    else:
        drawn = 0
        for _ in range(min(balance, LOTTERY_MAX_DRAWS)):
            payload = {"client_token": f"draw-{uuid.uuid4().hex}"}
            draw_status, draw_body = call(
                "POST", f"{origin}{LOTTERY_DRAW_PATH}", growth, payload
            )
            if draw_status == 200 and _ok(draw_body, draw_status):
                drawn += 1
                continue
            break
        if drawn:
            steps.append(ExtraStep(LOTTERY_STEP, "连签抽奖", "ok", f"抽了 {drawn} 次"))
        else:
            steps.append(ExtraStep(LOTTERY_STEP, "连签抽奖", "skipped", "抽奖未开启或次数不足"))
    return steps


# ---------- 派猫猫旅行 ----------


def _travel_config(
    call: Call, origin: str, headers: dict[str, str]
) -> tuple[bool, list[dict[str, Any]]]:
    growth = growth_headers(headers, origin)
    status_code, body = call("GET", f"{origin}{TRAVEL_PREFIX}/config", growth, None)
    data = _data(body) if _ok(body, status_code) else {}
    enabled = data.get("enabled")
    locations = data.get("locations") if isinstance(data.get("locations"), list) else []
    return (True if enabled is None else bool(enabled)), [
        item for item in locations if isinstance(item, dict)
    ]


def _travel_status(
    call: Call, origin: str, headers: dict[str, str]
) -> dict[str, Any] | None:
    growth = growth_headers(headers, origin)
    status_code, body = call("GET", f"{origin}{TRAVEL_PREFIX}/status", growth, None)
    if not _ok(body, status_code):
        return None
    return _data(body)


def detect_travel(
    call: Call, origin: str, headers: dict[str, str]
) -> ExtraActivity:
    enabled, _ = _travel_config(call, origin, headers)
    if not enabled:
        return ExtraActivity(state="not_started", note="派猫猫旅行当前未开放")
    status = _travel_status(call, origin, headers)
    if status is None:
        return ExtraActivity(state="unavailable", note="旅行接口不可用（活动可能未开放）")
    state = _text(status.get("state"))
    location = status.get("location") if isinstance(status.get("location"), dict) else {}
    place = _text(location.get("name"))
    if state == "arrived":
        return ExtraActivity(
            state="todo",
            automatable=True,
            note=f"已到达{place}，奖励待领取" if place else "猫猫已到达，奖励待领取",
        )
    if state == "traveling":
        return ExtraActivity(
            state="in_progress",
            note=f"正在前往{place}的路上" if place else "猫猫正在旅行中",
        )
    if state == "idle" and bool(status.get("daily_limit_reached")):
        return ExtraActivity(state="done", note="今日已派完，明天再来")
    if state == "idle":
        return ExtraActivity(state="todo", automatable=True, note="可以派猫猫出门了")
    return ExtraActivity(state="unavailable", note="旅行状态无法识别")


TRAVEL_KEY = "workbuddy.travel"
TRAVEL_TITLE = "派猫猫旅行"


def _travel_context(
    call: Call, origin: str, headers: dict[str, str]
) -> tuple[bool, list[dict[str, Any]], dict[str, Any] | None]:
    """一次取齐：活动是否开放、可选地点、当前旅行状态。"""
    enabled, locations = _travel_config(call, origin, headers)
    if not enabled:
        return False, locations, None
    return True, locations, _travel_status(call, origin, headers)


def _travel_guard(enabled: bool, status: dict[str, Any] | None) -> ExtraStep | None:
    """开场检查：活动没开放 / 接口不可用时直接给跳过项。"""
    if not enabled:
        return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "派猫猫旅行当前未开放")
    if status is None:
        return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "旅行接口不可用（活动可能未开放）")
    return None


def travel_claim(
    call: Call, origin: str, headers: dict[str, str]
) -> ExtraStep:
    """只做「回领」：猫回来了就领奖，没回来就说明为什么没领。

    与 ``travel_depart`` 拆开，是为了让「早上派、下午领」能排成两个独立计划任务。
    以前两者挤在一个函数里、谁先命中就 return，导致同一天只能做其中一件。
    """
    enabled, _, status = _travel_context(call, origin, headers)
    guard = _travel_guard(enabled, status)
    if guard is not None:
        return guard
    assert status is not None
    state = _text(status.get("state"))
    if state != "arrived":
        if state == "traveling":
            return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "猫猫还在路上，等回来再领")
        if state == "idle" and bool(status.get("daily_limit_reached")):
            return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "今日已派完，没有待领的奖励")
        if state == "idle":
            return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "猫猫还没出门，没有可领的奖励")
        return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "旅行状态无法识别")

    growth = growth_headers(headers, origin)
    record_id = _int(status.get("record_id")) or 0
    payload = {"record_id": record_id} if record_id > 0 else {}
    status_code, body = call("POST", f"{origin}{TRAVEL_PREFIX}/claim", growth, payload)
    if _ok(body, status_code):
        data = _data(body)
        credit = _num(data.get("reward_credit")) or _num(data.get("credit"))
        detail = f"领取成功 +{int(credit)}" if credit else "领取成功"
        return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "ok", detail, credit)
    return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "failed", _message(body) or "领取失败")


def travel_depart(
    call: Call, origin: str, headers: dict[str, str]
) -> ExtraStep:
    """只做「派出」：空闲且今天还没派完就出门。"""
    enabled, locations, status = _travel_context(call, origin, headers)
    guard = _travel_guard(enabled, status)
    if guard is not None:
        return guard
    assert status is not None
    state = _text(status.get("state"))
    if state == "traveling":
        return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "猫猫已经在路上了")
    if state == "arrived":
        return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "有奖励待领取，先领再派")
    if state == "idle" and bool(status.get("daily_limit_reached")):
        return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "今日已派完")
    if state != "idle":
        return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "旅行状态无法识别")

    growth = growth_headers(headers, origin)
    location = locations[0] if locations else {}
    location_id = location.get("id")
    if location_id is None:
        return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "没有可选的旅行地点")
    status_code, body = call(
        "POST", f"{origin}{TRAVEL_PREFIX}/depart", growth, {"location_id": location_id}
    )
    if _ok(body, status_code):
        name = _text(location.get("name"))
        return ExtraStep(
            TRAVEL_KEY, TRAVEL_TITLE, "ok", f"已派出，目的地{name}" if name else "已派出"
        )
    message = _message(body)
    lowered = message.lower()
    if "already traveling" in lowered:
        return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "猫猫已经在路上了")
    if "daily limit" in lowered or "daily_limit" in lowered:
        return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "今日已派完")
    if "no active buddy" in lowered:
        return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "skipped", "账号还没有 Buddy")
    return ExtraStep(TRAVEL_KEY, TRAVEL_TITLE, "failed", message or "派发失败")


def run_travel(
    call: Call, origin: str, headers: dict[str, str]
) -> ExtraStep:
    """手动「全部活动」用：先试着回领，领不到再看能不能派出。

    两个动作在同一次执行里不会都成功（领完再派会撞每日上限），所以按「领优先」
    的次序做；都跳过时用回领的说明，因为它更能反映现状（「还在路上」优于「今日已派完」）。
    """
    claim = travel_claim(call, origin, headers)
    if claim.status != "skipped":
        return claim
    depart = travel_depart(call, origin, headers)
    if depart.status == "ok":
        return depart
    return claim


#: 计划任务用的阶段名。`all` 是手动「全部活动」的组合行为。
TRAVEL_STAGES: tuple[str, ...] = ("depart", "claim", "all")


def run_travel_stage(
    call: Call, origin: str, headers: dict[str, str], stage: str
) -> ExtraStep:
    """按阶段跑派猫猫旅行。未知阶段按 `all` 处理，绝不静默什么都不做。"""
    if stage == "depart":
        return travel_depart(call, origin, headers)
    if stage == "claim":
        return travel_claim(call, origin, headers)
    return run_travel(call, origin, headers)



# ---------- 夜猫子 ----------


def _find_task(tasks: list[Any], code: str) -> dict[str, Any] | None:
    for task in tasks:
        if isinstance(task, dict) and _text(task.get("task_code")) == code:
            return task
    return None


def _task_progress(task: dict[str, Any], default_target: int) -> tuple[int, int]:
    progress = task.get("progress") if isinstance(task.get("progress"), dict) else {}
    return _int(progress.get("current")) or 0, _int(progress.get("target")) or default_target


def in_night_window() -> bool:
    hour = datetime.now(tz=CST).hour
    return hour >= 23 or hour < 8


def detect_black_cat(tasks: list[Any]) -> ExtraActivity | None:
    """夜猫子任务：不在任务列表里返回 ``None``（不占位）。"""
    task = _find_task(tasks, "black_cat")
    if task is None:
        return None
    status = _text(task.get("accept_status")).lower()
    current, target = _task_progress(task, CAT_TARGET)
    if status == "claimed":
        return ExtraActivity(state="done", note="夜猫子奖励已领取")
    if status == "completed" or current >= target:
        return ExtraActivity(
            state="todo",
            automatable=True,
            progress_current=current,
            progress_target=target,
            progress_text=f"{current}/{target}",
            note="已达成，奖励待领取",
        )
    window = in_night_window()
    return ExtraActivity(
        state="todo" if window else "in_progress",
        automatable=True,
        progress_current=current,
        progress_target=target,
        progress_text=f"{current}/{target}",
        note=(
            "夜间窗口内可补一次夜间对话"
            if window
            else f"当前不在 23:00–08:00 窗口，进度 {current}/{target}"
        ),
    )


def _cat_event(uid: str, conversation_id: str) -> dict[str, Any]:
    now = _now_ms()
    return {
        "eventCode": "chat_request_send",
        "timestamp": now,
        "reportDelay": 0,
        "mode": "night",
        "conversationId": conversation_id,
        "requestId": conversation_id,
        "inputLength": 12,
        "requestModelId": "glm-5.2",
        "requestModelName": "GLM-5.2",
        "presentAt": now,
        "rootRequestId": conversation_id,
        "parentConversationId": conversation_id,
        "agentName": "default",
        "userId": uid,
    }


def run_black_cat(
    call: Call, origin: str, headers: dict[str, str], uid: str
) -> ExtraStep:
    key, title = "workbuddy.black_cat", "夜猫子"
    if not in_night_window():
        return ExtraStep(key, title, "skipped", "当前不在 23:00–08:00 窗口")
    growth = growth_headers(headers, origin)
    status_code, body = call("GET", f"{origin}{TASKS_PATH}", growth, None)
    tasks = _data(body).get("tasks") if _ok(body, status_code) else None
    if not isinstance(tasks, list):
        return ExtraStep(key, title, "failed", "任务列表读取失败")
    task = _find_task(tasks, "black_cat")
    if task is None:
        return ExtraStep(key, title, "skipped", "没有夜猫子任务")
    status = _text(task.get("accept_status")).lower()
    if status == "claimed":
        return ExtraStep(key, title, "skipped", "夜猫子奖励已领取")
    current, target = _task_progress(task, CAT_TARGET)

    if status in ("", "not_accepted"):
        call("POST", f"{origin}{ACCEPT_PATH}", growth, {"task_codes": ["black_cat"]})

    if status != "completed" and current < target:
        for _ in range(min(max(target - current, 0), CAT_CAP)):
            conversation_id = f"wbcat-{_now_ms()}"
            event = _cat_event(uid, conversation_id)
            report_status, _ = call("POST", f"{origin}{REPORT_PATH}", growth, [event])
            if report_status != 200:
                break

    # 回读后再决定领奖，避免用旧进度误领。
    status_code, body = call("GET", f"{origin}{TASKS_PATH}", growth, None)
    tasks = _data(body).get("tasks") if _ok(body, status_code) else None
    latest = _find_task(tasks, "black_cat") if isinstance(tasks, list) else None
    if latest is None:
        return ExtraStep(key, title, "skipped", "任务进度无法确认")
    latest_status = _text(latest.get("accept_status")).lower()
    current, target = _task_progress(latest, CAT_TARGET)
    if latest_status == "claimed":
        return ExtraStep(key, title, "skipped", "夜猫子奖励已领取")
    if latest_status == "completed" or current >= target:
        claim_status, claim_body = call("POST", f"{origin}{CAT_CLAIM_PATH}", growth, None)
        if _ok(claim_body, claim_status):
            return ExtraStep(key, title, "ok", "领取成功")
        message = _message(claim_body)
        if claim_status in (400, 404, 409) or "已领取" in message:
            return ExtraStep(key, title, "skipped", message or "奖励已领取")
        return ExtraStep(key, title, "failed", message or "领取失败")
    return ExtraStep(key, title, "skipped", f"进度 {current}/{target}，本次已补过一次")


# ---------- 开学季 ----------


def school_task_action(code: str) -> str | None:
    return {
        "chat_3_times": "mini_chat",
        "expert_use": "expert",
        "share_invite": "share",
        "desktop_chat_1_time": "desktop_seq",
    }.get(code)


def _school_get(call: Call, origin: str, headers: dict[str, str]) -> tuple[int, Any]:
    return call("GET", f"{origin}{SCHOOL_TASKS_PATH}", headers, None)


def detect_school(call: Call, origin: str, headers: dict[str, str]) -> ExtraActivity:
    status_code, body = _school_get(call, origin, headers)
    data = _data(body)
    if not _ok(body, status_code):
        return ExtraActivity(state="unavailable", note=_message(body) or "开学季活动读取失败")
    if not bool(data.get("in_period")):
        return ExtraActivity(state="not_started", note="开学季活动已结束或尚未开始")
    tasks = data.get("tasks") if isinstance(data.get("tasks"), list) else []
    if not tasks:
        return ExtraActivity(state="unavailable", note="活动没有返回任务")
    claimed: list[str] = []
    pending: list[str] = []
    automatable = False
    for task in tasks:
        if not isinstance(task, dict):
            continue
        code = _text(task.get("task_code"))
        state = _text(task.get("status")).lower()
        title = _text(task.get("title")) or code
        if state == "claimed":
            claimed.append(title)
            continue
        pending.append(title)
        if school_task_action(code) and state in (
            "",
            "pending",
            "in_progress",
            "viewed",
            "completed",
        ):
            automatable = True
    total = len(claimed) + len(pending)
    note = ""
    if pending:
        shown = "、".join(pending[:3])
        more = f" 等 {len(pending)} 项" if len(pending) > 3 else ""
        note = f"还差：{shown}{more}"
    return ExtraActivity(
        state="done" if not pending else "todo",
        automatable=automatable,
        progress_current=len(claimed),
        progress_target=total,
        progress_text=f"{len(claimed)}/{total} 已领取",
        note=note,
    )


def _mini_chat_event(uid: str, conversation_id: str) -> dict[str, Any]:
    return {
        "eventCode": "chat_request_send",
        "timestamp": _now_ms(),
        "reportDelay": 0,
        "source": "mini_program",
        "ideName": "wx_app_cloud",
        "ideType": "WorkBuddy_MP",
        "extName": "workbuddy-mp",
        "extVersion": "SaaS",
        "mode": "chat",
        "conversationId": conversation_id,
        "requestId": conversation_id,
        "inputLength": 12,
        "activityId": SCHOOL_ACTIVITY_ID,
        "mentionContexts": [],
        "mentionContextCount": 0,
        "userId": uid,
    }


def _expert_event(uid: str, conversation_id: str) -> dict[str, Any]:
    return {
        "eventCode": "expert_actual_use",
        "timestamp": _now_ms(),
        "reportDelay": 0,
        "source": "mini_program",
        "ideName": "wx_app_cloud",
        "ideType": "WorkBuddy_MP",
        "extName": "workbuddy-mp",
        "extVersion": "SaaS",
        "userId": uid,
        "id": FALLBACK_EXPERT_ID,
        "name": FALLBACK_EXPERT_ID,
        "expertTitle": FALLBACK_EXPERT_NAME,
        "type": "send_message",
        "characterCount": 12,
        "expertType": "agent",
        "conversationId": conversation_id,
        "activityId": SCHOOL_ACTIVITY_ID,
    }


def _desktop_events(uid: str, conversation_id: str) -> list[dict[str, Any]]:
    at = _now_ms()
    base = {"userId": uid, "activityId": SCHOOL_ACTIVITY_ID}

    def event(code: str) -> dict[str, Any]:
        return {"eventCode": code, **base}

    created = {
        **event("agent_task_created"),
        "source": "LOCAL",
        "name": "working",
        "task_target": "local",
        "mode": "craft",
        "requestModelId": "fast-model",
        "requestModelName": "fast-model",
        "conversationId": conversation_id,
        "messageId": conversation_id,
    }
    send = {
        **event("chat_message_send"),
        "messageId": f"{conversation_id}-assistant",
        "historyCount": 0,
        "currentStepCount": 1,
        "traceId": conversation_id,
        "rootRequestId": conversation_id,
        "parentConversationId": conversation_id,
        "agentName": "cli",
        "agentType": "main",
    }
    request = {
        **event("chat_request_send"),
        "inputLength": 24,
        "mode": "craft",
        "conversationId": conversation_id,
        "requestId": conversation_id,
        "traceId": conversation_id,
        "rootRequestId": conversation_id,
        "parentConversationId": conversation_id,
        "agentName": "cli",
        "agentType": "main",
        "timestamp": at,
    }
    response = {
        **event("chat_message_response"),
        "messageId": f"{conversation_id}-assistant",
        "responseModelId": "fast-model",
        "isSuccessful": True,
        "finishReason": "stop",
        "traceId": conversation_id,
        "conversationId": conversation_id,
        "rootRequestId": conversation_id,
        "parentConversationId": conversation_id,
        "agentName": "cli",
        "agentType": "main",
    }
    status = {
        **event("chat_message_status"),
        "messageId": f"{conversation_id}-assistant",
        "messageErrorCode": "0",
        "traceId": conversation_id,
        "rootRequestId": conversation_id,
        "parentConversationId": conversation_id,
    }
    done = {
        **event("chat_request_response"),
        "mode": "craft",
        "isSuccessful": True,
        "finishReason": "stop",
        "rootRequestId": conversation_id,
        "parentConversationId": conversation_id,
    }
    return [created, send, request, response, status, done]


def _school_tasks(
    call: Call, origin: str, headers: dict[str, str]
) -> list[Any] | None:
    status_code, body = _school_get(call, origin, headers)
    if not _ok(body, status_code):
        return None
    tasks = _data(body).get("tasks")
    return tasks if isinstance(tasks, list) else None


def _process_school_task(
    call: Call,
    origin: str,
    headers: dict[str, str],
    uid: str,
    code: str,
    action: str,
    task: dict[str, Any],
) -> str:
    """返回 ``claimed`` / ``already`` / ``pending`` / ``failed``。"""
    status = _text(task.get("status")).lower()
    if status == "claimed":
        return "already"
    current = _int(task.get("progress")) or 0
    target = _int(task.get("target_count")) or 1
    if status == "pending":
        call(
            "POST",
            f"{origin}/portal/activity/school/tasks/{code}/viewed",
            headers,
            {},
        )
    # 已完成只是还没领奖：不必再触发判据，直接走回读 + claim。
    rounds = 0 if status == "completed" else (
        1 if action in ("share", "desktop_seq") else max(target - current, 1)
    )
    for _ in range(rounds):
        if action == "share":
            status_code, body = call(
                "POST", f"{origin}{SCHOOL_SHARE_PATH}", headers, {"channel": "wechat"}
            )
        else:
            conversation_id = f"wb{school_task_action(code) or 'task'}-{_now_ms()}"
            if action == "mini_chat":
                events: Any = [_mini_chat_event(uid, conversation_id)]
            elif action == "expert":
                events = [_expert_event(uid, conversation_id)]
            else:
                events = _desktop_events(uid, conversation_id)
            status_code, body = call("POST", f"{origin}{REPORT_PATH}", headers, events)
        if status_code and status_code >= 400:
            break

    tasks = _school_tasks(call, origin, headers) or []
    latest = _find_task(tasks, code)
    if latest is None:
        return "failed"
    after_status = _text(latest.get("status")).lower()
    after_progress = _int(latest.get("progress")) or current
    if after_status == "claimed":
        return "already"
    if after_status == "completed" or after_progress >= target:
        claim_status, claim_body = call(
            "POST",
            f"{origin}/portal/activity/school/tasks/{code}/claim",
            headers,
            {},
        )
        return "claimed" if _ok(claim_body, claim_status) else "failed"
    return "pending"


def run_school(
    call: Call, origin: str, headers: dict[str, str], uid: str
) -> ExtraStep:
    key, title = "workbuddy.school", "开学季活动"
    status_code, body = call("GET", f"{origin}{SCHOOL_CONFIG_PATH}", headers, None)
    if not _ok(body, status_code):
        return ExtraStep(key, title, "skipped", "活动未开启或已结束")
    if not bool(_data(body).get("in_period")):
        return ExtraStep(key, title, "skipped", "活动不在进行期")

    tasks = _school_tasks(call, origin, headers)
    if tasks is None:
        return ExtraStep(key, title, "failed", "任务列表读取失败")

    done: list[str] = []
    pending: list[str] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        code = _text(task.get("task_code"))
        action = school_task_action(code)
        if action is None:
            continue
        label = _text(task.get("title")) or code
        result = _process_school_task(call, origin, headers, uid, code, action, task)
        if result in ("claimed", "already"):
            done.append(label)
        elif result == "pending":
            pending.append(label)

    # 收尾：把转盘次数抽掉。
    drawn = 0
    config_status, config_body = call("GET", f"{origin}{SCHOOL_CONFIG_PATH}", headers, None)
    chance = _data(config_body).get("chance")
    balance = _int(chance.get("balance")) if isinstance(chance, dict) else 0
    while (balance or 0) > 0 and drawn < LOTTERY_MAX_DRAWS:
        draw_status, draw_body = call(
            "POST",
            f"{origin}{SCHOOL_WHEEL_PATH}",
            headers,
            {"draw_uuid": str(uuid.uuid4())},
        )
        if draw_status == 409 or not _ok(draw_body, draw_status):
            break
        next_balance = _int(_data(draw_body).get("chance_balance"))
        if next_balance is None or next_balance >= (balance or 0):
            break
        balance = next_balance
        drawn += 1

    parts: list[str] = []
    if done:
        parts.append(f"已完成 {len(done)} 项")
    if pending:
        parts.append(f"还差：{'、'.join(pending[:3])}")
    if drawn:
        parts.append(f"转盘抽了 {drawn} 次")
    if not parts:
        return ExtraStep(key, title, "skipped", "没有可自动完成的任务")
    status = "ok" if done or drawn else "skipped"
    return ExtraStep(key, title, status, "；".join(parts))
