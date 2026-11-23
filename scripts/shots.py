"""生成 README / 官网用的界面截图，并在截图前完成脱敏。

为什么在页面里脱敏，而不是事后按坐标打码：
原先 `mask_screenshots.py` 用写死的像素矩形（基于某一版布局）去打码，界面一改
坐标就全废，只能重新量一遍。这里改成「先改 DOM 再截图」——
账号名、账号 ID、本机路径、邮箱、首字母头像都在浏览器里被替换成圆点或模糊，
布局不动、也不存在坐标漂移问题。

用法：

    # 1. 起后端（另开一个终端）
    python -m chaierww.main

    # 2. 截图
    python scripts/shots.py                    # 浅色，输出到 docs/images/
    python scripts/shots.py --theme dark       # 深色，输出到 docs/images-dark/
    python scripts/shots.py --only overview    # 只截某几张

依赖：playwright（`pip install playwright && playwright install chromium`）。
原始截图不含敏感信息，产物可直接进公开仓库。
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def find_chromium() -> str | None:
    """复用本机已装的 Chromium，省掉 `playwright install` 的一次下载。

    优先级：CHAIERWW_CHROME 环境变量 → ms-playwright 缓存 → 系统 Chrome/Edge。
    """
    env = os.environ.get("CHAIERWW_CHROME")
    if env and Path(env).is_file():
        return env

    cache = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    hits = sorted(glob.glob(str(cache / "chromium-*" / "chrome-win64" / "chrome.exe")))
    if hits:
        return hits[-1]

    for candidate in (
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ):
        if Path(candidate).is_file():
            return candidate
    return None


# 视口统一 1440 宽，截完再缩到 OUTPUT_WIDTH。
VIEWPORT_W = 1440

# 每张图：路由 + 视口高度。
# 高度按各页首屏信息量给，别让下面留一大块空白——这些页面都是内部滚动容器，
# 调大视口高度并不会多截到内容，只会多留白，所以每张都要单独调。
#
# 刻意**不截**的页面：网关的「日志」tab 会显示真实的 prompt 预览，
# 那是用户的提问原文，再怎么正则也挡不全，所以不进公开仓库。
SHOTS: list[dict[str, object]] = [
    {"name": "overview", "path": "/overview", "height": 1280},
    # 号池默认是紧凑视图，公开截图用宽松视图：分组徽标、凭据到期、会话同步都在这一档，
    # 和 README 里「账号、积分、凭据状态一屏看全」的说法对得上。
    {
        "name": "accounts",
        "path": "/accounts",
        "height": 1040,
        "session": {"chaierww.pool-density": "comfortable"},
    },
    {"name": "activity", "path": "/activity", "height": 900},
    # 活动页默认落在「官方活动」目录，逐账号任务要显式带 ?tab=accounts，
    # 两张一起放，README 才能把「目录里有什么」和「我的号做到哪」讲全。
    {"name": "activity-accounts", "path": "/activity?tab=accounts", "height": 900},
    {"name": "usage", "path": "/usage", "height": 1000},
    {"name": "health", "path": "/health", "height": 1000},
    {"name": "gateway-overview", "path": "/gateway?tab=overview", "height": 940},
    {"name": "gateway-keys", "path": "/gateway?tab=keys", "height": 760},
    {"name": "gateway-models", "path": "/gateway?tab=models", "height": 860},
    {"name": "settings-schedule", "path": "/settings?tab=automation", "height": 980},
    {"name": "settings-backup", "path": "/settings?tab=data", "height": 830},
    {"name": "settings-env", "path": "/settings?tab=client", "height": 1140},
    {"name": "settings-general", "path": "/settings?tab=appearance", "height": 1360},
]


def _iso(days: int = 0, hours: int = 0) -> str:
    moment = datetime.now(UTC) + timedelta(days=days, hours=hours)
    return moment.isoformat(timespec="seconds")


def _demo_daily(days: int = 7, base: int = 6) -> list[dict[str, object]]:
    today = datetime.now(UTC).date()
    return [
        {
            "date": (today - timedelta(days=offset)).isoformat(),
            "requests": base + (days - offset) * 2,
            "credit": round((base + (days - offset) * 2) * 0.42, 1),
            "models": [],
        }
        for offset in range(days - 1, -1, -1)
    ]


def _demo_detail(count: int = 42) -> list[dict[str, object]]:
    """WorkBuddy 的请求明细：用量页的均值、模型占比、对话还原都从这份数据算。"""
    now = datetime.now(UTC)
    rows = []
    for index in range(count):
        moment = now - timedelta(days=index // 6, hours=index % 6, minutes=index * 3)
        credit = round(2.4 + (index % 5) * 0.7, 1)
        rows.append(
            {
                "requestId": f"demo-request-{index:03d}",
                "credit": credit,
                "model": "claude-sonnet-4.5" if index % 3 else "gpt-5.1",
                "client": "WorkBuddy",
                "requestTime": moment.isoformat(timespec="seconds"),
                "date": moment.date().isoformat(),
                "conversationId": None,
            }
        )
    return rows


def demo_fixtures() -> dict[str, dict[str, object]]:
    """公开截图用的演示数据。

    只覆盖会露出账号、用量、健康事件与客户端路径的接口；其余请求照常打到
    空数据实例。这样截图里既没有真实账号，也不会因为空库变成一片「暂无」。
    """
    accounts = [
        {
            "id": "acc-demo-1",
            "scope": "workbuddy-cn",
            "platform": "workbuddy",
            "externalId": "demo-wb-1",
            "displayName": "林间",
            "note": "",
            "groupName": "主力号",
            "avatarPath": None,
            "source": "local_import",
            "credentialState": "usable",
            "credentialExpiresAt": _iso(12),
            "credentialRefreshable": True,
            "credentialDeviceForeign": False,
            "credentialCheckedAt": _iso(0, -2),
            "credentialRefreshedAt": _iso(0, -26),
            "credentialDetail": "凭据可用",
            "credentialErrorCode": "",
            "credentialNextRetryAt": None,
            "credentialAutoPaused": False,
            "credentialReconcileAt": None,
            "credentialReconcileResult": "",
            "loginState": "logged_in",
            "lastLoginProbeAt": _iso(0, -1),
            "loginProbeDetail": "",
            "lastUsedAt": _iso(0, -2),
            "createdAt": _iso(-60),
            "updatedAt": _iso(0, -2),
        },
        {
            "id": "acc-demo-2",
            "scope": "workbuddy-cn",
            "platform": "workbuddy",
            "externalId": "demo-wb-2",
            "displayName": "阿岚",
            "note": "",
            "groupName": "备用号",
            "avatarPath": None,
            "source": "local_import",
            "credentialState": "unverified",
            "credentialExpiresAt": _iso(1),
            "credentialRefreshable": True,
            "credentialDeviceForeign": False,
            "credentialCheckedAt": _iso(0, -9),
            "credentialRefreshedAt": _iso(0, -50),
            "credentialDetail": "凭据将在 1 天后到期",
            "credentialErrorCode": "",
            "credentialNextRetryAt": None,
            "credentialAutoPaused": False,
            "credentialReconcileAt": None,
            "credentialReconcileResult": "",
            "loginState": "logged_in",
            "lastLoginProbeAt": _iso(0, -9),
            "loginProbeDetail": "",
            "lastUsedAt": _iso(-1, -4),
            "createdAt": _iso(-45),
            "updatedAt": _iso(0, -9),
        },
        {
            "id": "acc-demo-3",
            "scope": "trae-cn",
            "platform": "trae",
            "externalId": "demo-trae-1",
            "displayName": "周末",
            "note": "",
            "groupName": "主力号",
            "avatarPath": None,
            "source": "local_import",
            "credentialState": "usable",
            "credentialExpiresAt": _iso(9),
            "credentialRefreshable": True,
            "credentialDeviceForeign": False,
            "credentialCheckedAt": _iso(0, -3),
            "credentialRefreshedAt": _iso(0, -20),
            "credentialDetail": "凭据可用",
            "credentialErrorCode": "",
            "credentialNextRetryAt": None,
            "credentialAutoPaused": False,
            "credentialReconcileAt": None,
            "credentialReconcileResult": "",
            "loginState": "logged_in",
            "lastLoginProbeAt": _iso(0, -3),
            "loginProbeDetail": "",
            "lastUsedAt": _iso(0, -5),
            "createdAt": _iso(-80),
            "updatedAt": _iso(0, -3),
        },
        {
            "id": "acc-demo-4",
            "scope": "trae-cn",
            "platform": "trae",
            "externalId": "demo-trae-2",
            "displayName": "文森",
            "note": "",
            "groupName": "备用号",
            "avatarPath": None,
            "source": "local_import",
            "credentialState": "relogin_required",
            "credentialExpiresAt": _iso(-2),
            "credentialRefreshable": False,
            "credentialDeviceForeign": False,
            "credentialCheckedAt": _iso(0, -6),
            "credentialRefreshedAt": None,
            "credentialDetail": "登录态已失效，需要重新登录",
            "credentialErrorCode": "relogin_required",
            "credentialNextRetryAt": None,
            "credentialAutoPaused": False,
            "credentialReconcileAt": None,
            "credentialReconcileResult": "",
            "loginState": "logged_out",
            "lastLoginProbeAt": _iso(0, -6),
            "loginProbeDetail": "",
            "lastUsedAt": _iso(-4),
            "createdAt": _iso(-30),
            "updatedAt": _iso(0, -6),
        },
    ]

    usage_accounts = [
        {
            "accountId": "acc-demo-1",
            "displayName": "林间",
            "platform": "workbuddy",
            "scope": "workbuddy-cn",
            "detectable": True,
            "requests": 320,
            "creditConsumed": 134.4,
            "todayRequests": 18,
            "todayCredit": 7.6,
            "weekRequests": 96,
            "weekCredit": 40.3,
            "monthRequests": 320,
            "monthCredit": 134.4,
            "models": [
                {"model": "claude-sonnet-4.5", "requests": 190, "credit": 79.8},
                {"model": "gpt-5.1", "requests": 130, "credit": 54.6},
            ],
            "daily": _demo_daily(31, 3),
            "detail": _demo_detail(42),
            "detailTruncated": False,
            "conversations": [],
            "linkedRequests": 0,
            "creditsConsumed": 760.5,
            "creditsTotal": 1200.0,
            "error": None,
            "updatedAt": _iso(0, -1),
        },
        {
            "accountId": "acc-demo-3",
            "displayName": "周末",
            "platform": "trae",
            "scope": "trae-cn",
            "detectable": True,
            "requests": 420,
            "creditConsumed": 0.0,
            "todayRequests": 22,
            "todayCredit": 0.0,
            "weekRequests": 128,
            "weekCredit": 0.0,
            "monthRequests": 420,
            "monthCredit": 0.0,
            "models": [{"model": "claude-sonnet-4.5", "requests": 420, "credit": 0.0}],
            "daily": _demo_daily(31, 6),
            "detail": [],
            "detailTruncated": False,
            "conversations": [],
            "linkedRequests": 0,
            "creditsConsumed": 880.0,
            "creditsTotal": 2000.0,
            "error": None,
            "updatedAt": _iso(0, -1),
        },
    ]

    health_items = [
        {
            "id": "demo-health-1",
            "kind": "credential_expiring",
            "severity": "warn",
            "state": "open",
            "scope": "workbuddy-cn",
            "accountId": "acc-demo-2",
            "title": "凭据即将到期",
            "detail": "阿岚（WorkBuddy）：凭据将在 1 天后到期",
            "evidence": {"expiresAt": _iso(1)},
            "actions": [
                {"id": "refresh", "kind": "navigate", "label": "去号池处理", "to": "/accounts"}
            ],
            "firstSeenAt": _iso(0, -9),
            "lastSeenAt": _iso(0, -1),
            "stateChangedAt": _iso(0, -9),
            "snoozedUntil": None,
            "resolvedAt": None,
        },
        {
            "id": "demo-health-2",
            "kind": "activity_failed",
            "severity": "warn",
            "state": "open",
            "scope": "workbuddy-cn",
            "accountId": "acc-demo-1",
            "title": "活动执行失败",
            "detail": "林间（WorkBuddy）：每日签到未完成",
            "evidence": {"activityKey": "workbuddy.daily_checkin"},
            "actions": [
                {"id": "rerun", "kind": "navigate", "label": "去活动页", "to": "/activity"}
            ],
            "firstSeenAt": _iso(0, -5),
            "lastSeenAt": _iso(0, -1),
            "stateChangedAt": _iso(0, -5),
            "snoozedUntil": None,
            "resolvedAt": None,
        },
    ]

    overview_items = [
        {
            "kind": "credential_expiring",
            "severity": "warn",
            "title": "凭据即将到期",
            "detail": "阿岚（WorkBuddy）：凭据将在 1 天后到期",
            "accountId": "acc-demo-2",
            "action": {
                "type": "run",
                "endpoint": "/credentials/refresh",
                "accountIds": ["acc-demo-2"],
                "mode": "due",
                "scope": "workbuddy-cn",
            },
        },
        {
            "kind": "activity_failed",
            "severity": "warn",
            "title": "活动执行失败",
            "detail": "林间（WorkBuddy）：每日签到未完成",
            "accountId": "acc-demo-1",
            "action": {
                "type": "run",
                "endpoint": "/activities/run",
                "accountIds": ["acc-demo-1", "acc-demo-3"],
                "mode": "due",
                "scope": "workbuddy-cn",
            },
        },
    ]

    detection = {
        "clients": [
            {
                "scope": "workbuddy-cn",
                "platform": "workbuddy",
                "label": "WorkBuddy 国内版",
                "installed": True,
                "running": True,
                "loggedIn": True,
                "stateDir": r"C:\Users\demo\AppData\Local\CodeBuddyExtension\Data\Public\auth",
                "appPath": r"C:\Program Files\WorkBuddy\WorkBuddy.exe",
                "message": "",
                "currentAccountId": "demo-wb-1",
                "downloadUrl": "https://www.workbuddy.cn/",
                "slots": [
                    {
                        "key": "desktop",
                        "label": "桌面端",
                        "writable": True,
                        "installed": True,
                        "stateFile": r"C:\Users\demo\AppData\Local\CodeBuddyExtension"
                        r"\Data\Public\auth\workbuddy-desktop.info",
                        "appPath": r"C:\Program Files\WorkBuddy\WorkBuddy.exe",
                        "reason": "",
                    }
                ],
                "localPorts": {"available": True, "ports": [], "reason": ""},
            },
            {
                "scope": "trae-cn",
                "platform": "trae",
                "label": "Trae 国内版",
                "installed": True,
                "running": True,
                "loggedIn": True,
                "stateDir": r"C:\Users\demo\AppData\Roaming\Trae CN\User\globalStorage",
                "appPath": r"C:\Program Files\Trae CN\Trae CN.exe",
                "message": "",
                "currentAccountId": "demo-trae-1",
                "downloadUrl": "https://www.trae.cn/",
                "slots": [
                    {
                        "key": "ide",
                        "label": "Trae IDE",
                        "writable": True,
                        "installed": True,
                        "stateFile": r"C:\Users\demo\AppData\Roaming\Trae CN"
                        r"\User\globalStorage\storage.json",
                        "appPath": r"C:\Program Files\Trae CN\Trae CN.exe",
                        "reason": "",
                    }
                ],
                "localPorts": {"available": True, "ports": [], "reason": ""},
            },
        ]
    }

    now_iso = _iso(0, -1)
    today = datetime.now(UTC).date()
    reward_series = [
        {"day": (today - timedelta(days=offset)).isoformat(), "reward": reward}
        for offset, reward in zip(
            range(6, -1, -1), (8.0, 14.0, 9.5, 16.0, 11.0, 18.0, 12.0), strict=True
        )
    ]
    return {
        "/api/accounts/merged": {"accounts": accounts},
        # /api/detect 返回单个客户端，/api/detection 返回 {clients}，两者不能共用同一份结构。
        "/api/detect": detection["clients"][0],
        "/api/detection": detection,
        "/api/preferences": {
            "migrateMemory": True,
            "migrateConnectors": True,
            "migrateSessions": True,
            "migrateProfile": True,
            "migrateWorkspaces": True,
            "migrateWebview": True,
            "sessionSyncEnabled": True,
            "autoBackup": True,
            "autoGlobalBackup": True,
            "autoGlobalParts": ["config"],
            "onboardingVersion": 3,
            "onboardingCompleted": [
                "install",
                "signin",
                "first_switch",
                "retention",
                "daily_run",
            ],
            "onboardingDismissedAt": now_iso,
            "firstSwitchAt": _iso(-20),
            "relaunchAfterSwitch": True,
            "localMetricsEnabled": False,
            "minHealthyAccounts": 2,
            "minCreditsDays": 3.0,
            "healthAutoRefresh": True,
            "healthAutoRerun": True,
            "isolationEnabled": True,
            "isolationThreshold": 3,
            "isolationHours": 24,
            "activityDeadlineDays": 3,
        },
        "/api/accounts/credits": {
            "credits": {
                "acc-demo-1": {
                    "available": True,
                    "credits": 760.5,
                    "total": 1200.0,
                    "expiresAt": None,
                    "expiringSoon": False,
                    "error": None,
                    "updatedAt": now_iso,
                    "subscription": "会员 Pro 连续包月",
                    "isPaid": True,
                    "packages": [],
                },
                "acc-demo-2": {
                    "available": True,
                    "credits": 86.0,
                    "total": 200.0,
                    "expiresAt": None,
                    "expiringSoon": True,
                    "error": None,
                    "updatedAt": now_iso,
                    "subscription": "体验版",
                    "isPaid": False,
                    "packages": [],
                },
                "acc-demo-3": {
                    "available": True,
                    "credits": 880.0,
                    "total": 2000.0,
                    "expiresAt": None,
                    "expiringSoon": False,
                    "error": None,
                    "updatedAt": now_iso,
                    "subscription": "会员 Pro 连续包月",
                    "isPaid": True,
                    "packages": [],
                },
                "acc-demo-4": {
                    "available": False,
                    "credits": None,
                    "total": None,
                    "expiresAt": None,
                    "expiringSoon": False,
                    "error": "登录态已失效，查不到积分",
                    "updatedAt": now_iso,
                    "subscription": None,
                    "isPaid": None,
                    "packages": [],
                },
            }
        },
        "/api/usage": {
            "summary": {
                "accounts": 4,
                "detectable": 2,
                "totalRequests": 740,
                "totalCreditConsumed": 1640.5,
                "workbuddyCreditConsumed": 760.5,
                "traeCreditsConsumed": 880.0,
            },
            "accounts": usage_accounts,
            "daily": _demo_daily(31, 12),
            "models": [
                {"model": "claude-sonnet-4.5", "requests": 610, "credit": 79.8},
                {"model": "gpt-5.1", "requests": 130, "credit": 54.6},
            ],
            "detail": [],
            "detailTruncated": False,
            "updatedAt": now_iso,
        },
        "/api/health/events": {
            "items": health_items,
            "summary": {
                "open": 2,
                "acked": 0,
                "snoozed": 0,
                "resolved": 0,
                "high": 0,
                "warn": 2,
                "info": 0,
                "unhandled": 2,
                "auto": {"ok24h": 5, "failed24h": 0, "lastAt": now_iso},
            },
            "checkedAt": now_iso,
        },
        "/api/overview/actionables": {
            "items": overview_items,
            "pulse": {
                "accounts": 4,
                "detectable": 2,
                "activityActionable": 2,
                "activityActionableAccounts": 2,
                "activityDone": 8,
                "activityTotal": 10,
                "credits": 1640,
                "creditsExpiring": 1,
                "credentialExpired": 1,
                "credentialExpiring": 1,
                "clientsRunning": 2,
                "gateway": {"requests": 1280, "errors": 3, "tokens": 42, "activeKeys": 2},
            },
            "campaigns": [],
        },
        "/api/activities/rewards": {
            "since": _iso(-7),
            "until": _iso(0),
            "days": 7,
            "total": 128.5,
            "today": 12.0,
            "daysSeries": reward_series,
            "byAccount": [
                {"accountId": "acc-demo-1", "displayName": "林间", "reward": 56.0},
                {"accountId": "acc-demo-3", "displayName": "周末", "reward": 44.5},
                {"accountId": "acc-demo-2", "displayName": "阿岚", "reward": 28.0},
            ],
            "byKey": [
                {"key": "workbuddy.daily_checkin", "title": "每日签到", "reward": 48.0},
                {"key": "workbuddy.travel", "title": "派猫猫旅行", "reward": 42.5},
                {"key": "trae.daily_checkin", "title": "每日签到", "reward": 38.0},
            ],
        },
        "/api/pool/health": {
            "healthyAccounts": 3,
            "accounts": 4,
            "expiring24h": 1,
            "cooling": 1,
            "creditsDays": 12.5,
            "successRate24h": 0.98,
            "thresholds": {"minHealthyAccounts": 2, "minCreditsDays": 3.0},
            "alerts": [],
            "checkedAt": now_iso,
        },
        "/api/compat": {
            "items": [
                {
                    "platform": "workbuddy",
                    "scope": "workbuddy-cn",
                    "state": "compatible",
                    "detail": "",
                    "current": None,
                    "latest": {
                        "platform": "workbuddy",
                        "scope": "workbuddy-cn",
                        "clientVersion": "2.0.0-demo",
                        "fingerprint": "a1b2c3d4e5f60718",
                        "statePathsOk": True,
                        "missingPaths": [],
                        "firstSeenAt": now_iso,
                        "lastSeenAt": now_iso,
                        "confirmedAt": now_iso,
                    },
                    "history": [],
                },
                {
                    "platform": "trae",
                    "scope": "trae-cn",
                    "state": "compatible",
                    "detail": "",
                    "current": None,
                    "latest": {
                        "platform": "trae",
                        "scope": "trae-cn",
                        "clientVersion": "2.0.0-demo",
                        "fingerprint": "b1c2d3e4f5061728",
                        "statePathsOk": True,
                        "missingPaths": [],
                        "firstSeenAt": now_iso,
                        "lastSeenAt": now_iso,
                        "confirmedAt": now_iso,
                    },
                    "history": [],
                },
            ],
            "probed": True,
            "deduped": False,
            "checkedAt": now_iso,
            "changed": [],
        },
    }


def install_demo_routes(page, fixtures: dict[str, dict[str, object]]) -> None:
    """把演示数据挂到页面上；未覆盖的接口继续走真实后端（空数据实例）。"""

    def handler(route) -> None:
        payload = fixtures.get(urlparse(route.request.url).path)
        if payload is None:
            route.continue_()
            return
        route.fulfill(
            status=200,
            content_type="application/json; charset=utf-8",
            body=json.dumps(payload, ensure_ascii=False),
        )

    page.route("**/api/**", handler)


# 脱敏脚本：在页面里跑，把敏感文本换成圆点、把首字母头像糊掉。
REDACT_JS = r"""
(payload) => {
  const explicit = (payload.explicit || []).filter(Boolean)
    .sort((a, b) => b.length - a.length);

  const dots = (s) => "\u2022".repeat(Math.min(Math.max(s.length, 3), 24));

  // 首字母头像：圆形小色块里只有一个字，模糊掉
  const blurStyle = document.createElement("style");
  blurStyle.textContent = ".__shot-blur{filter:blur(7px)!important;}";
  document.head.appendChild(blurStyle);

  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      const parent = node.parentElement;
      if (!parent) return NodeFilter.FILTER_REJECT;
      const tag = parent.tagName;
      if (tag === "SCRIPT" || tag === "STYLE" || tag === "TEXTAREA") {
        return NodeFilter.FILTER_REJECT;
      }
      return node.nodeValue.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });

  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);

  let replaced = 0;
  for (const node of nodes) {
    const before = node.nodeValue;
    let text = before;

    for (const secret of explicit) {
      if (secret && text.includes(secret)) text = text.split(secret).join(dots(secret));
    }
    // 路径里的用户名：C:\Users\xxxx / /Users/xxxx / /home/xxxx
    text = text.replace(/([A-Za-z]:\\Users\\)([^\\\s]+)/g,
      (m, head) => head + "\u2022\u2022\u2022\u2022\u2022");
    text = text.replace(/(\/Users\/)([^\/\s]+)/g,
      (m, head) => head + "\u2022\u2022\u2022\u2022\u2022");
    text = text.replace(/(\/home\/)([^\/\s]+)/g,
      (m, head) => head + "\u2022\u2022\u2022\u2022\u2022");
    // 邮箱
    text = text.replace(/[\w.+-]+@[\w-]+\.[\w.-]+/g, (m) => dots(m));
    // 网关密钥：界面上本来就是掩码形态（sk-e5a52c••••e8c2），可见的前缀与尾部
    // 都是真密钥的一部分，所以连同中间的圆点一起整段抹掉。
    text = text.replace(/\bsk-[A-Za-z0-9_\u2022-]{4,}/g, "sk-\u2022\u2022\u2022\u2022\u2022\u2022");
    // IPv4：回环地址不脱敏（服务地址写 127.0.0.1 是设计的一部分，抹了就成乱码了），
    // 内网段 192.168.x / 10.x 反而要抹——那是真实的内网拓扑。
    text = text.replace(/\b\d{1,3}(?:\.\d{1,3}){3}\b/g, (m) =>
      m.startsWith("127.") || m === "0.0.0.0" ? m : dots(m));
    // 长十六进制串：会话 ID / machineId / 摘要
    text = text.replace(/\b[0-9a-f]{16,}\b/gi, (m) => dots(m));
    // 6 位以上连续数字：账号 ID / 手机号
    text = text.replace(/\b\d{6,}\b/g, (m) => dots(m));

    if (text !== before) {
      node.nodeValue = text;
      replaced += 1;
    }
  }

  // 圆形首字母头像（一个字的方块，宽高接近、圆角够大）
  let blurred = 0;
  for (const el of document.querySelectorAll("body *")) {
    if (el.children.length > 0) continue;
    const label = (el.textContent || "").trim();
    if (!label || label.length > 2) continue;
    const box = el.getBoundingClientRect();
    if (box.width < 22 || box.width > 60) continue;
    if (Math.abs(box.width - box.height) > 4) continue;
    const radius = parseFloat(getComputedStyle(el).borderRadius) || 0;
    if (radius < 8) continue;
    el.classList.add("__shot-blur");
    blurred += 1;
  }

  // 底部渐隐：页面比视口长时，硬切会在卡片中间截断，加一层背景渐变收尾
  const rgba = (value, alpha) => {
    const probe = document.createElement("canvas").getContext("2d");
    probe.fillStyle = value;
    const hex = probe.fillStyle;
    if (hex.startsWith("#")) {
      const n = parseInt(hex.slice(1), 16);
      return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${alpha})`;
    }
    return hex.replace(/rgba?\(([^)]+)\)/, (m, inner) => {
      const parts = inner.split(",").slice(0, 3).map((s) => s.trim());
      return `rgba(${parts.join(",")},${alpha})`;
    });
  };
  const bg = getComputedStyle(document.body).backgroundColor;
  const sidebar = document.querySelector("aside");
  const left = sidebar ? Math.round(sidebar.getBoundingClientRect().right) : 0;
  const fade = document.createElement("div");
  fade.style.cssText = [
    "position:fixed", "right:0", "bottom:0", "height:72px", "z-index:2147483000",
    "pointer-events:none", `left:${left}px`,
    `background:linear-gradient(to bottom, ${rgba(bg, 0)} 0%, ${rgba(bg, 0.92)} 62%, ${bg} 100%)`,
  ].join(";");
  document.body.appendChild(fade);

  return { replaced, blurred };
}
"""


def local_secrets() -> list[str]:
    """本机标识：家目录路径与用户名。

    环境页会把 `~/.chaierww`、客户端安装路径、日志目录整条打出来，
    光靠「替换 C:\\Users\\ 后面那段」不够——用户名还可能单独出现在别处
    （标题、路径中段、提示语）。所以把完整的家目录和用户名本身也当敏感词。
    """
    home = Path.home()
    user = home.name
    found = {str(home), home.as_posix(), str(home).replace("/", "\\")}
    if len(user) >= 4:  # 太短的用户名会把正常数字也一起打码
        found.add(user)
    return [item for item in found if item]


def collect_secrets(page) -> list[str]:
    """从接口里取真实账号字段，作为脱敏词表。

    直接读数据库或配置文件都不合适（截图脚本不该碰数据目录），
    走已经渲染出这些数据的同一个接口最省事。
    注意 `/api/accounts` 必须带 scope，所以先取 `/api/scopes`。
    """
    try:
        data = page.evaluate(
            """async () => {
                const getJson = async (url) => {
                    const res = await fetch(url, { credentials: 'include' });
                    return res.ok ? res.json() : null;
                };
                const scopeBody = await getJson('/api/scopes');
                const scopes = (scopeBody && scopeBody.scopes) || [];
                const out = [];
                for (const scope of scopes) {
                    if (!scope.implemented) continue;
                    const url = `/api/accounts?scope=${encodeURIComponent(scope.key)}`;
                    const body = await getJson(url);
                    const list = (body && (body.accounts || body.items)) || [];
                    for (const acc of list) {
                        for (const key of ['displayName', 'externalId', 'note']) {
                            const value = acc && acc[key];
                            if (typeof value === 'string' && value.trim().length >= 2) {
                                out.push(value.trim());
                            }
                        }
                    }
                }
                return out;
            }"""
        )
    except Exception as exc:  # noqa: BLE001
        print(f"  ! 取账号词表失败（继续用正则兜底）：{exc}")
        return []
    return data or []


def main() -> int:
    parser = argparse.ArgumentParser(description="chaierww 界面截图（含脱敏）")
    parser.add_argument("--base", default="http://127.0.0.1:57910", help="后端地址")
    parser.add_argument("--theme", default="light", choices=["light", "dark"], help="界面主题")
    parser.add_argument("--out", default=str(DOCS / "images"), help="输出目录")
    parser.add_argument("--only", default="", help="只截这些（逗号分隔的 name）")
    parser.add_argument("--width", type=int, default=1600, help="最终图片宽度")
    parser.add_argument("--chrome", default="", help="指定浏览器可执行文件（默认自动探测）")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="用内置演示数据替换账号 / 用量 / 健康 / 客户端接口（公开截图用）",
    )
    args = parser.parse_args()

    only = {s.strip() for s in args.only.split(",") if s.strip()}
    targets = [s for s in SHOTS if not only or s["name"] in only]
    if not targets:
        print(f"没有匹配的截图项：{args.only}")
        return 1

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    executable = args.chrome or find_chromium()
    if executable:
        print(f"浏览器：{executable}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=executable)
        try:
            for shot in targets:
                name = str(shot["name"])
                width, height = VIEWPORT_W, int(shot["height"])  # type: ignore[arg-type]
                page = browser.new_page(
                    viewport={"width": width, "height": height},
                    device_scale_factor=2,
                    locale="zh-CN",
                    color_scheme="dark" if args.theme == "dark" else "light",
                )
                # 主题 / 语言都存 localStorage，必须在首屏脚本前落一次。
                # 视图偏好（如号池卡片密度）存 sessionStorage，按需按张覆盖。
                init = (
                    f"localStorage.setItem('chaierww.theme', '{args.theme}');"
                    "localStorage.setItem('chaierww.locale', 'zh');"
                )
                for key, value in dict(shot.get("session") or {}).items():
                    init += f"sessionStorage.setItem('{key}', '{value}');"
                page.add_init_script(init)

                if args.demo:
                    install_demo_routes(page, demo_fixtures())

                url = f"{args.base.rstrip('/')}{shot['path']}"
                page.goto(url, wait_until="networkidle", timeout=45000)
                page.wait_for_timeout(2400)  # 等图表动画 / 骨架屏 / 积分查询结束

                # 演示数据本身是假的，不再从页面反采敏感词；本机路径仍要脱敏。
                secrets = ([] if args.demo else collect_secrets(page)) + local_secrets()
                stats = page.evaluate(REDACT_JS, {"explicit": secrets})
                page.wait_for_timeout(120)

                raw = out_dir / f".raw-{name}.png"
                page.screenshot(path=str(raw))

                # 2x 截图缩到目标宽度，边缘更干净、文件也小
                with Image.open(raw) as img:
                    target_h = round(img.height * args.width / img.width)
                    img.resize((args.width, target_h), Image.LANCZOS).save(
                        out_dir / f"{name}.png", "PNG", optimize=True
                    )
                raw.unlink(missing_ok=True)

                size_kb = (out_dir / f"{name}.png").stat().st_size // 1024
                print(
                    f"  {name:16s} {args.width}x{target_h}  "
                    f"{size_kb:4d} KB  脱敏 {stats['replaced']} 处 / 头像 {stats['blurred']} 个"
                )
                page.close()
        finally:
            browser.close()

    print(f"完成：{len(targets)} 张 -> {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
