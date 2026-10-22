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

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

FILENAME = "preferences.json"

DEFAULTS: dict[str, Any] = {
    # 切换时迁移哪些数据（记忆 / 连接器 / 会话 / 档案 / 工作区），默认全开
    "migrateMemory": True,
    "migrateConnectors": True,
    "migrateSessions": True,
    "migrateProfile": True,
    "migrateWorkspaces": True,
    # 每日自动备份，默认开启；每个客户端只留最近 7 份快照。
    # 设置页的开关已随「客户端数据快照」一起下线，这里保留字段与默认值不动。
    "autoBackup": True,
    # 每日自动做一份「全局备份」（默认分区 = 不含凭据，保留最近 7 份）。
    # 设置 → 备份 页「全局备份」卡片右上角那个开关就是它。
    "autoGlobalBackup": True,
}


@dataclass(frozen=True, slots=True)
class Preferences:
    migrate_memory: bool = True
    migrate_connectors: bool = True
    migrate_sessions: bool = True
    migrate_profile: bool = True
    migrate_workspaces: bool = True
    auto_backup: bool = True
    auto_global_backup: bool = True

    def payload(self) -> dict[str, Any]:
        return {
            "migrateMemory": self.migrate_memory,
            "migrateConnectors": self.migrate_connectors,
            "migrateSessions": self.migrate_sessions,
            "migrateProfile": self.migrate_profile,
            "migrateWorkspaces": self.migrate_workspaces,
            "autoBackup": self.auto_backup,
            "autoGlobalBackup": self.auto_global_backup,
        }


class PreferencesStore:
    def __init__(self, home: Path) -> None:
        self._file = home / FILENAME

    def load(self) -> Preferences:
        if not self._file.is_file():
            return Preferences()
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return Preferences()
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
            auto_backup=flag("autoBackup"),
            auto_global_backup=flag("autoGlobalBackup"),
        )

    def save(self, prefs: Preferences) -> Preferences:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        self._file.write_text(
            json.dumps(prefs.payload(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return prefs
