"""诊断包：健康度快照 + 可交给维护者的 zip。

硬约束：
- **绝不**收录凭据、Key、Cookie、账号外部 ID、对话正文；
- 计数只给数字，配置只给「是否存在 / 能否解析」；
- 日志沿用运行日志的脱敏结果，只取尾部片段，控制包体大小。
"""

from __future__ import annotations

import contextlib
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
from uuid import uuid4

from fastapi import FastAPI

from chaierww import __version__
from chaierww.domain.activities import OFFICIAL_CATALOG_VERSION
from chaierww.domain.preferences import FILENAME as PREFERENCES_FILENAME
from chaierww.domain.scheduler import FILENAME as SCHEDULES_FILENAME
from chaierww.infra.db import quick_check
from chaierww.infra.fsutil import scan_corrupt_files
from chaierww.infra.startup import read_startup_error
from chaierww.logging_setup import redact_text

LOG_TAIL_BYTES = 128 * 1024
MAX_LOG_FILES = 10
#: 磁盘剩余低于 200 MB 就告警：SQLite 写入与备份都需要余量，撑满会直接损坏库。
DISK_WARN_BYTES = 200 * 1024 * 1024


def _writable(path: Path) -> bool:
    """真写一个探针文件再删掉。只问「能不能写」，不创建长期文件。"""
    probe = path / f".write-probe-{uuid4().hex[:8]}"
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        with contextlib.suppress(OSError):
            probe.unlink()
        return False


def _human_bytes(value: int | None) -> str:
    """磁盘余量给「12.4 GB」这种可读值；拿不到就留空，不编数字。"""
    if value is None:
        return ""
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return ""


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
    scheduler = getattr(app.state, "scheduler", None)
    try:
        usage = shutil.disk_usage(settings.home)
        free = usage.free
    except OSError:
        free = None
    return {
        "gatewayRunning": bool(gateway.running),
        "gatewayError": gateway.error,
        "schedulerRunning": bool(scheduler and scheduler.running),
        "diskFreeBytes": free,
    }


def _checks(
    settings: Any,
    database: dict[str, Any],
    config: dict[str, Any],
    runtime: dict[str, Any],
    startup_error: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """固定 8 项、固定顺序。标签由前端 i18n 负责，这里只给 id / level / value。"""
    quick = str(database.get("quickCheck") or "")
    corrupt = len(config.get("corruptFiles") or [])
    free = runtime.get("diskFreeBytes")
    return [
        {"id": "database", "level": "ok" if quick == "ok" else "error", "value": quick},
        {"id": "config", "level": "warn" if corrupt else "ok", "value": str(corrupt)},
        {
            "id": "data_dir",
            "level": "ok" if _writable(settings.home) else "error",
            "value": "",
        },
        {
            "id": "logs_dir",
            "level": "ok" if _writable(settings.logs_dir) else "error",
            "value": "",
        },
        {
            "id": "disk",
            "level": (
                "warn" if free is not None and free < DISK_WARN_BYTES else "ok"
            ),
            "value": _human_bytes(free),
        },
        {
            "id": "scheduler",
            "level": "ok" if runtime.get("schedulerRunning") else "warn",
            "value": "",
        },
        {
            "id": "gateway",
            "level": "error" if runtime.get("gatewayError") else "ok",
            "value": str(runtime.get("gatewayError") or ""),
        },
        {
            "id": "startup",
            "level": "warn" if startup_error else "ok",
            "value": str((startup_error or {}).get("type") or ""),
        },
    ]


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
    database = _db_stats(app)
    config = {
        "preferences": _file_state(settings.home / PREFERENCES_FILENAME),
        "schedules": _file_state(settings.home / SCHEDULES_FILENAME),
        "gateway": _file_state(settings.home / "gateway.json"),
        "gatewayKeys": _file_state(settings.home / "gateway-keys.json"),
        "gatewayPool": _file_state(settings.home / "gateway-pool.json"),
        "corruptFiles": [
            _display_path(item) for item in scan_corrupt_files(settings.home)
        ],
    }
    runtime = _runtime_stats(app)
    startup_error = read_startup_error(settings.home)
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
        "database": database,
        "config": config,
        "backups": _backup_stats(app),
        "runtime": runtime,
        "schedules": _schedule_summary(app),
        "preferences": _preferences_summary(app),
        "startupError": startup_error,
        "checks": _checks(settings, database, config, runtime, startup_error),
    }


def _log_tail(path: Path) -> bytes:
    size = path.stat().st_size
    with open(path, "rb") as handle:
        if size > LOG_TAIL_BYTES:
            handle.seek(size - LOG_TAIL_BYTES)
        raw = handle.read()
    # 旧日志是在写盘脱敏上线前写的，出包前必须再过一遍值级规则。
    text = raw.decode("utf-8", errors="replace")
    cleaned = "\n".join(redact_text(line) for line in text.splitlines())
    return cleaned.encode("utf-8")


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
            "不含凭据、API Key、Cookie、账号外部 ID 或对话正文。"
            "日志尾部按值级规则脱敏（Bearer/Key/邮箱/手机号/本机用户名）。\n",
        )
    return buffer.getvalue()
