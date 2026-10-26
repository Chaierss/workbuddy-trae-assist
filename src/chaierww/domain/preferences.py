"""应用级偏好（设置 → 常规）。

落在数据目录的 preferences.json，随号池数据同进退（备份 home 即全备份）。
字段必须是白名单内的：多给的键直接丢弃，避免前端把任意内容写进文件。

这里只剩「切换时迁移哪些数据」这一组开关，外加两个自动备份开关。**账号保活 /
自动签到**曾经也放在本文件里，但那是「设置 → 计划任务」里同名任务
（`keepalive` / `checkin`）的第二套开关，两处互为镜像、说法还不一定一致，
已统一收敛到调度器的 `schedules.json`。老文件里遗留的 `keepalive` /
`autoCheckin` 键会被白名单直接丢弃，不需要迁移脚本。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chaierww.infra.fsutil import read_json_or_default, write_json_atomic

logger = logging.getLogger("chaierww")

FILENAME = "preferences.json"

#: 首启引导的步骤 id。前端据此推进清单，后端只负责持久化与白名单校验。
#: ``retention`` 是可选项（开自动签到 / 签发网关 Key），前 3 步完成后才出现。
ONBOARDING_STEPS: tuple[str, ...] = (
    "install",
    "signin",
    "first_switch",
    "retention",
)

#: 自动备份的默认分区，与 backup.DEFAULT_GLOBAL_PARTS 保持一致（只有不含凭据的 config）。
#: 这里用字面量而不是从 backup 导入，是为了避开 backup↔preferences 的循环导入：
#: backup 只需要本文件的 FILENAME，本文件又需要 backup 的分区表 —— 白名单在
#: `clean_global_parts` 里做延迟导入解决。
_DEFAULT_PARTS: tuple[str, ...] = ("config",)

DEFAULTS: dict[str, Any] = {
    # 切换时迁移哪些数据（记忆 / 连接器 / 会话 / 档案 / 工作区），默认全开
    "migrateMemory": True,
    "migrateConnectors": True,
    "migrateSessions": True,
    "migrateProfile": True,
    "migrateWorkspaces": True,
    # 切换时是否把「网页版登录态」（按账号隔离的 Chromium 分区）一起搬。
    # 不搬的话，切到没在本机网页版登录过的号，桌面端能用、网页版没有登录态。
    "migrateWebview": True,
    # 会话镜像的后台采集（设置 → 会话同步）。默认开：只读采集，不写客户端，
    # 关掉的话只有切号那一刻才会镜像一次。
    "sessionSyncEnabled": True,
    # 每日自动备份，默认开启；每个客户端只留最近 7 份快照。
    # 设置页的开关已随「客户端数据快照」一起下线：恢复入口没了，再让用户关掉
    # 只是关掉一张看不见的安全网。所以读文件时**忽略存量的 false**，恒为 True。
    "autoBackup": True,
    # 每日自动做一份「全局备份」（默认分区 = 不含凭据，保留最近 7 份）。
    # 设置 → 备份 页「全局备份」卡片右上角那个开关就是它。
    "autoGlobalBackup": True,
    # 自动备份收录哪些分区。默认只有 config（不含凭据）。
    # 用户手动勾选含凭据分区（gateway / accounts）并确认后，这里跟着更新，
    # 之后自动备份也把它们纳入 —— 「勾选即确认」，不再每次弹二次确认。
    "autoGlobalParts": list(_DEFAULT_PARTS),
    # ---------- 首启引导（0.8.0） ----------
    #: 引导状态机的版本。0 = 还没判定过；首次读取时由前端/后端据号池判定：
    #: 有账号 = 老用户（直接视为完成），无账号 = 新用户（展示清单）。
    "onboardingVersion": 0,
    #: 已完成的步骤 id（白名单内，去重保序）。
    "onboardingCompleted": [],
    #: 用户点过「跳过 / 收起」的时间；非空表示不再自动展示。
    "onboardingDismissedAt": None,
    #: 第一次成功切换的时间（只写一次）。首次换号率的唯一本地依据。
    "firstSwitchAt": None,
    # ---------- 本地体验数据（0.8.0） ----------
    #: 本地漏斗事件开关。只写本机 NDJSON，不上传；关掉后不再产生新事件。
    "localMetricsEnabled": True,
}


@dataclass(frozen=True, slots=True)
class Preferences:
    migrate_memory: bool = True
    migrate_connectors: bool = True
    migrate_sessions: bool = True
    migrate_profile: bool = True
    migrate_workspaces: bool = True
    migrate_webview: bool = True
    session_sync_enabled: bool = True
    auto_backup: bool = True
    auto_global_backup: bool = True
    auto_global_parts: tuple[str, ...] = _DEFAULT_PARTS
    onboarding_version: int = 0
    onboarding_completed: tuple[str, ...] = ()
    onboarding_dismissed_at: str | None = None
    first_switch_at: str | None = None
    local_metrics_enabled: bool = True

    def payload(self) -> dict[str, Any]:
        return {
            "migrateMemory": self.migrate_memory,
            "migrateConnectors": self.migrate_connectors,
            "migrateSessions": self.migrate_sessions,
            "migrateProfile": self.migrate_profile,
            "migrateWorkspaces": self.migrate_workspaces,
            "migrateWebview": self.migrate_webview,
            "sessionSyncEnabled": self.session_sync_enabled,
            "autoBackup": self.auto_backup,
            "autoGlobalBackup": self.auto_global_backup,
            "autoGlobalParts": list(self.auto_global_parts),
            "onboardingVersion": self.onboarding_version,
            "onboardingCompleted": list(self.onboarding_completed),
            "onboardingDismissedAt": self.onboarding_dismissed_at,
            "firstSwitchAt": self.first_switch_at,
            "localMetricsEnabled": self.local_metrics_enabled,
        }


class PreferencesStore:
    def __init__(self, home: Path) -> None:
        self._file = home / FILENAME

    def load(self) -> Preferences:
        if not self._file.is_file():
            return Preferences()
        data = read_json_or_default(self._file, None, logger=logger)
        if not isinstance(data, dict):
            return Preferences()

        def flag(key: str) -> bool:
            return bool(data.get(key, DEFAULTS[key]))

        return Preferences(
            migrate_memory=flag("migrateMemory"),
            migrate_connectors=flag("migrateConnectors"),
            migrate_sessions=flag("migrateSessions"),
            migrate_profile=flag("migrateProfile"),
            migrate_workspaces=flag("migrateWorkspaces"),
            migrate_webview=flag("migrateWebview"),
            session_sync_enabled=flag("sessionSyncEnabled"),
            # 故意不走 flag()：存量文件里可能是 false，但现在没有入口能改回来，
            # 当成 True 处理才是对用户有利的方向（这是迁移，不是丢数据）。
            auto_backup=True,
            auto_global_backup=flag("autoGlobalBackup"),
            auto_global_parts=clean_global_parts(data.get("autoGlobalParts")),
            onboarding_version=clean_version(data.get("onboardingVersion")),
            onboarding_completed=clean_onboarding_steps(data.get("onboardingCompleted")),
            onboarding_dismissed_at=_optional_iso(data.get("onboardingDismissedAt")),
            first_switch_at=_optional_iso(data.get("firstSwitchAt")),
            local_metrics_enabled=flag("localMetricsEnabled"),
        )

    def mark_first_switch(self, at: str | None = None) -> bool:
        """记录「第一次成功切换」的时间；已有值时不覆盖。

        返回是否真的写入。切换流程成功后会调用一次，重复调用无副作用。
        """
        current = self.load()
        if current.first_switch_at:
            return False
        stamp = at or datetime.now(UTC).astimezone().isoformat(timespec="seconds")
        self.save(replace(current, first_switch_at=stamp))
        return True

    def save(self, prefs: Preferences) -> Preferences:
        write_json_atomic(self._file, prefs.payload())
        return prefs


def clean_global_parts(raw: Any) -> tuple[str, ...]:
    """把 `autoGlobalParts` 收敛成可信的分区元组。

    只认白名单内的分区 key，未知/非法的直接丢弃；结果去重且保序。
    空列表（非法）回落到默认的 config，保证自动备份永远不会空跑。

    白名单来自 backup 模块，这里延迟导入避免模块加载期的循环依赖。
    """
    from chaierww.domain.backup import DEFAULT_GLOBAL_PARTS, GLOBAL_PARTS

    if not isinstance(raw, list):
        return DEFAULT_GLOBAL_PARTS
    cleaned = tuple(dict.fromkeys(item for item in raw if item in GLOBAL_PARTS))
    return cleaned if cleaned else DEFAULT_GLOBAL_PARTS


def clean_onboarding_steps(raw: Any) -> tuple[str, ...]:
    """只保留白名单内的步骤 id，去重且保序；非法输入当作「没有完成任何一步」。"""
    if not isinstance(raw, list):
        return ()
    return tuple(dict.fromkeys(item for item in raw if item in ONBOARDING_STEPS))


def clean_version(raw: Any) -> int:
    """引导版本号：只接受非负整数，其余一律按 0（未初始化）处理。"""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def _optional_iso(raw: Any) -> str | None:
    """ISO 时间字符串原样保留，其余（数字/空串/对象）当作 null。"""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None
