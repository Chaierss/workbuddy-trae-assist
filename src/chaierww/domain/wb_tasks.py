"""WorkBuddy 成长任务 API 自动化（参考 workbuddy2api-panel 协议，2026-10-19 移植）。

用户拍板（2026-10-19）：参照 workbuddy2api-panel 做纯 API 自动化——构造客户端指纹
向 `/v2/report` 上报行为事件，上游计分后走 Web 域领奖。

## 三个域的分工（实测/参考项目一致）

| 域 | 用途 |
|:--|:--|
| `www.codebuddy.cn` | 任务列表 / accept / CLI 指纹 report / buddy 协议 / 签到 |
| `copilot.tencent.com` | 桌面指纹 report（workbuddy-desktop UA）/ 主题设置 |
| `www.workbuddy.cn` | 领奖 claim（`x-client-platform: web`，成长中心形状） |

## 任务闭环（每个任务）

列表 → 已 claimed 跳过 → 未 accepted 先 accept（幂等）→ 动作（上报事件组）→
轮询进度（上游计分是**异步**的，上报 200 ≠ 已计分，3s×4 有界轮询）→ 达标即
claim → 汇报 `+积分 +能量`。

## 第一批动作（参考项目实测「纯上报可点亮」）

chat_5 / first_buddy / automation_1 / Library_read / template_5 / playbook_prompt /
create_canvas / RichMeow_Chat / Buddy_App / Hp_Appearance。

**不做**（需真实对话烧积分 / 真实授权 / 小程序口径）：Model_chat_*、expert_*、
skill_1、black_cat、Sequential_*、school_season —— 上游没有对应动作时汇报
「本工具暂不支持自动完成」，不谎报成功。

## 幂等与安全

- 已 claimed / 已达标未领 的任务直接处理对应阶段，不重复上报。
- accept 幂等：重复接受上游返回成功或业务提示，都不算致命。
- claim 重复领返回业务错误（already_claimed），安全。
"""

from __future__ import annotations

import os
import platform
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from chaierww.domain.platform_api import (
    TIMEOUT_SECONDS,
    WORKBUDDY_API_ORIGIN,
    WORKBUDDY_WEB_ORIGIN,
    workbuddy_credentials,
    workbuddy_headers,
)

# 桌面指纹域与常量（参考项目 desktop.go）
COPILOT_ORIGIN = "https://copilot.tencent.com"
DESKTOP_UA = "WorkBuddy/5.5.6 WorkBuddy/5.5.6 CLI/2.137.1"
WEB_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

TASKS_LIST_PATH = "/v2/activity/growth/tasks"
TASKS_ACCEPT_PATH = "/v2/activity/growth/tasks/accept"
REPORT_PATH = "/v2/report"
BUDDY_AGREEMENT_PATH = "/activity/growth/buddy/agreement"
BUDDY_FIRST_PATH = "/activity/growth/buddy/first"
APPEARANCE_SET_PATH = "/v2/user-asset/appearance/set"

#: 领奖走 Web 成长中心形状（任务码在路径里）。
CLAIM_WEB_PATH = "/activity/growth/tasks/{code}/claim"

#: 上游计分异步：上报后进度要数秒才刷新，有界轮询。
POLL_ATTEMPTS = 3
POLL_GAP_SECONDS = 3.0
#: 动作之间的节流（防频控）。
ACTION_GAP_SECONDS = 1.0

#: 单账号整体时间预算（秒）。HTTP 同步执行，超预算即停，幂等可重跑。
TOTAL_BUDGET_SECONDS = 100.0


@dataclass(frozen=True, slots=True)
class GrowthTask:
    """任务列表里的单个任务（宽松解析上游两种 progress 形状）。"""

    code: str
    title: str
    accept_status: str = ""
    locked: bool = False
    target: int = 0
    current: int = 0

    @property
    def claimed(self) -> bool:
        return self.accept_status == "claimed"

    @property
    def claimable(self) -> bool:
        return not self.claimed and self.target > 0 and self.current >= self.target


@dataclass(frozen=True, slots=True)
class TaskRunOutcome:
    """单任务的执行结果。"""

    code: str
    title: str
    status: str  # ok / skipped / failed
    message: str = ""
    credit: float | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "title": self.title,
            "status": self.status,
            "message": self.message,
            "credit": self.credit,
        }


# ---------- 协议层 ----------


def _parse_tasks(data: Any) -> list[GrowthTask]:
    """解析 data.tasks[]；progress 可能是 {current,target} 对象或平铺字段。"""
    if not isinstance(data, dict):
        return []
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list):
        return []
    out: list[GrowthTask] = []
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("task_code") or "").strip()
        if not code:
            continue
        current = raw.get("current")
        target = raw.get("target")
        progress = raw.get("progress")
        if isinstance(progress, dict):
            current = progress.get("current", current)
            target = progress.get("target", target)
        out.append(
            GrowthTask(
                code=code,
                title=str(raw.get("title") or code),
                accept_status=str(raw.get("accept_status") or ""),
                locked=bool(raw.get("locked")),
                target=int(target or 0),
                current=int(current or 0),
            )
        )
    return out


class _Proto:
    """WorkBuddy 成长任务协议客户端（单账号一组 token）。"""

    def __init__(self, client: httpx.Client, token: str, uid: str, domain: str) -> None:
        self._client = client
        self.token = token
        self.uid = uid
        self.domain = domain
        # API 域头（任务列表 / accept / CLI report / buddy）
        self.api_headers = workbuddy_headers(token, uid, domain)
        # Web 域领奖头（成长中心形状）
        self.claim_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "x-client-platform": "web",
            "Origin": WORKBUDDY_WEB_ORIGIN,
            "Referer": f"{WORKBUDDY_WEB_ORIGIN}/profile/growth-center",
            "User-Agent": WEB_UA,
        }
        if uid:
            self.claim_headers["X-User-Id"] = uid
        if domain:
            self.claim_headers["X-Domain"] = domain
        # 桌面指纹头
        self.desktop_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
            "User-Agent": DESKTOP_UA,
            "X-Domain": COPILOT_ORIGIN,
            "X-Product": "SaaS",
        }
        if uid:
            self.desktop_headers["X-User-Id"] = uid
        # Web 指纹 report 头
        self.web_report_headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "x-client-platform": "web",
            "Origin": WORKBUDDY_WEB_ORIGIN,
            "User-Agent": WEB_UA,
        }
        if uid:
            self.web_report_headers["X-User-Id"] = uid

    def _desktop_fingerprint(self) -> dict[str, Any]:
        """桌面端公共指纹（参考项目 desktopFingerprint）。

        上游靠这套字段识别「真实 WorkBuddy 桌面客户端」。缺失 ideName/ideType/
        extName/machineId 等字段时事件会被接受但**不归账**（进度不涨）。
        ``reportDelay`` 用 2000（真实客户端的批量上报延迟语义），不是 0。
        """
        now = int(time.time() * 1000)
        facts = _desktop_facts()
        return {
            "timezone": "Asia/Shanghai",
            "reportDelay": 2000,
            "userId": self.uid,
            "username": "",
            "userNickname": "",
            "product": "SaaS",
            "releaseDate": 1789036585355,
            "commit": "5f9692923c93033111c51ad7b003eb80204a9b75",
            "ideName": "WorkBuddy",
            "ideType": "WorkBuddy",
            "ideVersion": "5.5.6",
            "machineId": _stable_hex(self.uid, "machine"),
            "sessionId": _stable_hex(self.uid, "session"),
            "extName": "workbuddy-desktop",
            "extVersion": "5.5.6",
            **facts,
            "timestamp": now,
            "presentAt": now,
        }

    def _post(self, url: str, headers: dict[str, str], body: Any = None) -> tuple[int, Any, str]:
        try:
            resp = self._client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            return 0, None, f"网络请求失败：{exc}"
        try:
            data: Any = resp.json()
        except ValueError:
            data = None
        message = ""
        if resp.status_code >= 400:
            message = _message_of(data) or f"HTTP {resp.status_code}"
        elif isinstance(data, dict) and data.get("code") not in (None, 0, 200):
            message = _message_of(data)
        return resp.status_code, data, message

    def _get(self, url: str, headers: dict[str, str]) -> tuple[int, Any, str]:
        try:
            resp = self._client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            return 0, None, f"网络请求失败：{exc}"
        try:
            data: Any = resp.json()
        except ValueError:
            data = None
        message = ""
        if resp.status_code >= 400:
            message = _message_of(data) or f"HTTP {resp.status_code}"
        elif isinstance(data, dict) and data.get("code") not in (None, 0, 200):
            message = _message_of(data)
        return resp.status_code, data, message

    # ---- 任务 ----

    def list_tasks(self) -> tuple[list[GrowthTask], str]:
        url = f"{WORKBUDDY_API_ORIGIN}{TASKS_LIST_PATH}"
        status, body, error = self._get(url, self.api_headers)
        if status != 200 or not isinstance(body, dict) or body.get("code") not in (0, 200):
            return [], error or f"HTTP {status}"
        return _parse_tasks(body.get("data")), ""

    def accept(self, codes: list[str]) -> str:
        _, body, error = self._post(
            f"{WORKBUDDY_API_ORIGIN}{TASKS_ACCEPT_PATH}",
            self.api_headers,
            {"task_codes": codes},
        )
        # accept 幂等：上游对已接受的返回成功或业务提示，都不算致命。
        # 只有网络层失败 / 非常明确的 not found 才当错误。
        if error and "not found" in error.lower():
            return error
        return ""

    def claim(self, code: str) -> tuple[float, float, str]:
        """领奖（Web 域）。返回 (credit, energy, error)。重复领取返回 (0,0,'')。"""
        url = WORKBUDDY_WEB_ORIGIN + CLAIM_WEB_PATH.format(code=code)
        status, body, error = self._post(url, self.claim_headers, {})
        if status != 200:
            return 0.0, 0.0, error or f"HTTP {status}"
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            return 0.0, 0.0, ""
        if data.get("already_claimed"):
            return 0.0, 0.0, ""
        credit = _num(data.get("credit"))
        energy = _num(data.get("energy"))
        return (credit or 0.0), (energy or 0.0), ""

    def task(self, code: str) -> tuple[GrowthTask | None, str]:
        tasks, error = self.list_tasks()
        if error:
            return None, error
        for item in tasks:
            if item.code == code:
                return item, ""
        return None, ""

    def task_waiting(
        self,
        code: str,
        *,
        attempts: int = POLL_ATTEMPTS,
        gap: float = POLL_GAP_SECONDS,
    ) -> GrowthTask | None:
        """回读任务；未达标时有界轮询（上游计分异步）。"""
        task, _ = self.task(code)
        if task is None or task.claimable or task.claimed:
            return task
        for _ in range(attempts - 1):
            time.sleep(gap)
            task2, _ = self.task(code)
            if task2 is None:
                return task
            task = task2
            if task.claimable or task.claimed:
                break
        return task

    # ---- 上报 ----

    def report_cli_chat(
        self, conversation_id: str, *, model_id: str = "", model_name: str = ""
    ) -> str:
        """CLI 指纹：codebuddy.cn/v2/report 发一条 chat_request_send。"""
        mid = model_id or "deepseek-v4-flash"
        mname = model_name or mid
        now = int(time.time() * 1000)
        event = {
            "eventCode": "chat_request_send",
            "timestamp": now,
            "reportDelay": 0,
            "mode": "craft",
            "conversationId": conversation_id,
            "requestId": conversation_id,
            "inputLength": 12,
            "requestModelId": mid,
            "requestModelName": mname,
            "isPlan": False,
            "isAutoExecuteTerminal": False,
            "isAutoModify": False,
            "codebaseEnable": False,
            "maxToken": 0,
            "maxSteps": 0,
            "temperature": 0,
            "maxRetries": 0,
            "mentionContexts": [],
            "knowledgeId": [],
            "knowledgeName": [],
            "codebaseId": "",
            "mentionContextCount": 0,
            "command": "",
            "expertId": "",
            "recommendId": "",
            "skillId": "",
            "skillCount": 0,
            "totalCount": 0,
            "fileUri": "",
            "presentAt": now,
            "traceId": "",
            "rootRequestId": conversation_id,
            "parentConversationId": conversation_id,
            "agentName": "default",
            "agentType": "conversation",
            "userId": self.uid,
        }
        _, _, error = self._post(
            f"{WORKBUDDY_API_ORIGIN}{REPORT_PATH}", self.api_headers, [event]
        )
        return error

    def report_desktop(self, events: list[dict[str, Any]]) -> str:
        """桌面指纹：copilot.tencent.com/v2/report 批量事件。

        上游强制要求 ``timestamp`` + ``reportDelay``，且靠整套桌面指纹
        （ideName/ideType/extName/machineId/sessionId/product 等）识别
        「真实 WorkBuddy 桌面客户端」——缺这些字段事件被接受但不归账。
        这里统一注入完整指纹，业务字段可覆盖（如真实设备对齐）。
        """
        fp = self._desktop_fingerprint()
        stamped = []
        for event in events:
            item = dict(fp)
            item.update(event)
            stamped.append(item)
        _, _, error = self._post(f"{COPILOT_ORIGIN}{REPORT_PATH}", self.desktop_headers, stamped)
        return error

    def report_web_click(self, page_url: str, element_id: str, element_name: str) -> str:
        """Web 指纹：workbuddy.cn/v2/report 发 web_element_click。"""
        event = {
            "eventCode": "web_element_click",
            "timestamp": int(time.time() * 1000),
            "reportDelay": 0,
            "pageURL": page_url,
            "elementId": element_id,
            "elementName": element_name,
            "os": "Win32",
            "arch": "",
            "osVersion": "10.0",
            "userAgent": WEB_UA,
            "machineId": _stable_id(self.uid, "webmachine"),
            "userId": self.uid,
            "userNickname": "",
            "enterpriseId": "",
        }
        _, _, error = self._post(
            f"{WORKBUDDY_WEB_ORIGIN}{REPORT_PATH}", self.web_report_headers, [event]
        )
        return error

    def buddy_agreement(self) -> str:
        _, _, error = self._post(
            f"{WORKBUDDY_API_ORIGIN}{BUDDY_AGREEMENT_PATH}", self.api_headers, {}
        )
        return error

    def buddy_first(self) -> str:
        _, _, error = self._post(
            f"{WORKBUDDY_API_ORIGIN}{BUDDY_FIRST_PATH}", self.api_headers, {}
        )
        return error

    def set_appearance(self, resource_key: str) -> str:
        _, _, error = self._post(
            f"{COPILOT_ORIGIN}{APPEARANCE_SET_PATH}",
            self.desktop_headers,
            {"kind": "theme", "resource_key": resource_key},
        )
        return error


def _message_of(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    for key in ("message", "msg", "error_msg"):
        text = str(body.get(key) or "").strip()
        if text:
            return text
    return ""


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


def _stable_id(seed: str, tag: str) -> str:
    """从 uid 派生稳定 id（不落盘、不回显 token）。"""
    import hashlib

    digest = hashlib.sha256(f"{tag}:{seed}".encode()).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def _memory_gb() -> int:
    """物理内存 GB 数；读不到就回落到 Windows 上一直用的 24，不让指纹中断上报。"""
    try:
        import psutil

        return max(1, int(psutil.virtual_memory().total // (1024**3)))
    except Exception:  # noqa: BLE001 - 指纹是尽力而为，取不到不值得让整次上报失败
        return 24


def _desktop_facts() -> dict[str, Any]:
    """上报指纹里的平台字段，按**真实运行平台**取，不再一律报 Windows。

    上游按这套字段识别桌面客户端；在 Linux 上继续报 ``os=win32`` /
    ``osVersion=10.0.26220`` 等于把自己伪装成另一台机器，一旦上游做一致性校验就是
    异常样本。默认值是尽力而为，读不到也不阻断上报。
    """
    system = sys.platform
    os_name = {"win32": "win32", "darwin": "darwin"}.get(system, "linux")
    machine = platform.machine().lower()
    arch = {
        "amd64": "x64",
        "x86_64": "x64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, machine or "x64")
    return {
        "os": os_name,
        "arch": arch,
        "osVersion": platform.version() or platform.release() or "",
        "cpuCores": os.cpu_count() or 1,
        "memorySize": _memory_gb(),
    }


def _stable_hex(seed: str, tag: str) -> str:
    """从 uid 派生 36 位 hex 设备标识（参考项目 deriveID：machineId/qimei36 复用）。"""
    import hashlib

    digest = hashlib.sha256(f"{tag}:{seed}".encode()).hexdigest()
    return digest[:36]


def _conv(prefix: str) -> str:
    return f"chaierww-{prefix}-{int(time.time() * 1000)}-{uuid.uuid4().hex[:6]}"


# ---------- 桌面事件构造（参考项目 desktop.go 移植） ----------


def _desktop_chat_sequence(conversation_id: str, request_id: str) -> list[dict[str, Any]]:
    """「桌面端成功对话」完整事件链（点亮 RichMeow_Chat）。"""
    msg = f"msg-{uuid.uuid4().hex[:8]}"
    now = int(time.time() * 1000)

    def ev(code: str, extra: dict[str, Any]) -> dict[str, Any]:
        return {"eventCode": code, **extra}

    return [
        ev(
            "agent_task_created",
            {
                "source": "LOCAL", "name": "working", "task_target": "local", "mode": "craft",
                "requestModelId": "fast-model", "requestModelName": "fast-model",
                "has_repo": False, "repo_type": "none", "workspace_type": "empty",
                "has_connector": False, "connector_types": [],
                "has_mention": False, "mention_types": [],
                "has_template": False, "action": "", "template_name": "",
                "has_expert": False, "expert_id": "", "expert_name": "", "expert_industry_id": "",
                "has_skill": False, "skill_names": [],
                "conversationId": conversation_id, "messageId": msg,
                "buddyId": "", "buddyName": "",
            },
        ),
        ev(
            "chat_message_send",
            {
                "messageId": msg + "-assistant", "historyCount": 0,
                "isContextTruncated": False, "currentStepCount": 1,
                "traceId": request_id, "rootRequestId": request_id,
                "parentConversationId": conversation_id,
                "agentName": "cli", "agentType": "main",
            },
        ),
        ev(
            "chat_request_send",
            {
                "inputLength": 24, "isPlan": False, "isAutoExecuteTerminal": False,
                "isAutoModify": False, "codebaseEnable": False, "maxToken": 0,
                "maxSteps": 500, "temperature": 0, "maxRetries": 0,
                "mentionContexts": [], "knowledgeId": [], "knowledgeName": [],
                "codebaseId": "", "mentionContextCount": 0, "command": "",
                "recommendId": "", "skillId": "", "skillCount": 0, "totalCount": 0,
                "traceId": request_id, "rootRequestId": request_id,
                "parentConversationId": conversation_id,
                "agentName": "cli", "agentType": "main",
                "codebuddy.session_id": conversation_id,
                "codebuddy.conversation_request_id": request_id,
            },
        ),
        ev(
            "chat_message_response",
            {
                "messageId": msg + "-assistant", "responseModelId": "fast-model",
                "inputToken": 120, "outputToken": 80, "totalToken": 200,
                "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
                "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
                "firstTokenAt": now, "traceId": request_id,
                "conversationId": conversation_id,
                "rootRequestId": request_id, "parentConversationId": conversation_id,
                "agentName": "cli", "agentType": "main",
                "codebuddy.session_id": conversation_id,
                "codebuddy.conversation_request_id": request_id,
            },
        ),
        ev(
            "chat_message_status",
            {
                "messageId": msg + "-assistant", "messageErrorCode": "0",
                "traceId": request_id, "rootRequestId": request_id,
                "parentConversationId": conversation_id,
                "agentName": "cli", "agentType": "main",
            },
        ),
        ev(
            "chat_request_response",
            {
                "mode": "craft", "toolCallCount": 0,
                "inputToken": 120, "outputToken": 80, "totalToken": 200,
                "cachedTokens": 0, "cachedWriteTokens": 0, "cachedMissTokens": 0,
                "isSuccessful": True, "messageErrorCode": "", "finishReason": "stop",
                "rootRequestId": request_id, "parentConversationId": conversation_id,
            },
        ),
    ]


def _desktop_automation_event() -> dict[str, Any]:
    return {
        "eventCode": "automated_task_create_suc", "name": "chaierww 自动化",
        "source": "manually", "modelId": "fast-model", "modelIsThinking": True,
        "connectorCount": 0, "skills": "", "skillCount": 0,
        "scheduleType": "once", "mode": "LOCAL",
    }


def _desktop_template_pair(
    conv: str, req: str, template_id: str, template_name: str
) -> list[dict[str, Any]]:
    events = _desktop_chat_sequence(conv, req)
    events.append(
        {
            "eventCode": "agent_task_created_with_template", "mode": "working",
            "isCustomModel": False, "id": template_id, "name": template_name, "requestId": req,
        }
    )
    events.append(
        {"eventCode": "template_used", "template_id": template_id, "task_mode": "working"}
    )
    return events


def _desktop_playbook_sequence(conv: str, req: str) -> list[dict[str, Any]]:
    events = _desktop_chat_sequence(conv, req)
    payload = {
        "id": "pm-gtm-launch-plan",
        "name": "新产品上市 GTM 发布计划一页纸",
        "type": "document",
        "categoryId": "",
        "categoryName": "",
    }
    events.append(
        {
            "eventCode": "web_element_click", "pageName": "playbook_detail",
            "elementId": "playbook_ctaClick", "elementName": payload["name"], "source": "discover",
        }
    )
    events.append(
        {"eventCode": "playbook_cta_click", "source": "discover", "position": 0, **payload}
    )
    events.append(
        {"eventCode": "playbook_prompt_send", "conversationId": conv, "requestId": req, **payload}
    )
    return events


def _desktop_canvas_sequence(conv: str, req: str) -> list[dict[str, Any]]:
    events = _desktop_chat_sequence(conv, req)
    events.append(
        {
            "eventCode": "wbx_design_canvas_task_create", "conversationId": conv,
            "requestId": req, "source": "summon_keyword", "cost": 12000, "isSuccessful": True,
        }
    )
    events.append(
        {
            "eventCode": "wbx_design_canvas_open", "conversationId": conv,
            "requestId": req, "id": f"ardot-file-{req[-8:]}",
            "source": "summon_keyword", "type": "page", "cost": 13000, "isSuccessful": True,
        }
    )
    return events


def _desktop_buddy_app_sequence() -> list[dict[str, Any]]:
    buddy_id = "cb_y5Dy46tPQGGWtueMxXbe"
    buddy_name = "企鹅教师助手"
    base = {"mode": "LOCAL", "buddyId": buddy_id, "buddyName": buddy_name}
    return [
        {"eventCode": "buddyapp_discover_click", **base},
        {
            "eventCode": "buddyapp_show", "elementId": buddy_id,
            "elementName": buddy_name, "position": 2, **base,
        },
        {
            "eventCode": "buddyapp_enter_click", "elementId": buddy_id,
            "elementName": buddy_name, "position": 2, "isFirstPage": "1", **base,
        },
        {
            "eventCode": "buddyapp_auth_confirm_click", "elementId": buddy_id,
            "elementName": buddy_name, **base,
        },
        {
            "eventCode": "buddyapp_bindaccount_skip_click", "elementId": buddy_id,
            "elementName": buddy_name, **base,
        },
    ]


# ---------- 动作表 ----------
# 每个动作：proto -> 消息（失败抛 TaskActionError）。上报判据后由闭环统一轮询+领奖。

HP_THEME_KEY = "theme-tkmw7j"  # 和平精英激战金秋（Hp_Appearance 判据主题）
LIBRARY_DOC_URL = "https://www.workbuddy.cn/space/d/o0KWYeynteVv06UnAZqIFm"


class TaskActionError(RuntimeError):
    pass


def _act_chat_5(proto: _Proto, task: GrowthTask) -> str:
    target = task.target or 5
    need = max(target - task.current, 0)
    if need == 0:
        return "进度已达标，无需上报"
    for i in range(need):
        error = proto.report_cli_chat(_conv("chat5"))
        if error:
            raise TaskActionError(f"上报第 {i + 1}/{need} 条失败：{error}")
        if i < need - 1:
            time.sleep(ACTION_GAP_SECONDS)
    return f"已补报 {need} 条对话事件"


def _act_first_buddy(proto: _Proto, task: GrowthTask) -> str:
    error = proto.report_cli_chat(_conv("adopt"))
    if error:
        raise TaskActionError(f"前置上报失败：{error}")
    time.sleep(ACTION_GAP_SECONDS)
    error = proto.buddy_agreement()
    if error:
        raise TaskActionError(f"同意协议失败：{error}")
    error = proto.buddy_first()
    if error:
        raise TaskActionError(f"领取 Buddy 失败（上游要求当日活跃，可稍后重试）：{error}")
    return "已领取 Buddy（+300 分 +8 能量）"


def _act_automation(proto: _Proto, task: GrowthTask) -> str:
    error = proto.report_desktop([_desktop_automation_event()])
    if error:
        raise TaskActionError(f"定时任务事件上报失败：{error}")
    return "已上报定时任务创建事件"


def _act_library(proto: _Proto, task: GrowthTask) -> str:
    error = proto.report_web_click(
        LIBRARY_DOC_URL, "library_doc_intro_click", "WorkBuddy资料库介绍"
    )
    if error:
        raise TaskActionError(f"资料库事件上报失败：{error}")
    return "已上报资料库介绍阅读事件"


def _act_template(proto: _Proto, task: GrowthTask) -> str:
    templates = [
        ("1", "深度研究"), ("2", "周报生成"), ("3", "竞品分析"),
        ("4", "活动策划"), ("5", "代码评审"),
    ]
    for i, (tid, name) in enumerate(templates):
        ms = int(time.time() * 1000)
        error = proto.report_desktop(
            _desktop_template_pair(
                f"chaierww-tpl-{ms}-{i}", f"chaierww-tpl-req-{ms}-{i}", tid, name
            )
        )
        if error:
            raise TaskActionError(f"第 {i + 1} 组模板事件上报失败：{error}")
        time.sleep(0.3)
    return "已上报 template_used ×5"


def _act_playbook(proto: _Proto, task: GrowthTask) -> str:
    ms = int(time.time() * 1000)
    error = proto.report_desktop(
        _desktop_playbook_sequence(f"chaierww-pb-{ms}", f"chaierww-pb-req-{ms}")
    )
    if error:
        raise TaskActionError(f"灵感事件上报失败：{error}")
    return "已上报灵感案例做同款事件组"


def _act_canvas(proto: _Proto, task: GrowthTask) -> str:
    ms = int(time.time() * 1000)
    error = proto.report_desktop(
        _desktop_canvas_sequence(f"chaierww-canvas-{ms}", f"chaierww-canvas-req-{ms}")
    )
    if error:
        raise TaskActionError(f"画布事件上报失败：{error}")
    return "已上报设计画布创建/打开事件组（+300 分任务）"


def _act_richmeow(proto: _Proto, task: GrowthTask) -> str:
    ms = int(time.time() * 1000)
    error = proto.report_desktop(
        _desktop_chat_sequence(f"chaierww-rm-{ms}", f"chaierww-rm-req-{ms}")
    )
    if error:
        raise TaskActionError(f"桌面对话链上报失败：{error}")
    return "已按桌面端指纹上报完整对话事件链"


def _act_buddy_app(proto: _Proto, task: GrowthTask) -> str:
    error = proto.report_desktop(_desktop_buddy_app_sequence())
    if error:
        raise TaskActionError(f"Buddy 应用事件上报失败：{error}")
    return "已上报 Buddy 应用进入五连事件"


def _act_appearance(proto: _Proto, task: GrowthTask) -> str:
    error = proto.set_appearance(HP_THEME_KEY)
    if error:
        raise TaskActionError(f"设置主题失败：{error}")
    time.sleep(2.0)
    error = proto.report_desktop(
        [
            {
                "eventCode": "appearance_skin_apply", "action": "apply", "source": "settings_close",
                "id": HP_THEME_KEY, "vipLevel": 0, "series": "", "type": "unknown",
            }
        ]
    )
    if error:
        raise TaskActionError(f"皮肤生效事件上报失败：{error}")
    return "已设置主题并上报皮肤生效事件"


#: 动作表：任务码 → (标题, 动作)。顺序即执行顺序（先解锁依赖项）。
ACTIONS: dict[str, tuple[str, Callable[[_Proto, GrowthTask], str]]] = {
    "chat_5": ("和 AI 聊天 5 次", _act_chat_5),
    "first_buddy": ("领取一只 Buddy", _act_first_buddy),
    "automation_1": ("设置自动化任务", _act_automation),
    "Library_read": ("体验「资料库」", _act_library),
    "template_5": ("使用 5 个模板", _act_template),
    "playbook_prompt": ("探索优秀灵感", _act_playbook),
    "create_canvas": ("体验「设计创意模式」", _act_canvas),
    "RichMeow_Chat": ("桌面端对话1次", _act_richmeow),
    "Buddy_App": ("体验 WorkBuddy 「发现应用」", _act_buddy_app),
    "Hp_Appearance": ("体验「和平精英」主题", _act_appearance),
}


# ---------- 闭环 ----------


class GrowthTaskRunner:
    """单账号成长任务自动化闭环。线程安全（内部互斥，与签到执行共用纪律）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # trust_env=False：国内直连，不走本机代理（与 CreditsService 同理）。
        self._client = httpx.Client(timeout=TIMEOUT_SECONDS, trust_env=False)

    def close(self) -> None:
        self._client.close()

    def run_all(self, credential: dict[str, Any]) -> dict[str, Any]:
        """对该账号执行全部可自动的成长任务。幂等：已完成的自动跳过。"""
        with self._lock:
            return self._run_all_locked(credential)

    def _run_all_locked(self, credential: dict[str, Any]) -> dict[str, Any]:
        started = time.monotonic()

        def budget_left() -> float:
            return TOTAL_BUDGET_SECONDS - (time.monotonic() - started)

        try:
            token, uid, domain = workbuddy_credentials(credential)
        except Exception as exc:  # noqa: BLE001 - 统一转成 skipped 报告
            return {"outcomes": [], "error": str(exc)}

        proto = _Proto(self._client, token, uid, domain)
        tasks, error = proto.list_tasks()
        if error:
            return {"outcomes": [], "error": error or "任务列表获取失败"}

        outcomes: list[TaskRunOutcome] = []
        # 动作表覆盖不到的任务（expert/skill/小程序族）：本工具不伪造上报，但可以
        # 替用户把任务「接受」下来 —— 上游只有 accepted 之后才开始计进度，不接受
        # 的话用户在客户端真实操作也不会被记到这张任务卡上。
        idle: list[GrowthTask] = []

        for task in tasks:
            if task.locked or task.claimed:
                continue
            entry = ACTIONS.get(task.code)
            if entry is None:
                if task.claimable or task.accept_status == "completed":
                    if budget_left() <= 0:
                        outcomes.append(
                            TaskRunOutcome(
                                task.code,
                                task.title,
                                "skipped",
                                "时间预算用尽，稍后再点一次可继续（幂等）",
                            )
                        )
                        continue
                    # 用户已经在客户端做完了，只是没领奖 —— 直接领。
                    outcomes.append(self._claim(proto, task, task.title))
                elif task.accept_status in ("", "not_accepted"):
                    idle.append(task)
                continue
            if budget_left() <= 0:
                outcomes.append(
                    TaskRunOutcome(
                        task.code, task.title, "skipped", "时间预算用尽，稍后再点一次可继续（幂等）"
                    )
                )
                continue
            title, action = entry
            outcomes.append(self._run_one(proto, task, title, action))

        if idle:
            if budget_left() <= 0:
                for task in idle:
                    outcomes.append(
                        TaskRunOutcome(
                            task.code,
                            task.title,
                            "skipped",
                            "时间预算用尽，稍后再点一次可继续（幂等）",
                        )
                    )
            else:
                # 一次批量接受：幂等，且不产生任何行为上报。
                error = proto.accept([task.code for task in idle])
                for task in idle:
                    if error:
                        outcomes.append(
                            TaskRunOutcome(
                                task.code, task.title, "failed", f"接受任务失败：{error}"
                            )
                        )
                    else:
                        outcomes.append(
                            TaskRunOutcome(
                                task.code,
                                task.title,
                                "skipped",
                                "已接受任务；该任务要求真实使用，请在客户端完成",
                            )
                        )

        return {"outcomes": [item.payload() for item in outcomes], "error": None}

    def _run_one(
        self,
        proto: _Proto,
        task: GrowthTask,
        title: str,
        action: Callable[[_Proto, GrowthTask], str],
    ) -> TaskRunOutcome:
        def outcome(status: str, message: str, credit: float | None = None) -> TaskRunOutcome:
            return TaskRunOutcome(task.code, title, status, message, credit)

        # 已达标未领：直接领奖，不上报。
        if task.claimable or task.accept_status == "completed":
            return self._claim(proto, task, title)

        # 未接受先接受（幂等）。
        if task.accept_status in ("", "not_accepted"):
            error = proto.accept([task.code])
            if error:
                return outcome("failed", f"接受任务失败：{error}")
            time.sleep(ACTION_GAP_SECONDS)

        # 动作（上报判据）。
        try:
            message = action(proto, task)
        except TaskActionError as exc:
            return outcome("failed", str(exc))

        # 回读（异步计分，有界轮询）→ 达标即领奖。
        after = proto.task_waiting(task.code)
        if after is not None and (after.claimable or after.current >= (after.target or 1)):
            return self._claim(proto, after, title, prefix=message + "；")
        return outcome("ok", message + "（已上报，计分异步未落定，稍后再点可领奖）")

    def _claim(
        self, proto: _Proto, task: GrowthTask, title: str, *, prefix: str = ""
    ) -> TaskRunOutcome:
        credit, energy, error = proto.claim(task.code)
        if error:
            return TaskRunOutcome(task.code, title, "failed", f"{prefix}领奖失败：{error}")
        if credit == 0 and energy == 0:
            return TaskRunOutcome(task.code, title, "skipped", f"{prefix}奖励已领取过")
        detail = f"{prefix}已领奖 +{_fmt(credit)} 积分"
        if energy:
            detail += f" +{_fmt(energy)} 能量"
        return TaskRunOutcome(task.code, title, "ok", detail, credit or None)


def _fmt(value: float | None) -> str:
    if value is None:
        return ""
    return str(int(value)) if float(value).is_integer() else f"{value:.2f}"


def summarize_outcomes(outcomes: list[dict[str, Any]]) -> dict[str, int]:
    ok = sum(1 for item in outcomes if item.get("status") == "ok")
    skipped = sum(1 for item in outcomes if item.get("status") == "skipped")
    failed = sum(1 for item in outcomes if item.get("status") == "failed")
    return {"ok": ok, "skipped": skipped, "failed": failed}
