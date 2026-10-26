"""启动失败的可读落盘：``startup-error.json``。

后端在 ``create_app`` 阶段失败时（数据库损坏、端口被占、配置无法读取……），
进程往往来不及写日志就退出。桌面端只看到一个「后端异常退出」，用户拿不到原因。
这里把异常类型、脱敏后的消息与处理建议写成固定 JSON，桌面端启动失败分支读取并展示。
"""

from __future__ import annotations

import contextlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from chaierww.infra.fsutil import write_json_atomic

FILENAME = "startup-error.json"
_MAX_MESSAGE = 500

#: 只要看到这些形态就当凭据处理，一律替换成 ``***``。
_SECRET_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"(?i)(sk-)[A-Za-z0-9]{8,}"),
    re.compile(r"(?i)((?:token|secret|password|api[_-]?key)\s*[=:]\s*)\S+"),
)


def scrub(message: str) -> str:
    """把明显的凭据形态替换掉，并截断长度。"""
    value = message.strip()
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(lambda match: match.group(1) + "***", value)
    if len(value) > _MAX_MESSAGE:
        value = value[: _MAX_MESSAGE - 1] + "…"
    return value


def write_startup_error(home: Path, exc: BaseException) -> Path:
    path = home / FILENAME
    payload = {
        "at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "type": type(exc).__name__,
        "message": scrub(str(exc)),
        "hint": (
            "可查看 logs/ 下的运行日志；若数据库损坏，"
            "可从 backups/global/<id>/chaierww.db 恢复后重启。"
        ),
    }
    write_json_atomic(path, payload)
    return path


def clear_startup_error(home: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        (home / FILENAME).unlink()


def read_startup_error(home: Path) -> dict[str, object] | None:
    path = home / FILENAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None
