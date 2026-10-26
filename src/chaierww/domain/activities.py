"""活动检测与一键执行。

「活动」在本工具里指**账号在平台上的签到/成长类奖励**，只做两件事：
把每个账号当前能做什么如实查出来，把其中**可以纯接口完成**的那部分代做掉。

## 协议出处（2026-10-19 用真实账号逐个验过，不是猜的）

WorkBuddy（`{origin}` 由令牌签发域决定，见 `platform_api.workbuddy_origin`）

- 签到状态 `POST /v2/billing/meter/checkin-activity-status`
  关键字段：`active` / `today_checked_in` / `streak_days` / `daily_credit` /
  `today_credit` / `week_checkin_days` / `start_time` / `end_time` /
  `theme_name` / `season`
- 执行签到 `POST /v2/billing/meter/daily-checkin`
  `code=0` 成功；已签到时 `code=10001` + `msg="今天已签到，请明天再来"`
- 成长任务 `GET /v2/activity/growth/tasks`
  `data.tasks[]`，每项 `accept_status`（`claimed`/`accepted`/`not_accepted`）、
  `progress{current,target}`、`reward_credit`、`title`、`jump_url`
- 连签里程 `GET /activity/growth/streak`
  `streak.days` / `streak.next_tier{,_remaining}` / `makeup_cards.balance` /
  `redemption_status`
- 抽奖次数 `GET /activity/growth/lottery/chances` → `data.balance`

Trae（`{account_base}` 取自凭据里的 host，缺省 `https://api.trae.cn`）

- 签到状态 `POST /trae/api/v2/ug/checkin_credits/status`
  `checked_in` / `did_checked_in` / `enable` / `credits` / `extra_credits`
- 执行签到 `POST /trae/api/v2/ug/checkin_credits/claim` → `code=0`

## 为什么**不用** `checkin-status` 当兜底（重要）

旧项目的 `get_checkin_status` 在 `checkin-activity-status` 失败时会回退到
`/v2/billing/meter/checkin-status`。实测这条老接口**对已签到的账号也返回
`code:0` + `today_checked_in:false` + 全零**（`active:false`，是上一代活动的空壳）。
拿它当兜底会得到「今天没签」的**假结论** —— 于是工具会去重复签到、
在界面上谎报未完成。所以这里只认 `checkin-activity-status`：
查不到就报「检测失败」，绝不猜。

## 诚实性纪律

1. **只代做有动作表覆盖的任务**。成长任务里 chat_5 / first_buddy / automation_1 /
   Library_read / template_5 / playbook_prompt / create_canvas / RichMeow_Chat /
   Buddy_App / Hp_Appearance 十项按参考项目协议做纯 API 上报 + 领奖（见 `wb_tasks`）；
   expert/skill/小程序族等需要真实客户端操作的任务**不代做**，标「需手动」+ 原因。
2. **不可查 ≠ 什么都没做**。WorkBuddy 客户端导入的账号令牌是 DPAPI 信封，
   查不了就 `detectable=false` + 原因，绝不当成「没有活动」。
3. **不谎报收益**。签到成功但响应里没有具体额度时，message 只说「签到成功」，
   不编一个数字出来。
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from chaierww.domain.accounts.models import Account, CredentialState
from chaierww.domain.accounts.repository import AccountNotFound
from chaierww.domain.platform_api import (
    TIMEOUT_SECONDS,
    TRAE_ACCOUNT_BASE,
    WORKBUDDY_WEB_ORIGIN,
    CredentialError,
    trae_credentials,
    trae_headers,
    workbuddy_credentials,
    workbuddy_headers,
    workbuddy_origin,
)
from chaierww.domain.scopes import UnknownScope, get_scope
from chaierww.domain.wb_extras import (
    ExtraActivity,
    ExtraStep,
    detect_black_cat,
    detect_school,
    detect_travel,
    run_black_cat,
    run_school,
    run_streak_rewards,
    run_travel,
    run_travel_stage,
    school_headers,
)
from chaierww.domain.wb_tasks import ACTIONS, GrowthTaskRunner

logger = logging.getLogger("chaierww")

#: 能被 wb_tasks.ACTIONS 自动执行的任务码集合。其余（expert/skill/小程序族）仍需手动。
_AUTOMATABLE_TASK_CODES = frozenset(ACTIONS.keys())

WB_CHECKIN_STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
WB_CHECKIN_PATH = "/v2/billing/meter/daily-checkin"
WB_TASKS_PATH = "/v2/activity/growth/tasks"
WB_STREAK_PATH = "/activity/growth/streak"
WB_LOTTERY_PATH = "/activity/growth/lottery/chances"

TRAE_CHECKIN_STATUS_PATH = "/trae/api/v2/ug/checkin_credits/status"
TRAE_CHECKIN_CLAIM_PATH = "/trae/api/v2/ug/checkin_credits/claim"

#: 签到 claim 的三个业务码。都是 HTTP 200 包着的业务失败，光看状态码看不出来。
TRAE_CODE_PARAM_INVALID = 9004  # 设备头缺失/形态不对
TRAE_CODE_BUSY = 9074  # 上游限流，可退避重试
TRAE_CODE_DEVICE_TAKEN = 9095  # 该设备今天已经被别的账号签到了

#: 9074 的退避梯度（秒）。上游是「先从门口挡回去、过一会儿再看」的策略，
#: 紧跟着立刻重试一定还是 9074，必须真等。梯度别太长：这段睡眠在调度线程里。
TRAE_CLAIM_RETRY_DELAYS: tuple[float, ...] = (5.0, 12.0)

#: 短期 = 每天重置；长期 = 活动期内累计。
GROUP_DAILY = "daily"
GROUP_SEASONAL = "seasonal"

#: 活动状态。与「能不能自动做」是**两个维度**：已完成的手动项状态仍是 done。
STATE_DONE = "done"
STATE_TODO = "todo"
STATE_IN_PROGRESS = "in_progress"
STATE_NOT_STARTED = "not_started"
STATE_UNAVAILABLE = "unavailable"

#: 一键执行的结果。
RESULT_OK = "ok"
RESULT_SKIPPED = "skipped"
RESULT_FAILED = "failed"

CACHE_TTL_SECONDS = 120.0
ACCOUNT_GAP_SECONDS = 0.8
#: 失败重试前的退避（秒）。上游 5xx / 网络抖动往往几秒内就恢复，但立刻重打只会再失败一次。
RETRY_BACKOFF_SECONDS = 3.0
#: 并发检测的账号数上限。上游对单账号有风控，但对不同账号并发是安全的。
DETECT_WORKERS = 4

#: WorkBuddy 判「今天已经签过了」的业务码。上游正常返回，不算失败。
WB_ALREADY_CHECKED_IN = 10001

MANUAL_REASON_TASKS = (
    "这些任务要在客户端里真实操作（专家对话、使用技能、关注公众号等）才算完成，"
    "本工具没有对应的自动化动作"
)
MANUAL_REASON_STREAK = (
    "补签、连签兑换、抽奖都要消耗账号里的补签卡或抽奖次数；当前两样都没有，"
    "等上游发下来后本工具会自动代做"
)

#: 活动目录：**零网络**的静态清单。页面先拿它渲染骨架卡片（「未检测」），
#: 再用真实检测结果按 ``key`` 覆盖 —— 这样冷启动不再是一片空白。
#:
#: ``key`` 必须与解析器输出的 key 严格一致（有单测兜底，防目录与检测漂移）。
ACTIVITY_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "key": "workbuddy.daily_checkin",
        "platform": "workbuddy",
        "group": GROUP_DAILY,
        "title": "每日签到",
        "subtitle": "每日一次，签到即得积分",
        "description": "在 WorkBuddy 活动页签到，按当前档位领取当日积分；可纯接口代签。",
        "rewardHint": "每日额度由活动档位决定",
        "automatable": True,
        "theme": "green",
        "icon": "calendar-check",
        "tags": ("每日", "自动"),
        "rules": (
            "每天可签到一次",
            "签到奖励按当前活动档位发放",
            "已领取的奖励不会因断签回收",
        ),
        "rewards": ("每日积分", "连续签到档位奖励"),
        "periodNote": "长期开放",
        "officialUrl": f"{WORKBUDDY_WEB_ORIGIN}/",
        "officialLabel": "WorkBuddy 官网",
        "featured": True,
        "sortOrder": 10,
    },
    {
        "key": "workbuddy.growth_tasks",
        "platform": "workbuddy",
        "group": GROUP_SEASONAL,
        "title": "成长任务",
        "subtitle": "活动期内累计，完成后领奖",
        "description": (
            "chat_5 / first_buddy / automation_1 等十项可纯接口上报并领奖；"
            "专家对话、技能使用、小程序族仍需在客户端真实操作。"
        ),
        "rewardHint": "每项 reward_credit 不等",
        "automatable": True,
        "theme": "blue",
        "icon": "sprout",
        "tags": ("活动期", "自动+手动"),
        "rules": (
            "活动期内完成成长任务后领取奖励",
            "可纯接口上报的任务由本工具代做",
            "专家对话、技能使用、小程序任务需在客户端手动完成",
        ),
        "rewards": ("每项任务 reward_credit", "活动期累计奖励"),
        "periodNote": "活动期内",
        "officialUrl": f"{WORKBUDDY_WEB_ORIGIN}/",
        "officialLabel": "WorkBuddy 官网",
        "featured": False,
        "sortOrder": 20,
    },
    {
        "key": "workbuddy.streak",
        "platform": "workbuddy",
        "group": GROUP_SEASONAL,
        "title": "连签里程",
        "subtitle": "连签天数达标后兑换奖励",
        "description": (
            "可代领每日礼包/补偿、补签昨日漏签、兑换达标档位并抽掉已有抽奖次数；"
            "上游已领取、无次数等正常态只记「跳过」。"
        ),
        "rewardHint": "档位奖励以「距下一档还差几天」估算",
        "automatable": True,
        "theme": "amber",
        "icon": "flame",
        "tags": ("活动期", "自动"),
        "rules": (
            "连续签到累计达标后可兑换档位奖励",
            "补签、兑换、抽奖会消耗账号内的补签卡或抽奖次数",
            "上游已领取或没有次数时只记录跳过",
        ),
        "rewards": ("连签档位奖励", "每日礼包/补偿", "抽奖次数"),
        "periodNote": "活动期内",
        "officialUrl": f"{WORKBUDDY_WEB_ORIGIN}/",
        "officialLabel": "WorkBuddy 官网",
        "featured": False,
        "sortOrder": 30,
    },
    {
        "key": "workbuddy.travel",
        "platform": "workbuddy",
        "group": GROUP_DAILY,
        "title": "派猫猫旅行",
        "subtitle": "派出猫猫，到点回来领奖励",
        "description": "按服务端状态机自动处理：已到达就领奖，空闲就派出，旅行中等待。",
        "rewardHint": "到达奖励由上游 reward_credit 决定",
        "automatable": True,
        "theme": "violet",
        "icon": "plane",
        "tags": ("每日", "自动"),
        "rules": (
            "派出猫猫后进入旅行状态",
            "到达后领取奖励，旅行中等待",
            "旅行中不会重复派出",
        ),
        "rewards": ("到达奖励", "旅行里程奖励"),
        "periodNote": "每日可派",
        "officialUrl": f"{WORKBUDDY_WEB_ORIGIN}/",
        "officialLabel": "WorkBuddy 官网",
        "featured": True,
        "sortOrder": 40,
    },
    {
        "key": "workbuddy.black_cat",
        "platform": "workbuddy",
        "group": GROUP_DAILY,
        "title": "夜猫子",
        "subtitle": "23:00–08:00 使用 GLM-5.2 夜间对话",
        "description": (
            "夜间窗口内补一次 GLM-5.2 夜间对话事件并领奖；窗口外只展示进度，不做动作。"
        ),
        "rewardHint": "达成 3 次后领取任务奖励",
        "automatable": True,
        "theme": "teal",
        "icon": "moon",
        "tags": ("每日", "夜间", "自动"),
        "rules": (
            "活动窗口为 23:00–08:00",
            "使用 GLM-5.2 完成夜间对话",
            "累计 3 次后领取任务奖励",
        ),
        "rewards": ("夜间任务奖励",),
        "periodNote": "23:00–08:00",
        "officialUrl": f"{WORKBUDDY_WEB_ORIGIN}/",
        "officialLabel": "WorkBuddy 官网",
        "featured": False,
        "sortOrder": 50,
    },
    {
        "key": "workbuddy.school",
        "platform": "workbuddy",
        "group": GROUP_SEASONAL,
        "title": "开学季活动",
        "subtitle": "小程序任务 + 转盘",
        "description": (
            "对话 3 次 / 专家对话 / 分享 / 桌面对话四项可纯接口完成并领奖，"
            "收尾抽空转盘次数；学生认证需手动。"
        ),
        "rewardHint": "每项任务奖励以活动页为准",
        "automatable": True,
        "theme": "rose",
        "icon": "graduation-cap",
        "tags": ("活动期", "自动+手动"),
        "rules": (
            "完成对话、专家对话、分享、桌面对话等任务",
            "任务完成后领取对应奖励并抽掉转盘次数",
            "学生认证需在客户端手动完成",
        ),
        "rewards": ("任务奖励", "转盘抽奖"),
        "periodNote": "活动期内",
        # 开学季在 2026-09-30 结束。上游不再发奖励，目录里保留条目只是为了
        # 「历史运行记录还能显示标题」，不再出现在目录/活动页、也不允许执行。
        "endsAt": "2026-09-30",
        "officialUrl": f"{WORKBUDDY_WEB_ORIGIN}/",
        "officialLabel": "WorkBuddy 官网",
        "featured": True,
        "sortOrder": 60,
    },
    {
        "key": "trae.daily_checkin",
        "platform": "trae",
        "group": GROUP_DAILY,
        "title": "每日签到",
        "subtitle": "每日一次，签到得积分",
        "description": "Trae 每日签到领积分；可纯接口代签。",
        "rewardHint": "每日积分由上游 credits 字段决定",
        "automatable": True,
        "theme": "blue",
        "icon": "sun",
        "tags": ("每日", "自动"),
        "rules": (
            "每天可签到一次",
            "签到积分由上游 credits 字段决定",
            "单设备每日只能被一个账号签到",
        ),
        "rewards": ("每日积分",),
        "periodNote": "长期开放",
        "officialUrl": "https://www.trae.cn/",
        "officialLabel": "Trae 官网",
        "featured": False,
        "sortOrder": 70,
    },
)

#: 官方活动目录版本。每次增删活动或修改规则/入口时同步更新，页面会展示。
OFFICIAL_CATALOG_VERSION = "2026.10.2"
OFFICIAL_CATALOG_UPDATED_AT = "2026-10-26"
OFFICIAL_DISCLAIMER = (
    "活动信息整理自官方客户端/官网公开内容。本工具与平台无隶属、合作或授权关系；"
    "活动规则、奖励与时间以官方页面为准。"
)

ACTIVITY_KEYS = frozenset(entry["key"] for entry in ACTIVITY_CATALOG)


def _today() -> date:
    """活动日期用**本地日期**判断：活动是按用户时区的自然日发放的。"""
    return datetime.now(UTC).astimezone().date()


def _parse_day(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def is_available(entry: dict[str, Any], today: date | None = None) -> bool:
    """条目当前是否还在活动期内。

    没有 ``endsAt`` = 长期开放；有 ``endsAt`` 且今天**晚于**结束日即下架
    （结束日当天仍可运行）。无法解析日期按「开放」处理：宁可多显示一天，
    也不要因为目录写错日期把一个仍在进行的活动藏起来。
    """
    ends = _parse_day(entry.get("endsAt"))
    if ends is None:
        return True
    return (today or _today()) <= ends


def available_catalog(today: date | None = None) -> tuple[dict[str, Any], ...]:
    """当前可用的目录条目（页面骨架、计划任务白名单都用它）。"""
    return tuple(entry for entry in ACTIVITY_CATALOG if is_available(entry, today))


def available_keys(today: date | None = None) -> frozenset[str]:
    return frozenset(entry["key"] for entry in available_catalog(today))


def drop_ended(
    accounts: list[AccountActivities], today: date | None = None
) -> list[AccountActivities]:
    """把已下架活动从检测结果里摘掉。

    上游可能还在返回旧活动的数据；继续展示会让「活动页还有开学季」这件事
    永远修不掉。历史执行流水不受影响（它读的是库里的记录，不是这里）。
    """
    kept = available_keys(today)
    ended = ACTIVITY_KEYS - kept
    if not ended:
        return accounts
    return [
        replace(
            account,
            items=tuple(item for item in account.items if item.key not in ended),
        )
        for account in accounts
    ]

#: 卡片主题与图标都走白名单，前端只认这些值，避免目录里写错字符串后静默降级。
CAMPAIGN_THEMES = frozenset({"green", "blue", "amber", "violet", "teal", "rose"})
CAMPAIGN_ICONS = frozenset(
    {"calendar-check", "sprout", "flame", "plane", "moon", "graduation-cap", "sun"}
)
OFFICIAL_URL_HOSTS = ("workbuddy.cn", "trae.cn")


def validate_official_catalog() -> None:
    """校验官方活动目录字段与官方入口白名单（测试用，不在 import 时抛错）。"""
    seen: set[str] = set()
    for entry in ACTIVITY_CATALOG:
        key = entry["key"]
        if key in seen:
            raise ValueError(f"活动目录 key 重复：{key}")
        seen.add(key)
        for field in (
            "theme",
            "icon",
            "tags",
            "rules",
            "rewards",
            "periodNote",
            "officialUrl",
            "officialLabel",
            "sortOrder",
        ):
            if not entry.get(field):
                raise ValueError(f"活动目录缺少字段 {field}：{key}")
        if entry["theme"] not in CAMPAIGN_THEMES:
            raise ValueError(f"活动主题非法：{key} -> {entry['theme']}")
        if entry["icon"] not in CAMPAIGN_ICONS:
            raise ValueError(f"活动图标非法：{key} -> {entry['icon']}")
        starts = entry.get("startsAt")
        ends = entry.get("endsAt")
        for label, value in (("startsAt", starts), ("endsAt", ends)):
            if value is not None and _parse_day(value) is None:
                raise ValueError(f"活动日期格式非法（{label}，需 YYYY-MM-DD）：{key} -> {value}")
        if starts is not None and ends is not None and _parse_day(starts) > _parse_day(ends):
            raise ValueError(f"活动开始日晚于结束日：{key} -> {starts} > {ends}")
        parsed = urlsplit(entry["officialUrl"])
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not host:
            raise ValueError(f"活动官方入口必须是 https：{key} -> {entry['officialUrl']}")
        if not any(
            host == allowed or host.endswith(f".{allowed}") for allowed in OFFICIAL_URL_HOSTS
        ):
            raise ValueError(f"活动官方入口域名不在白名单：{key} -> {host}")


# ---------- 数据结构 ----------


@dataclass(frozen=True, slots=True)
class ActivityItem:
    """一个账号上的一项活动。"""

    key: str
    platform: str
    group: str
    title: str
    subtitle: str = ""
    state: str = STATE_UNAVAILABLE
    automatable: bool = False
    manual_reason: str = ""
    progress_current: int | None = None
    progress_target: int | None = None
    progress_text: str = ""
    reward: float | None = None
    reward_text: str = ""
    period_start: str | None = None
    period_end: str | None = None
    note: str = ""
    #: 手动项的客户端深链（来自上游 `jump_url`）：工具做不了，但能一键把用户送到页面。
    jump_url: str = ""
    #: 仍需手动完成的具体条目（标题 / 说明 / 深链），逐条摊给用户，不藏起来。
    manual_tasks: tuple[dict[str, str], ...] = ()

    @property
    def actionable(self) -> bool:
        """能立刻做掉吗（自动 + 待办）。"""
        return self.automatable and self.state == STATE_TODO

    def payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "platform": self.platform,
            "group": self.group,
            "title": self.title,
            "subtitle": self.subtitle,
            "state": self.state,
            "automatable": self.automatable,
            "manualReason": self.manual_reason,
            "progressCurrent": self.progress_current,
            "progressTarget": self.progress_target,
            "progressText": self.progress_text,
            "reward": self.reward,
            "rewardText": self.reward_text,
            "periodStart": self.period_start,
            "periodEnd": self.period_end,
            "note": self.note,
            "jumpUrl": self.jump_url,
            "manualTasks": [dict(item) for item in self.manual_tasks],
        }


@dataclass(frozen=True, slots=True)
class AccountActivities:
    """一个账号的全部活动。``detectable=False`` 表示查不了（不是没有活动）。"""

    account_id: str
    display_name: str
    platform: str
    scope: str
    detectable: bool
    items: tuple[ActivityItem, ...] = ()
    error: str | None = None
    updated_at: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "accountId": self.account_id,
            "displayName": self.display_name,
            "platform": self.platform,
            "scope": self.scope,
            "detectable": self.detectable,
            "error": self.error,
            "updatedAt": self.updated_at,
            "activities": [item.payload() for item in self.items],
        }


@dataclass(frozen=True, slots=True)
class RunResult:
    """单账号单活动的执行结果。"""

    account_id: str
    display_name: str
    platform: str
    key: str
    title: str
    status: str
    message: str
    reward: float | None = None
    #: 失败是否值得再试一次：网络异常（状态码 0）/ 上游 5xx / Trae 9074 才算；
    #: 401/403/400 与业务拒绝重试也只会再失败一次。
    retryable: bool = False
    #: 这是第几次尝试的结果（1 = 首轮，2 = 失败重试轮）。
    attempt: int = 1

    def payload(self) -> dict[str, Any]:
        return {
            "accountId": self.account_id,
            "displayName": self.display_name,
            "platform": self.platform,
            "key": self.key,
            "title": self.title,
            "status": self.status,
            "message": self.message,
            "reward": self.reward,
            "retryable": self.retryable,
            "attempt": self.attempt,
        }


class ActivityBusyError(RuntimeError):
    """上一次一键执行还没结束。"""


# ---------- 小工具 ----------


def _now_iso() -> str:
    return datetime.now(UTC).astimezone().isoformat(timespec="seconds")


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _int(value: Any) -> int | None:
    number = _num(value)
    return int(number) if number is not None else None


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _abs_jump_url(value: Any) -> str:
    """上游 `jump_url` 可能是相对路径或客户端自定义协议，尽量补成可直接点的地址。"""
    url = _text(value)
    if not url:
        return ""
    if url.startswith("/"):
        return f"{WORKBUDDY_WEB_ORIGIN}{url}"
    if url.startswith(("http://", "https://", "workbuddy://")):
        return url
    return ""


def _message_of(body: Any) -> str:
    """上游的错误文案可能在 msg / message 里，两个都看。"""
    if not isinstance(body, dict):
        return ""
    for key in ("message", "msg"):
        text = _text(body.get(key))
        if text:
            return text
    return ""


def _unauthorized(status_code: int, message: str) -> bool:
    """令牌失效的判定：要看状态码，也要看业务码/文案（上游两种都出现过）。"""
    if status_code in (401, 403):
        return True
    lowered = message.lower()
    return any(token in lowered for token in ("unauthorized", "token", "登录", "失效", "过期"))


def _retryable_status(status_code: int) -> bool:
    """值得再试一次的错误：网络层失败（0）与上游 5xx。

    4xx 是「请求本身不被接受」，业务拒绝是「上游明确说不」，两类重试都只会再失败一次。
    """
    return status_code == 0 or status_code >= 500


def _call(
    client: httpx.Client,
    method: str,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any] | None = None,
) -> tuple[int, Any, str]:
    """发一次请求，返回 ``(状态码, JSON 对象或 None, 错误文案)``。状态码 0 = 网络层失败。"""
    try:
        response = client.request(method, url, headers=headers, json=body)
    except httpx.HTTPError as exc:
        return 0, None, f"网络请求失败：{exc}"
    try:
        parsed = response.json()
    except ValueError:
        return response.status_code, None, f"响应不是 JSON（HTTP {response.status_code}）"
    if response.status_code >= 400 and not isinstance(parsed, dict):
        return response.status_code, None, f"HTTP {response.status_code}"
    return response.status_code, parsed, ""


def _failed_item(
    *, key: str, platform: str, group: str, title: str, reason: str, subtitle: str = ""
) -> ActivityItem:
    """检测失败时仍然出现在列表里 —— 消失比报错更让人困惑。"""
    return ActivityItem(
        key=key,
        platform=platform,
        group=group,
        title=title,
        subtitle=subtitle,
        state=STATE_UNAVAILABLE,
        note=reason,
    )


def _extra_item(
    *,
    key: str,
    title: str,
    subtitle: str,
    group: str,
    extra: ExtraActivity,
) -> ActivityItem:
    """把 wb_extras 的检测结论翻译成活动项（key/标题仍由本模块统一管理）。"""
    return ActivityItem(
        key=key,
        platform="workbuddy",
        group=group,
        title=title,
        subtitle=subtitle,
        state=extra.state,
        automatable=extra.automatable,
        progress_current=extra.progress_current,
        progress_target=extra.progress_target,
        progress_text=extra.progress_text,
        reward=extra.reward,
        reward_text=extra.reward_text,
        note=extra.note,
    )


# ---------- WorkBuddy 解析 ----------


def parse_wb_checkin(data: dict[str, Any]) -> ActivityItem:
    """`checkin-activity-status` 的 `data` 段 → 每日签到这一项。"""
    active = bool(data.get("active"))
    done = bool(data.get("today_checked_in"))
    daily = _num(data.get("daily_credit"))
    today = _num(data.get("today_credit"))
    streak = _int(data.get("streak_days")) or 0
    week_days = _int(data.get("week_checkin_days"))
    theme = _text(data.get("theme_name"))
    season = _int(data.get("season"))

    if done:
        state = STATE_DONE
    elif active:
        state = STATE_TODO
    else:
        state = STATE_NOT_STARTED

    # 「已领」和「可得」必须分开说：把 daily 当成已领就是谎报收益。
    if done and today is not None:
        reward_text = f"今日 +{_fmt(today)}"
    elif daily is not None:
        reward_text = f"可得 {_fmt(daily)}"
    else:
        reward_text = ""

    notes: list[str] = []
    if streak:
        notes.append(f"连签 {streak} 天")
    if week_days is not None:
        notes.append(f"本周 {week_days}/7")
    if not active:
        notes.append("当前没有进行中的签到活动")
    elif data.get("is_streak_day") and data.get("streak_bonus_credit"):
        notes.append(f"今天是连签奖励日 +{_fmt(_num(data.get('streak_bonus_credit')))}")

    subtitle = " · ".join(part for part in (theme, f"第 {season} 期" if season else "") if part)

    return ActivityItem(
        key="workbuddy.daily_checkin",
        platform="workbuddy",
        group=GROUP_DAILY,
        title="每日签到",
        subtitle=subtitle,
        state=state,
        automatable=True,
        reward=today if done else daily,
        reward_text=reward_text,
        period_start=_text(data.get("start_time")) or None,
        period_end=_text(data.get("end_time")) or None,
        note=" · ".join(notes),
    )


def parse_wb_tasks(tasks: list[Any]) -> ActivityItem:
    """成长任务列表 → 一项汇总（可自动执行有动作的任务，其余标「需手动」）。"""
    claimed = 0
    pending: list[str] = []
    potential = 0.0
    automatable_codes: set[str] = set()
    #: 手动族里第一条的客户端深链 —— 让「需手动」也能一键跳到对应页面。
    manual_jump = ""
    manual_tasks: list[dict[str, str]] = []
    for task in tasks:
        if not isinstance(task, dict):
            continue
        status = _text(task.get("accept_status"))
        code = _text(task.get("task_code"))
        if status == "claimed":
            claimed += 1
            continue
        pending.append(_text(task.get("title")) or code)
        potential += _num(task.get("reward_credit")) or 0.0
        # 有动作表覆盖的任务码 → 可自动；其余（expert/skill/小程序族）仍需手动。
        if code and code in _AUTOMATABLE_TASK_CODES:
            automatable_codes.add(code)
        else:
            jump = _abs_jump_url(task.get("jump_url"))
            if jump and not manual_jump:
                manual_jump = jump
            if code:
                manual_tasks.append(
                    {
                        "code": code,
                        "title": _text(task.get("title")) or code,
                        "desc": _text(task.get("task_desc")) or _text(task.get("description")),
                        "jumpUrl": jump,
                    }
                )

    total = claimed + len(pending)
    if total == 0:
        return _failed_item(
            key="workbuddy.growth_tasks",
            platform="workbuddy",
            group=GROUP_SEASONAL,
            title="成长任务",
            reason="上游没有返回任何任务",
        )

    # 只要还有「可自动」的待办任务，就允许一键执行；手动族单独标注。
    automatable = bool(automatable_codes)

    # 状态：有可自动的待办 → 仍算「待办」（一键执行按钮据此点亮）；
    # 全部待办都是手动族 → 进行中/待办仅作展示。
    if not pending:
        state = STATE_DONE
    elif automatable:
        state = STATE_TODO
    elif claimed:
        state = STATE_IN_PROGRESS
    else:
        state = STATE_TODO

    note = ""
    if pending:
        shown = "、".join(pending[:3])
        more = f" 等 {len(pending)} 项" if len(pending) > 3 else ""
        note = f"还差：{shown}{more}"

    manual_reason = ""
    if pending and not automatable:
        manual_reason = MANUAL_REASON_TASKS

    return ActivityItem(
        key="workbuddy.growth_tasks",
        platform="workbuddy",
        group=GROUP_SEASONAL,
        title="成长任务",
        state=state,
        automatable=automatable,
        manual_reason=manual_reason,
        progress_current=claimed,
        progress_target=total,
        progress_text=f"{claimed}/{total} 已领取",
        reward=round(potential, 2) if potential else None,
        reward_text=f"还剩 {_fmt(potential)} 可分" if potential else "",
        note=note,
        jump_url=manual_jump,
        manual_tasks=tuple(manual_tasks),
    )


def parse_wb_streak(data: dict[str, Any], *, chances: int | None = None) -> ActivityItem:
    """连签里程（含补签卡/兑换/抽奖）→ 一项汇总。"""
    streak = data.get("streak") if isinstance(data.get("streak"), dict) else {}
    days = _int(streak.get("days")) or 0
    month_total = _int(streak.get("month_total_days"))
    next_tier = _text(streak.get("next_tier"))
    remaining = _int(streak.get("next_tier_remaining"))

    cards = data.get("makeup_cards") if isinstance(data.get("makeup_cards"), dict) else {}
    card_balance = _int(cards.get("balance")) or 0
    card_max = _int(cards.get("max"))

    redemption = (
        data.get("redemption_status")
        if isinstance(data.get("redemption_status"), dict)
        else {}
    )
    tiers = redemption.get("tiers") if isinstance(redemption.get("tiers"), list) else []
    claimable_tiers: list[str] = []
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        name = _text(tier.get("tier"))
        status = _text(redemption.get(f"tier_{name}_status"))
        if status and status not in ("locked", "claimed", "done"):
            claimable_tiers.append(name)

    # 有东西可拿 → 待处理；否则只要连签在推进就算进行中。
    if claimable_tiers or card_balance > 0 or (chances or 0) > 0:
        state = STATE_TODO
    elif days > 0:
        state = STATE_IN_PROGRESS
    else:
        state = STATE_NOT_STARTED

    notes = [f"连签 {days} 天"]
    if next_tier and remaining:
        notes.append(f"距 {next_tier} 档还差 {remaining} 天")
    if month_total is not None:
        notes.append(f"本月 {month_total} 天")
    notes.append(f"补签卡 {card_balance}{f'/{card_max}' if card_max else ''} 张")
    if chances is not None:
        notes.append(f"抽奖 {chances} 次")
    if claimable_tiers:
        notes.append(f"可领档位：{'、'.join(claimable_tiers)}")

    reward = None
    reward_text = ""
    for tier in tiers:
        if isinstance(tier, dict) and _text(tier.get("tier")) == next_tier:
            credit = _num(tier.get("credit"))
            if credit is not None and credit > 0:
                reward = credit
                reward_text = f"{next_tier} 档 +{_fmt(credit)}"
            break

    label = "连签里程"
    # 有可领档位 / 补签卡 / 抽奖次数 → 本工具能代做；否则没有动作可执行。
    automatable = bool(claimable_tiers or card_balance > 0 or (chances or 0) > 0)
    return ActivityItem(
        key="workbuddy.streak",
        platform="workbuddy",
        group=GROUP_SEASONAL,
        title=label,
        state=state,
        automatable=automatable,
        manual_reason="" if automatable else MANUAL_REASON_STREAK,
        progress_current=days,
        progress_target=_tier_days(next_tier) if next_tier else None,
        progress_text=(
            f"{days}/{_tier_days(next_tier)} 天" if next_tier and _tier_days(next_tier) else ""
        ),
        reward=reward,
        reward_text=reward_text,
        note=" · ".join(notes),
    )


def _tier_days(tier: str) -> int | None:
    """`"7d"` / `"14d"` / `"28d"` → 天数。"""
    digits = "".join(char for char in tier if char.isdigit())
    return int(digits) if digits else None


# ---------- Trae 解析 ----------


def parse_trae_checkin(body: dict[str, Any]) -> ActivityItem:
    """`checkin_credits/status` 响应 → 每日签到这一项。"""
    done = bool(body.get("checked_in") or body.get("did_checked_in"))
    enabled = body.get("enable")
    enable = True if enabled is None else bool(enabled)
    credits = _num(body.get("credits"))
    extra = _num(body.get("extra_credits"))

    if done:
        state = STATE_DONE
    elif enable:
        state = STATE_TODO
    else:
        state = STATE_NOT_STARTED

    reward_text = f"每日 {_fmt(credits)} 积分" if credits is not None else ""
    notes: list[str] = []
    if extra:
        notes.append(f"额外奖励 {_fmt(extra)}")
    if not enable:
        notes.append("当前没有进行中的签到活动")

    return ActivityItem(
        key="trae.daily_checkin",
        platform="trae",
        group=GROUP_DAILY,
        title="每日签到",
        state=state,
        automatable=True,
        reward=credits,
        reward_text=reward_text,
        note=" · ".join(notes),
    )


# ---------- 服务 ----------


class ActivitiesService:
    """检测每个账号的活动状态，并代做其中可以纯接口完成的部分。"""

    def __init__(
        self,
        repository: Any,
        *,
        ttl: float = CACHE_TTL_SECONDS,
        growth_runner: GrowthTaskRunner | None = None,
    ) -> None:
        self._repo = repository
        self._ttl = ttl
        self._growth_runner = growth_runner
        self._cache: dict[str, tuple[float, AccountActivities]] = {}
        self._lock = threading.Lock()
        self._run_lock = threading.Lock()
        # trust_env=False：两端都是国内直连端点，不该走本机环境代理。
        self._client = httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False)

    # ----- 检测 -----

    def detect(
        self, accounts: list[Account], *, force: bool = False
    ) -> list[AccountActivities]:
        """按账号并发检测（缓存命中直接返回）。

        之前是串行 + 每号 0.8s 间隔，二十个号要等十几秒；并发度压到 4，
        既能把等待压到 1/4 左右，也不至于让上游把我们当爬虫。
        """
        now = time.time()
        order = {account.id: index for index, account in enumerate(accounts)}
        result: list[AccountActivities] = []
        pending: list[Account] = []
        for account in accounts:
            with self._lock:
                cached = self._cache.get(account.id)
            if cached and not force and now - cached[0] < self._ttl:
                result.append(cached[1])
            else:
                pending.append(account)

        if pending:
            with ThreadPoolExecutor(max_workers=DETECT_WORKERS) as pool:
                futures = {
                    account.id: pool.submit(self._detect_one, account) for account in pending
                }
                for account_id, future in futures.items():
                    detected = future.result()
                    with self._lock:
                        self._cache[account_id] = (time.time(), detected)
                    result.append(detected)

        result.sort(key=lambda item: order.get(item.account_id, 0))
        return result

    def invalidate(self, account_id: str | None = None) -> None:
        with self._lock:
            if account_id is None:
                self._cache.clear()
            else:
                self._cache.pop(account_id, None)

    def _detect_one(self, account: Account) -> AccountActivities:
        try:
            platform = get_scope(account.scope).platform
        except UnknownScope:
            return self._blank(account, "workbuddy", "未知平台")
        if account.credential_state is CredentialState.RELOGIN_REQUIRED:
            # 预检：已确认失效的号不再打上游 —— 只会再拿一次 401，还会加重风控画像。
            return self._blank(account, platform, "登录已失效，请重新扫码登录")
        try:
            credential = self._repo.current_credential(account.id)
        except AccountNotFound:
            return self._blank(account, platform, "没有可用凭据")

        if platform == "workbuddy":
            return self._detect_workbuddy(account, credential)
        if platform == "trae":
            return self._detect_trae(account, credential)
        return self._blank(account, platform, "不支持的平台")

    def _blank(self, account: Account, platform: str, error: str) -> AccountActivities:
        return AccountActivities(
            account_id=account.id,
            display_name=account.display_name,
            platform=platform,
            scope=account.scope,
            detectable=False,
            error=error,
            updated_at=_now_iso(),
        )

    def _detect_workbuddy(
        self, account: Account, credential: dict[str, Any]
    ) -> AccountActivities:
        try:
            token, uid, domain = workbuddy_credentials(credential)
        except CredentialError as exc:
            return self._blank(account, "workbuddy", str(exc))

        origin = workbuddy_origin(domain)
        headers = workbuddy_headers(token, uid, domain)

        # wb_extras 只拿到「方法/URL → 结果」这一层，复用本模块的 `_call`
        # （测试里 patch 的也是它，额外活动因此不会打出真实网络）。
        def extra_call(
            method: str, url: str, extra_headers: dict[str, str], body: Any = None
        ) -> tuple[int, Any]:
            status_code, payload, _ = _call(self._client, method, url, extra_headers, body)
            return status_code, payload

        items: list[ActivityItem] = []
        errors: list[str] = []

        # 1) 签到（唯一可自动执行的一项）
        status_code, body, error = _call(
            self._client, "POST", f"{origin}{WB_CHECKIN_STATUS_PATH}", headers, {}
        )
        if isinstance(body, dict):
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            if body.get("code") in (0, 200) and data:
                items.append(parse_wb_checkin(data))
            else:
                message = _message_of(body) or f"HTTP {status_code}"
                self._note_auth(account, status_code, body, message)
                items.append(
                    _failed_item(
                        key="workbuddy.daily_checkin",
                        platform="workbuddy",
                        group=GROUP_DAILY,
                        title="每日签到",
                        reason=self._explain(status_code, message),
                    )
                )
                errors.append(message)
        else:
            self._note_auth(account, status_code, body, error)
            items.append(
                _failed_item(
                    key="workbuddy.daily_checkin",
                    platform="workbuddy",
                    group=GROUP_DAILY,
                    title="每日签到",
                    reason=self._explain(status_code, error),
                )
            )
            errors.append(error)

        # 2) 成长任务
        status_code, body, error = _call(
            self._client, "GET", f"{origin}{WB_TASKS_PATH}", headers
        )
        tasks = None
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            raw_tasks = body["data"].get("tasks")
            if isinstance(raw_tasks, list):
                tasks = raw_tasks
        if tasks is not None:
            items.append(parse_wb_tasks(tasks))
            cat = detect_black_cat(tasks)
            if cat is not None:
                items.append(
                    _extra_item(
                        key="workbuddy.black_cat",
                        title="夜猫子",
                        subtitle="23:00–08:00 使用 GLM-5.2",
                        group=GROUP_DAILY,
                        extra=cat,
                    )
                )
        else:
            message = _message_of(body) or error or f"HTTP {status_code}"
            self._note_auth(account, status_code, body, message)
            items.append(
                _failed_item(
                    key="workbuddy.growth_tasks",
                    platform="workbuddy",
                    group=GROUP_SEASONAL,
                    title="成长任务",
                    reason=self._explain(status_code, message),
                )
            )
            errors.append(message)

        # 3) 连签里程 + 抽奖次数
        streak_data: dict[str, Any] | None = None
        status_code, body, error = _call(
            self._client, "GET", f"{origin}{WB_STREAK_PATH}", headers
        )
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            streak_data = body["data"]
        chances: int | None = None
        status_code, body, error = _call(
            self._client, "GET", f"{origin}{WB_LOTTERY_PATH}", headers
        )
        if isinstance(body, dict) and isinstance(body.get("data"), dict):
            chances = _int(body["data"].get("balance"))
        if streak_data is not None:
            items.append(parse_wb_streak(streak_data, chances=chances))
        else:
            message = _message_of(body) or error or f"HTTP {status_code}"
            self._note_auth(account, status_code, body, message)
            items.append(
                _failed_item(
                    key="workbuddy.streak",
                    platform="workbuddy",
                    group=GROUP_SEASONAL,
                    title="连签里程",
                    reason=self._explain(status_code, message),
                )
            )
            errors.append(message)

        # 4) 派猫猫旅行 / 开学季：各自内部把网络异常翻成「不可用」，不拖垮整张卡。
        items.append(
            _extra_item(
                key="workbuddy.travel",
                title="派猫猫旅行",
                subtitle="派出猫猫，到点领奖励",
                group=GROUP_DAILY,
                extra=detect_travel(extra_call, origin, headers),
            )
        )
        items.append(
            _extra_item(
                key="workbuddy.school",
                title="开学季活动",
                subtitle="小程序任务 + 转盘",
                group=GROUP_SEASONAL,
                extra=detect_school(
                    extra_call, origin, school_headers(token, uid, domain, origin)
                ),
            )
        )

        return AccountActivities(
            account_id=account.id,
            display_name=account.display_name,
            platform="workbuddy",
            scope=account.scope,
            detectable=len(errors) < len(items),
            items=tuple(items),
            error=errors[0] if errors and len(errors) == len(items) else None,
            updated_at=_now_iso(),
        )

    def _detect_trae(self, account: Account, credential: dict[str, Any]) -> AccountActivities:
        try:
            jwt, device_id, host = trae_credentials(credential)
        except CredentialError as exc:
            return self._blank(account, "trae", str(exc))

        base = (host or TRAE_ACCOUNT_BASE).rstrip("/")
        headers = trae_headers(jwt, device_id)
        status_code, body, error = _call(
            self._client, "POST", f"{base}{TRAE_CHECKIN_STATUS_PATH}", headers, {}
        )
        if isinstance(body, dict) and body.get("code") in (0, 200):
            item = parse_trae_checkin(body)
            return AccountActivities(
                account_id=account.id,
                display_name=account.display_name,
                platform="trae",
                scope=account.scope,
                detectable=True,
                items=(item,),
                updated_at=_now_iso(),
            )

        message = _message_of(body) or error or f"HTTP {status_code}"
        self._note_auth(account, status_code, body, message)
        return AccountActivities(
            account_id=account.id,
            display_name=account.display_name,
            platform="trae",
            scope=account.scope,
            detectable=False,
            items=(
                _failed_item(
                    key="trae.daily_checkin",
                    platform="trae",
                    group=GROUP_DAILY,
                    title="每日签到",
                    reason=self._explain(status_code, message),
                ),
            ),
            error=message,
            updated_at=_now_iso(),
        )

    @staticmethod
    def _explain(status_code: int, message: str) -> str:
        """把上游错误翻成用户能看懂的一句话。"""
        if _unauthorized(status_code, message):
            return "登录已失效，请重新扫码登录或重新导入"
        if status_code == 0:
            return message or "网络请求失败"
        return f"检测失败：{message}" if message else f"检测失败（HTTP {status_code}）"

    # ----- 执行 -----

    def run(
        self,
        accounts: list[Account],
        *,
        include_growth: bool = True,
        retry_failed: bool = False,
        keys: set[str] | None = None,
    ) -> dict[str, Any]:
        """对每个账号执行**可自动且未完成**的活动；已完成的跳过并说明原因。

        ``include_growth=False`` 时只跑每日签到，不跑成长任务 —— 计划任务的
        「签到」与「全部活动」两个档位据此区分。

        ``keys`` 为活动 key 白名单：``None`` 表示全量（保持原有语义），
        传集合时只跑集合内的活动，供「只执行本活动」使用。

        ``retry_failed=True`` 时，首轮结束后只把**可重试的失败账号**再跑一遍
        （网络异常 / 5xx / Trae 9074），401/403/400 与业务拒绝不重试。
        重试轮的结论会顶替首轮同账号同活动的行，`results` 仍是「每个活动一行」。
        """
        if not self._run_lock.acquire(blocking=False):
            raise ActivityBusyError("上一次一键执行还没结束，请稍等")
        # 已结束的活动一律不执行：上游不再发奖励，代做只会得到一串业务失败。
        # 这里统一收口，计划任务、全量执行、单活动执行都受益。
        keys = available_keys() if keys is None else keys & available_keys()
        retried = 0
        retry_ok = 0
        try:
            results = self._run_accounts(
                accounts, include_growth=include_growth, keys=keys
            )
            if retry_failed:
                retry_targets = _retry_targets(accounts, results)
                if retry_targets:
                    time.sleep(RETRY_BACKOFF_SECONDS)
                    retried = len(retry_targets)
                    second = [
                        replace(item, attempt=2)
                        for item in self._run_accounts(
                            retry_targets, include_growth=include_growth, keys=keys
                        )
                    ]
                    retry_ok = sum(1 for item in second if item.status == RESULT_OK)
                    results = _merge_attempt(results, second)
        finally:
            self._run_lock.release()
            # 状态已变，缓存必须失效，否则前端刷新看到的还是旧结论。
            self.invalidate()

        self._record_runs(results)
        summary = _summarize_results(results)
        summary["retried"] = retried
        summary["retryOk"] = retry_ok
        return {
            "results": [item.payload() for item in results],
            "summary": summary,
        }

    def _run_accounts(
        self,
        accounts: list[Account],
        *,
        include_growth: bool = True,
        keys: set[str] | None = None,
    ) -> list[RunResult]:
        results: list[RunResult] = []
        for index, account in enumerate(accounts):
            if index:
                # 连续打上游容易被风控；账号之间留一点间隔。
                time.sleep(ACCOUNT_GAP_SECONDS)
            results.extend(
                self._run_one(account, include_growth=include_growth, keys=keys)
            )
        return results

    def run_travel_stage(self, accounts: list[Account], *, stage: str) -> dict[str, Any]:
        """只跑派猫猫旅行的一个阶段（depart / claim），给计划任务用。

        为什么不复用 ``run()``：计划任务一天要跑两次（早上派、下午领），走整包会把
        签到、成长任务、其它活动一起再打一遍上游 —— 又慢又容易触发风控。
        这里只发「读状态 + 一个写动作」两次请求。

        非 WorkBuddy 账号直接跳过：派猫猫是 WorkBuddy 专属活动，不是策略省略。
        """
        if not self._run_lock.acquire(blocking=False):
            raise ActivityBusyError("上一次一键执行还没结束，请稍等")
        results: list[RunResult] = []
        try:
            for index, account in enumerate(accounts):
                if index:
                    time.sleep(ACCOUNT_GAP_SECONDS)
                results.extend(self._run_travel_one(account, stage=stage))
        finally:
            self._run_lock.release()
            self.invalidate()
        self._record_runs(results)
        return {
            "results": [item.payload() for item in results],
            "summary": _summarize_results(results),
        }

    def _run_travel_one(self, account: Account, *, stage: str) -> list[RunResult]:
        try:
            platform = get_scope(account.scope).platform
        except UnknownScope:
            return []
        if platform != "workbuddy":
            return []
        try:
            credential = self._repo.current_credential(account.id)
        except AccountNotFound:
            return []
        try:
            token, uid, domain = workbuddy_credentials(credential)
        except CredentialError as exc:
            return [
                self._result(
                    account, "workbuddy.travel", "派猫猫旅行", RESULT_SKIPPED, str(exc)
                )
            ]
        origin = workbuddy_origin(domain)
        headers = workbuddy_headers(token, uid, domain)

        def extra_call(
            method: str, url: str, extra_headers: dict[str, str], body: Any = None
        ) -> tuple[int, Any]:
            status_code, payload, _ = _call(self._client, method, url, extra_headers, body)
            return status_code, payload

        try:
            step = run_travel_stage(extra_call, origin, headers, stage)
        except Exception as exc:  # noqa: BLE001 - 单项异常不炸整体
            logger.exception("派猫猫旅行异常（%s）：%s", stage, exc)
            return [
                self._result(
                    account, "workbuddy.travel", "派猫猫旅行", RESULT_FAILED, str(exc)
                )
            ]
        return [
            self._result(account, step.key, step.title, step.status, step.message, step.reward)
        ]

    def _run_one(
        self,
        account: Account,
        *,
        include_growth: bool = True,
        keys: set[str] | None = None,
    ) -> list[RunResult]:
        try:
            platform = get_scope(account.scope).platform
        except UnknownScope:
            return []
        if keys is not None and not any(
            key.startswith(f"{platform}.") for key in keys
        ):
            # 本活动白名单与这个账号的平台无关：不发任何请求。
            return []
        if account.credential_state is CredentialState.RELOGIN_REQUIRED:
            # 预检：登录态已失效时任何写请求都会失败，直接列为「待人工处理」。
            return [
                self._result(
                    account,
                    "account.login",
                    "登录状态",
                    RESULT_SKIPPED,
                    "登录已失效，请重新扫码登录后再执行活动",
                )
            ]
        try:
            credential = self._repo.current_credential(account.id)
        except AccountNotFound:
            return []
        if platform == "workbuddy":
            return self._run_workbuddy(
                account, credential, include_growth=include_growth, keys=keys
            )
        if platform == "trae":
            return self._run_trae(account, credential, keys=keys)
        return []

    def _run_workbuddy(
        self,
        account: Account,
        credential: dict[str, Any],
        *,
        include_growth: bool = True,
        keys: set[str] | None = None,
    ) -> list[RunResult]:
        key, title = "workbuddy.daily_checkin", "每日签到"

        def want(activity_key: str) -> bool:
            return keys is None or activity_key in keys

        try:
            token, uid, domain = workbuddy_credentials(credential)
        except CredentialError as exc:
            if not any(
                want(item)
                for item in (
                    key,
                    "workbuddy.growth_tasks",
                    "workbuddy.streak",
                    "workbuddy.travel",
                    "workbuddy.black_cat",
                    "workbuddy.school",
                )
            ):
                return []
            return [self._result(account, key, title, RESULT_SKIPPED, str(exc))]

        origin = workbuddy_origin(domain)
        headers = workbuddy_headers(token, uid, domain)

        def extra_call(
            method: str, url: str, extra_headers: dict[str, str], body: Any = None
        ) -> tuple[int, Any]:
            status_code, payload, _ = _call(self._client, method, url, extra_headers, body)
            return status_code, payload

        results: list[RunResult] = []

        # 先读状态：已签到的直接跳过，不产生任何写请求。
        # 只跑其它活动（例如派猫猫）时跳过这一步，避免多打一次上游。
        if want(key):
            status_code, body, error = _call(
                self._client, "POST", f"{origin}{WB_CHECKIN_STATUS_PATH}", headers, {}
            )
            payload = body.get("data") if isinstance(body, dict) else None
            data = payload if isinstance(payload, dict) else {}
            if not data or body.get("code") not in (0, 200):
                message = _message_of(body) or error or f"HTTP {status_code}"
                self._note_auth(account, status_code, body, message)
                results.append(
                    self._result(
                        account,
                        key,
                        title,
                        RESULT_FAILED,
                        self._explain(status_code, message),
                        retryable=_retryable_status(status_code),
                    )
                )
            else:
                item = parse_wb_checkin(data)
                if item.state == STATE_DONE:
                    results.append(
                        self._result(
                            account, key, title, RESULT_SKIPPED, item.reward_text or "今日已签到"
                        )
                    )
                elif item.state == STATE_NOT_STARTED:
                    results.append(
                        self._result(
                            account, key, title, RESULT_SKIPPED, "当前没有进行中的签到活动"
                        )
                    )
                else:
                    status_code, body, error = _call(
                        self._client, "POST", f"{origin}{WB_CHECKIN_PATH}", headers, {}
                    )
                    results.append(
                        self._classify_wb_claim(account, key, title, status_code, body, error)
                    )

        # 成长任务：有 runner、且本轮允许跑成长任务时才做；结果逐项转成 RunResult。
        if include_growth and self._growth_runner is not None and want(
            "workbuddy.growth_tasks"
        ):
            results.extend(self._run_growth_tasks(account, credential))

        # 额外活动：连签奖励链 / 派猫猫 / 夜猫子 / 开学季。
        # 这些动作都会写上游（领奖、派猫、做小程序任务），只在「全部活动」档位执行；
        # 计划任务的「签到」档位（include_growth=False）保持只签到、不产生其它写请求。
        # 单项失败只记该项，不影响其它项（上游经常单项 500）。
        extras: list[ExtraStep] = []
        if include_growth:
            if want("workbuddy.streak"):
                try:
                    extras.extend(run_streak_rewards(extra_call, origin, headers, uid))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("连签奖励链异常：%s", exc)
                    extras.append(
                        ExtraStep("workbuddy.streak", "连签里程", RESULT_FAILED, str(exc))
                    )
            if want("workbuddy.travel"):
                try:
                    extras.append(run_travel(extra_call, origin, headers))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("派猫猫旅行异常：%s", exc)
                    extras.append(
                        ExtraStep("workbuddy.travel", "派猫猫旅行", RESULT_FAILED, str(exc))
                    )
            if want("workbuddy.black_cat"):
                try:
                    extras.append(run_black_cat(extra_call, origin, headers, uid))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("夜猫子异常：%s", exc)
                    extras.append(
                        ExtraStep("workbuddy.black_cat", "夜猫子", RESULT_FAILED, str(exc))
                    )
            if want("workbuddy.school"):
                school = school_headers(token, uid, domain, origin)
                try:
                    extras.append(run_school(extra_call, origin, school, uid))
                except Exception as exc:  # noqa: BLE001
                    logger.exception("开学季异常：%s", exc)
                    extras.append(
                        ExtraStep("workbuddy.school", "开学季活动", RESULT_FAILED, str(exc))
                    )

        for step in extras:
            results.append(
                self._result(
                    account, step.key, step.title, step.status, step.message, step.reward
                )
            )

        return results

    def _run_growth_tasks(
        self, account: Account, credential: dict[str, Any]
    ) -> list[RunResult]:
        key, title = "workbuddy.growth_tasks", "成长任务"
        try:
            report = self._growth_runner.run_all(credential)
        except Exception as exc:  # noqa: BLE001 - 任何意外都归为失败项，不炸整个 run
            logger.exception("成长任务自动化异常：%s", exc)
            return [self._result(account, key, title, RESULT_FAILED, f"成长任务执行异常：{exc}")]

        if report.get("error"):
            return [
                self._result(account, key, title, RESULT_FAILED, f"成长任务：{report['error']}")
            ]

        outcomes = report.get("outcomes", [])
        if not outcomes:
            return [
                self._result(account, key, title, RESULT_SKIPPED, "没有可自动执行的成长任务")
            ]

        results: list[RunResult] = []
        for outcome in outcomes:
            code = outcome.get("code", "")
            task_title = outcome.get("title", "") or code
            status = outcome.get("status", RESULT_FAILED)
            message = outcome.get("message", "")
            credit = outcome.get("credit")
            results.append(
                self._result(
                    account,
                    f"workbuddy.growth_tasks.{code}",
                    task_title,
                    status,
                    message,
                    credit,
                )
            )
        return results

    def _classify_wb_claim(
        self,
        account: Account,
        key: str,
        title: str,
        status_code: int,
        body: Any,
        error: str,
    ) -> RunResult:
        code = body.get("code") if isinstance(body, dict) else None
        message = _message_of(body) or error or f"HTTP {status_code}"
        if code in (0, 200):
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            reward = _num(data.get("today_credit")) or _num(data.get("credits"))
            detail = f"签到成功 +{_fmt(reward)}" if reward else "签到成功"
            return self._result(account, key, title, RESULT_OK, detail, reward)
        if code == WB_ALREADY_CHECKED_IN or "已签到" in message:
            # 上游说今天已经签过了 —— 这不是失败，是「无事可做」。
            return self._result(account, key, title, RESULT_SKIPPED, message or "今日已签到")
        self._note_auth(account, status_code, body, message)
        return self._result(
            account,
            key,
            title,
            RESULT_FAILED,
            self._explain(status_code, message),
            retryable=_retryable_status(status_code),
        )

    def _run_trae(
        self,
        account: Account,
        credential: dict[str, Any],
        *,
        keys: set[str] | None = None,
    ) -> list[RunResult]:
        key, title = "trae.daily_checkin", "每日签到"
        if keys is not None and key not in keys:
            return []
        try:
            jwt, device_id, host = trae_credentials(credential)
        except CredentialError as exc:
            return [self._result(account, key, title, RESULT_SKIPPED, str(exc))]

        base = (host or TRAE_ACCOUNT_BASE).rstrip("/")
        headers = trae_headers(jwt, device_id)

        status_code, body, error = _call(
            self._client, "POST", f"{base}{TRAE_CHECKIN_STATUS_PATH}", headers, {}
        )
        if not isinstance(body, dict) or body.get("code") not in (0, 200):
            message = _message_of(body) or error or f"HTTP {status_code}"
            self._note_auth(account, status_code, body, message)
            return [
                self._result(
                    account,
                    key,
                    title,
                    RESULT_FAILED,
                    self._explain(status_code, message),
                    retryable=_retryable_status(status_code),
                )
            ]

        item = parse_trae_checkin(body)
        if item.state == STATE_DONE:
            return [self._result(account, key, title, RESULT_SKIPPED, "今日已签到")]
        if item.state == STATE_NOT_STARTED:
            return [
                self._result(account, key, title, RESULT_SKIPPED, "当前没有进行中的签到活动")
            ]

        status_code, body, error = _call(
            self._client, "POST", f"{base}{TRAE_CHECKIN_CLAIM_PATH}", headers, {}
        )
        code = body.get("code") if isinstance(body, dict) else None
        # 9074 是上游的冷却式限流：立刻重试必然还是 9074，必须退避后再打。
        for delay in TRAE_CLAIM_RETRY_DELAYS:
            if code != TRAE_CODE_BUSY:
                break
            time.sleep(delay)
            status_code, body, error = _call(
                self._client, "POST", f"{base}{TRAE_CHECKIN_CLAIM_PATH}", headers, {}
            )
            code = body.get("code") if isinstance(body, dict) else None

        if code in (0, 200):
            return [self._result(account, key, title, RESULT_OK, "签到成功", item.reward)]
        message = _message_of(body) or error or f"HTTP {status_code}"
        if code == TRAE_CODE_DEVICE_TAKEN:
            # 本账号没签成，只是这台设备今天的名额被别的账号占了 —— 报失败，别粉饰。
            return [
                self._result(
                    account,
                    key,
                    title,
                    RESULT_FAILED,
                    "签到失败：该设备今日已被其它账号签到（设备号冲突）",
                )
            ]
        if code == TRAE_CODE_BUSY:
            # 9074 是上游的冷却式限流 —— 退避后仍可能恢复，值得再试一次。
            return [
                self._result(
                    account,
                    key,
                    title,
                    RESULT_FAILED,
                    "签到失败：上游限流（退避重试仍未通过）",
                    retryable=True,
                )
            ]
        if code == TRAE_CODE_PARAM_INVALID:
            return [
                self._result(
                    account,
                    key,
                    title,
                    RESULT_FAILED,
                    "签到失败：设备号缺失或不被接受",
                )
            ]
        self._note_auth(account, status_code, body, message)
        return [
            self._result(
                account,
                key,
                title,
                RESULT_FAILED,
                self._explain(status_code, message),
                retryable=_retryable_status(status_code),
            )
        ]

    def _note_auth(
        self, account: Account, status_code: int, body: Any, message: str = ""
    ) -> None:
        """令牌真的失效时回写账号状态，保证「活动失败」与「号池显示」是同一份事实。

        判定口径与验活一致：只看 HTTP 401/403；WorkBuddy 的 WAF 指纹拦截
        （``code=10085``）不是令牌问题，必须排除 —— 否则一次网关抖动会把好号
        标成需重登，用户白点一遍登录。
        """
        if status_code not in (401, 403):
            return
        code = body.get("code") if isinstance(body, dict) else None
        if code == 10085:
            logger.info("活动请求被 WAF 拦截，不改凭据状态：%s", account.display_name)
            return
        try:
            self._repo.set_credential_state(account.id, CredentialState.RELOGIN_REQUIRED)
        except AccountNotFound:
            return
        except Exception:  # noqa: BLE001 - 状态回写失败不该让本轮活动结论消失
            logger.exception("回写凭据状态失败：%s", account.id)
            return
        logger.info(
            "活动请求返回 %s，凭据标记为需重新登录：%s", status_code, account.display_name
        )

    def _record_runs(self, results: list[RunResult]) -> None:
        """把本轮结论落库，供「今天做了什么 / 为什么失败」回看。

        写失败只降级为日志：流水是观测数据，不该让已经跑完的活动结果丢掉。
        """
        if not results:
            return
        day = datetime.now(UTC).astimezone().date().isoformat()
        rows = [
            {
                "accountId": item.account_id,
                "key": item.key,
                "day": day,
                "attempt": item.attempt,
                "status": item.status,
                "message": item.message,
                "reward": item.reward,
            }
            for item in results
        ]
        try:
            self._repo.record_activity_runs(rows)
        except Exception:  # noqa: BLE001
            logger.exception("写入活动执行流水失败")

    def _result(
        self,
        account: Account,
        key: str,
        title: str,
        status: str,
        message: str,
        reward: float | None = None,
        *,
        retryable: bool = False,
    ) -> RunResult:
        return RunResult(
            account_id=account.id,
            display_name=account.display_name,
            platform=get_scope(account.scope).platform,
            key=key,
            title=title,
            status=status,
            message=message,
            reward=reward,
            retryable=retryable,
        )

    def close(self) -> None:
        self._client.close()
        if self._growth_runner is not None:
            self._growth_runner.close()


def _summarize_results(results: list[RunResult]) -> dict[str, int]:
    return {
        "total": len(results),
        "ok": sum(1 for item in results if item.status == RESULT_OK),
        "skipped": sum(1 for item in results if item.status == RESULT_SKIPPED),
        "failed": sum(1 for item in results if item.status == RESULT_FAILED),
    }


def _retry_targets(accounts: list[Account], results: list[RunResult]) -> list[Account]:
    """首轮里出现「可重试失败」的账号，保持原顺序。

    只有 ``retryable`` 的失败才算：401/403/400 与业务拒绝重试也只会再失败一次。
    """
    failed = {
        item.account_id
        for item in results
        if item.status == RESULT_FAILED and item.retryable
    }
    return [account for account in accounts if account.id in failed]


def _merge_attempt(first: list[RunResult], second: list[RunResult]) -> list[RunResult]:
    """重试轮结果顶替首轮同账号同活动的行，保证「每个活动只留一行」。

    重试轮没返回的项（例如账号中途没凭据了）保留首轮那一行 —— 失败原因不该被吞掉。
    """
    latest = {(item.account_id, item.key): item for item in second}
    merged = [latest.pop((item.account_id, item.key), item) for item in first]
    merged.extend(latest.values())
    return merged


def summarize(accounts: list[AccountActivities]) -> dict[str, int]:
    """页面顶部的几个数。

    ``actionable`` 是唯一和「一键执行」直接对应的数：能自动做、且还没做的项数。
    其余是状态分布，用来让用户知道「为什么这次点完没有动静」。
    """
    items = [item for account in accounts for item in account.items]
    return {
        "accounts": len(accounts),
        "detectable": sum(1 for account in accounts if account.detectable),
        "actionable": sum(1 for item in items if item.actionable),
        "todo": sum(1 for item in items if item.state == STATE_TODO),
        "inProgress": sum(1 for item in items if item.state == STATE_IN_PROGRESS),
        "done": sum(1 for item in items if item.state == STATE_DONE),
        "manual": sum(
            1 for item in items if not item.automatable and item.state != STATE_DONE
        ),
        "unavailable": sum(1 for item in items if item.state == STATE_UNAVAILABLE),
    }


#: 活动级状态。与账号级 state 区分：账号级说「这个号这一项怎样」，
#: 活动级说「这个活动整体对当前号池是什么局面」。
CAMPAIGN_NO_ACCOUNTS = "no_accounts"
CAMPAIGN_NOT_DETECTED = "not_detected"
CAMPAIGN_ACTIVE = "active"
CAMPAIGN_PARTIAL = "partial"
CAMPAIGN_DONE = "done"
CAMPAIGN_UNAVAILABLE = "unavailable"
#: 检测到了目录里没有的活动 key。不能静默丢弃，否则「新活动上线」在界面上等于消失。
CAMPAIGN_UNCATALOGUED = "uncatalogued"


def _account_platform(account: AccountActivities) -> str:
    """账号所属平台。优先按 scope 解析，未知 scope 退回检测时记下的平台。"""
    try:
        return get_scope(account.scope).platform
    except UnknownScope:
        return account.platform


def summarize_campaigns(accounts: list[AccountActivities]) -> list[dict[str, Any]]:
    """把账号明细聚合到每个官方活动上，供活动卡片/概览直接渲染。

    统计口径只在这里算一次：前端不再二次推断，避免「卡片进度」与「按账号明细」
    对不上。每个账号只会落进一个桶，保证 ``done/todo/inProgress/manual/
    unavailable/notDetected`` 之和等于 ``accountsTotal``。
    """
    platform_counts: dict[str, int] = {}
    for account in accounts:
        platform = _account_platform(account)
        platform_counts[platform] = platform_counts.get(platform, 0) + 1

    campaigns: list[dict[str, Any]] = []
    for entry in sorted(available_catalog(), key=lambda item: item["sortOrder"]):
        key = entry["key"]
        platform = entry["platform"]
        accounts_total = platform_counts.get(platform, 0)

        done = todo = in_progress = manual = unavailable = not_detected = 0
        period_starts: list[str] = []
        period_ends: list[str] = []
        for account in accounts:
            if _account_platform(account) != platform:
                continue
            if not account.detectable:
                not_detected += 1
                continue
            item = next(
                (candidate for candidate in account.items if candidate.key == key), None
            )
            if item is None:
                # 可检测但没返回这一项：算「未检测到」，不冒充不可用。
                not_detected += 1
                continue
            if item.period_start:
                period_starts.append(item.period_start)
            if item.period_end:
                period_ends.append(item.period_end)
            if item.state == STATE_DONE:
                done += 1
            elif item.state in (STATE_UNAVAILABLE, STATE_NOT_STARTED):
                unavailable += 1
            elif not item.automatable:
                manual += 1
            elif item.state == STATE_TODO:
                todo += 1
            elif item.state == STATE_IN_PROGRESS:
                in_progress += 1
            else:
                unavailable += 1

        detectable = accounts_total - not_detected
        actionable = todo + in_progress
        all_done = accounts_total > 0 and done == accounts_total

        if accounts_total == 0:
            status = CAMPAIGN_NO_ACCOUNTS
        elif not_detected == accounts_total:
            status = CAMPAIGN_NOT_DETECTED
        elif all_done:
            status = CAMPAIGN_DONE
        elif done and (todo or in_progress or manual or unavailable or not_detected):
            status = CAMPAIGN_PARTIAL
        elif todo or in_progress or manual:
            status = CAMPAIGN_ACTIVE
        else:
            status = CAMPAIGN_UNAVAILABLE

        campaigns.append(
            {
                "key": key,
                "platform": platform,
                "group": entry["group"],
                "title": entry["title"],
                "subtitle": entry["subtitle"],
                "description": entry["description"],
                "rewardHint": entry["rewardHint"],
                "automatable": entry["automatable"],
                "theme": entry["theme"],
                "icon": entry["icon"],
                "tags": list(entry["tags"]),
                "rules": list(entry["rules"]),
                "rewards": list(entry["rewards"]),
                "periodNote": entry["periodNote"],
                "officialUrl": entry["officialUrl"],
                "officialLabel": entry["officialLabel"],
                "featured": entry["featured"],
                "sortOrder": entry["sortOrder"],
                "accountsTotal": accounts_total,
                "detectable": detectable,
                "done": done,
                "todo": todo,
                "inProgress": in_progress,
                "manual": manual,
                "unavailable": unavailable,
                "notDetected": not_detected,
                "actionable": actionable,
                "allDone": all_done,
                "status": status,
                "periodStart": min(period_starts) if period_starts else None,
                "periodEnd": max(period_ends) if period_ends else None,
            }
        )
    campaigns.extend(_uncatalogued_campaigns(accounts, platform_counts))

    # 未收录活动永远排在已收录活动之后，避免一个上游新 key 抢走主推位。
    campaigns.sort(key=lambda item: (item["status"] == CAMPAIGN_UNCATALOGUED, item["sortOrder"]))
    return campaigns


def _uncatalogued_campaigns(
    accounts: list[AccountActivities], platform_counts: dict[str, int]
) -> list[dict[str, Any]]:
    """为目录里没有的活动 key 生成兜底卡片。

    「检测到了但目录没收录」不能直接丢掉：新活动上线时用户会以为工具漏了，
    维护者也失去发现目录过期的信号。这里按 (key, platform) 聚合成一张朴素卡片。
    """
    buckets: dict[tuple[str, str], dict[str, Any]] = {}
    for account in accounts:
        platform = _account_platform(account)
        if not account.detectable:
            continue
        seen: set[str] = set()
        for item in account.items:
            if item.key in ACTIVITY_KEYS or item.key in seen:
                continue
            seen.add(item.key)
            slot = buckets.setdefault(
                (item.key, platform),
                {
                    "key": item.key,
                    "platform": platform,
                    "group": item.group,
                    "title": item.title or item.key,
                    "subtitle": item.subtitle,
                    "done": 0,
                    "todo": 0,
                    "inProgress": 0,
                    "manual": 0,
                    "unavailable": 0,
                    "detected": 0,
                },
            )
            slot["detected"] += 1
            if item.state == STATE_DONE:
                slot["done"] += 1
            elif item.state in (STATE_UNAVAILABLE, STATE_NOT_STARTED):
                slot["unavailable"] += 1
            elif not item.automatable:
                slot["manual"] += 1
            elif item.state == STATE_TODO:
                slot["todo"] += 1
            elif item.state == STATE_IN_PROGRESS:
                slot["inProgress"] += 1

    campaigns: list[dict[str, Any]] = []
    for (key, platform), slot in sorted(buckets.items()):
        accounts_total = platform_counts.get(platform, 0)
        not_detected = max(0, accounts_total - int(slot["detected"]))
        campaigns.append(
            {
                "key": key,
                "platform": platform,
                "group": slot["group"],
                "title": slot["title"],
                "subtitle": slot["subtitle"],
                "description": (
                    "检测到了活动目录里尚未收录的活动，这里展示的是上游原始信息。"
                    "目录会在下次更新时补上规则与官方入口。"
                ),
                "rewardHint": "",
                "automatable": False,
                "theme": "blue",
                "icon": "sun",
                "tags": ["未收录"],
                "rules": [],
                "rewards": [],
                "periodNote": "",
                "officialUrl": "",
                "officialLabel": "",
                "featured": False,
                "sortOrder": 999,
                "accountsTotal": accounts_total,
                "detectable": accounts_total - not_detected,
                "done": int(slot["done"]),
                "todo": int(slot["todo"]),
                "inProgress": int(slot["inProgress"]),
                "manual": int(slot["manual"]),
                "unavailable": int(slot["unavailable"]),
                "notDetected": not_detected,
                "actionable": int(slot["todo"]) + int(slot["inProgress"]),
                "allDone": accounts_total > 0 and int(slot["done"]) == accounts_total,
                "status": CAMPAIGN_UNCATALOGUED,
                "periodStart": None,
                "periodEnd": None,
            }
        )
    return campaigns
