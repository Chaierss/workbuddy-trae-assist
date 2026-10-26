"""诊断包：健康度快照 + 可交给维护者的 zip。

硬约束：
- **绝不**收录凭据、Key、Cookie、账号外部 ID、对话正文；
- 计数只给数字，配置只给「是否存在 / 能否解析」；
- 日志沿用运行日志的脱敏结果，只取尾部片段，控制包体大小。
"""

from __future__ import annotations

import io
import json
import platform
import shutil
import sys
import time
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI

from chaierww import __version__
from chaierww.domain.activities import OFFICIAL_CATALOG_VERSION
from chaierww.domain.preferences import FILENAME as PREFERENCES_FILENAME
from chaierww.domain.scheduler import FILENAME as SCHEDULES_FILENAME
from chaierww.infra.db import quick_check
from chaierww.infra.fsutil import scan_corrupt_files
from chaierww.infra.startup import read_startup_error

LOG_TAIL_BYTES = 128 * 1024
MAX_LOG_FILES = 10


def _display_path(path: Path) -> str:
    """把用户家目录替换成 `~`，避免诊断包泄露用户名。"""
    text = str(path)
    home = str(Path.home())
    return text.replace(home, "~") if home in text else text


def _file_state(path: Path) -> dict[str, Any]:
    """只回答「在不在、能不能解析」，不回传内容。"""
    state: dict[str, Any] = {"exists": path.is_file()}
    if not state["exists"]:
        return state
    state["bytes"] = path.stat().st_size
    try:
        json.loads(path.read_text(encoding="utf-8"))
        state["parsable"] = True
    except (OSError, ValueError):
        state["parsable"] = False
    return state


def _db_stats(app: FastAPI) -> dict[str, Any]:
    settings = app.state.settings
    db_path = settings.db_path
    stats: dict[str, Any] = {
        "path": _display_path(db_path),
        "bytes": db_path.stat().st_size if db_path.is_file() else 0,
        "walBytes": (
            db_path.with_name(db_path.name + "-wal").stat().st_size
            if db_path.with_name(db_path.name + "-wal").is_file()
            else 0
        ),
    }
    conn = getattr(app.state, "conn", None)
    if conn is None:
        stats["error"] = "连接不可用"
        return stats
    try:
        stats["userVersion"] = int(
            conn.execute("PRAGMA user_version").fetchone()[0]
        )
        stats["quickCheck"] = quick_check(conn)
        for table in ("accounts", "credentials", "snapshots", "activity_runs"):
            row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            stats[f"{table}Count"] = int(row[0]) if row else 0
    except Exception as exc:  # noqa: BLE001 - 诊断本身绝不能抛
        stats["error"] = type(exc).__name__
    return stats


def _backup_stats(app: FastAPI) -> dict[str, Any]:
    from chaierww.domain.scopes import all_scopes

    backups = app.state.backups
    settings = app.state.settings
    result: dict[str, Any] = {}
    try:
        global_latest = backups.list("global", limit=1)
        result["global"] = _snapshot_brief(global_latest[0]) if global_latest else None
        for scope in [item.key for item in all_scopes() if item.implemented]:
            latest = backups.list(scope, limit=1)
            result[scope] = _snapshot_brief(latest[0]) if latest else None
        result["preMigrateCount"] = (
            len(list(settings.pre_migrate_dir.glob("v*.db")))
            if settings.pre_migrate_dir.is_dir()
            else 0
        )
    except Exception as exc:  # noqa: BLE001
        result["error"] = type(exc).__name__
    return result


def _snapshot_brief(snapshot: Any) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "createdAt": snapshot.created_at,
        "reason": snapshot.reason,
        "bytes": snapshot.total_bytes,
        "files": snapshot.file_count,
        "verified": snapshot.verified,
    }


def _runtime_stats(app: FastAPI) -> dict[str, Any]:
    gateway = app.state.gateway_runtime
    settings = app.state.settings
    try:
        usage = shutil.disk_usage(settings.home)
        free = usage.free
    except OSError:
        free = None
    return {
        "gatewayRunning": bool(gateway.running),
        "gatewayError": gateway.error,
        "diskFreeBytes": free,
    }


def _schedule_summary(app: FastAPI) -> list[dict[str, Any]]:
    tasks = app.state.scheduler.list()
    return [
        {
            "key": task.key,
            "enabled": task.enabled,
            "times": list(task.times),
            "jitterMinutes": task.jitter_minutes,
            "accounts": len(task.account_ids),
            "activityKeys": len(task.activity_keys),
        }
        for task in tasks
    ]


def _preferences_summary(app: FastAPI) -> dict[str, Any]:
    prefs = app.state.preferences.load()
    return {
        "autoBackup": prefs.auto_backup,
        "autoGlobalBackup": prefs.auto_global_backup,
        "autoGlobalParts": list(prefs.auto_global_parts),
    }


def build_health(app: FastAPI) -> dict[str, Any]:
    settings = app.state.settings
    started_at = getattr(app.state, "started_at", time.monotonic())
    return {
        "generatedAt": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "app": {
            "version": __version__,
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "packaged": bool(getattr(sys, "frozen", False)),
            "dataDir": _display_path(settings.home),
            "uptimeSeconds": int(time.monotonic() - started_at),
            "activityCatalogVersion": OFFICIAL_CATALOG_VERSION,
        },
        "database": _db_stats(app),
        "config": {
            "preferences": _file_state(settings.home / PREFERENCES_FILENAME),
            "schedules": _file_state(settings.home / SCHEDULES_FILENAME),
            "gateway": _file_state(settings.home / "gateway.json"),
            "gatewayKeys": _file_state(settings.home / "gateway-keys.json"),
            "gatewayPool": _file_state(settings.home / "gateway-pool.json"),
            "corruptFiles": [
                _display_path(item)
                for item in scan_corrupt_files(settings.home)
            ],
        },
        "backups": _backup_stats(app),
        "runtime": _runtime_stats(app),
        "schedules": _schedule_summary(app),
        "preferences": _preferences_summary(app),
        "startupError": read_startup_error(settings.home),
    }


def _log_tail(path: Path) -> bytes:
    size = path.stat().st_size
    with open(path, "rb") as handle:
        if size > LOG_TAIL_BYTES:
            handle.seek(size - LOG_TAIL_BYTES)
        return handle.read()


def build_zip(app: FastAPI) -> bytes:
    health = build_health(app)
    settings = app.state.settings
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "report.json",
            json.dumps(health, ensure_ascii=False, indent=2) + "\n",
        )
        logs = sorted(settings.logs_dir.glob("*.log"))[-MAX_LOG_FILES:]
        for log in logs:
            try:
                archive.writestr(f"logs/{log.name}", _log_tail(log))
            except OSError:
                continue
        archive.writestr(
            "README.txt",
            "chaierww 诊断包：只含版本、计数、配置健康度与日志尾部；"
            "不含凭据、API Key、Cookie、账号外部 ID 或对话正文。\n",
        )
    return buffer.getvalue()
