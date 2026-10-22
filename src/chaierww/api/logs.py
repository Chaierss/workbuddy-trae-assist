"""运行日志查询：REST 分页，不做日志流。

三个硬要求（来自设置中枢三要素）：
1. **脱敏**：token / password / secret 的值在出参前统一打码，日志页搜不到明文凭据。
2. **绝对路径**：日志目录来自 `Settings.logs_dir`（基于数据目录推导），不依赖 CWD。
3. **前端筛选与后端能力对齐**：这里只提供真正支持的筛选，前端不摆空下拉框。

性能上按「日期倒序逐文件读、凑满一页即停」实现，不做全量扫描后再内存分页。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200
MAX_BYTES_PER_FILE = 4 * 1024 * 1024

_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

# 行首：2026-10-19 13:11:04 INFO    chaierww 消息
_LINE = re.compile(
    r"^(?P<at>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<logger>\S+)\s(?P<message>.*)$"
)

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    # Authorization: Bearer eyJhbGciOi... —— 先打码 token 本体
    (re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._\-+/=]{8,}"), r"\1 ***"),
    # token=abc / "access_token": "abc" / password: abc
    # `(?!bearer)` 是必须的：否则上面刚打好的「Bearer ***」会被当成
    # authorization 的值再打一遍，结果变成「*** ***」，排查时看不出原貌。
    (
        re.compile(
            r"(?i)\b(password|passwd|token|secret|api[_\-]?key|access[_\-]?token"
            r"|refresh[_\-]?token|credential|authorization)\b(\s*[=:]\s*)(?!bearer\b)([\"']?)"
            r"([^\s\"',}]{4,})"
        ),
        r"\1\2\3***",
    ),
    # 形如 {"$wbEncrypted":1,"envelope":"...."} 的整段密文
    (re.compile(r"(?i)(\"envelope\"\s*:\s*\")[^\"]{8,}(\")"), r"\1***\2"),
)


def redact_line(text: str) -> str:
    """把一行日志里可能存在的凭据打码。保留结构，方便排查。"""
    result = text
    for pattern, replacement in _REDACTIONS:
        result = pattern.sub(replacement, result)
    return result


def _log_dir(request: Request) -> Path:
    logs_dir: Path = request.app.state.settings.logs_dir
    if not logs_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"日志目录不存在：{logs_dir}")
    return logs_dir


def _log_files(logs_dir: Path) -> list[Path]:
    """按修改时间倒序：当前文件在最前，历史归档按日期跟上。"""
    files = [path for path in logs_dir.glob("*.log*") if path.is_file()]
    return sorted(files, key=lambda path: path.stat().st_mtime, reverse=True)


def _read_lines_tail(path: Path) -> list[str]:
    """只读文件尾部，避免历史日志过大时把内存吃满。"""
    size = path.stat().st_size
    with open(path, "rb") as handle:
        if size > MAX_BYTES_PER_FILE:
            handle.seek(size - MAX_BYTES_PER_FILE)
            handle.readline()  # 丢掉被截断的半行
        data = handle.read()
    return data.decode("utf-8", errors="replace").splitlines()


def _parse(line: str, name: str) -> dict[str, Any]:
    matched = _LINE.match(line)
    if matched is None:
        # 多行消息的续行、或非本格式的输出：保留原文，便于定位问题。
        return {
            "file": name,
            "at": None,
            "level": "",
            "logger": "",
            "message": redact_line(line),
        }
    return {
        "file": name,
        "at": matched.group("at"),
        "level": matched.group("level"),
        "logger": matched.group("logger"),
        "message": redact_line(matched.group("message")),
    }


def build_logs_router() -> APIRouter:
    router = APIRouter(prefix="/api/logs", tags=["日志"])

    @router.get("/files", summary="列出可查询的日志文件", tags=["日志"])
    def list_files(request: Request) -> dict[str, Any]:
        logs_dir = _log_dir(request)
        files = []
        for path in _log_files(logs_dir):
            stat = path.stat()
            files.append(
                {
                    "name": path.name,
                    "size": stat.st_size,
                    "updatedAt": datetime.fromtimestamp(stat.st_mtime, UTC)
                    .replace(microsecond=0)
                    .isoformat(),
                }
            )
        return {"dir": str(logs_dir), "files": files}

    @router.get("", summary="分页查询运行日志（已脱敏）", tags=["日志"])
    def query_logs(
        request: Request,
        page: int = Query(default=1, ge=1),
        page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
        level: str | None = None,
        keyword: str | None = None,
        file: str | None = None,
    ) -> dict[str, Any]:
        logs_dir = _log_dir(request)

        wanted_level = level.upper() if level else None
        if wanted_level and wanted_level not in _LEVELS:
            raise HTTPException(status_code=400, detail=f"未知日志级别：{level}")

        if file:
            # 只接受目录内的文件名，挡住 ../ 与绝对路径。
            if Path(file).name != file:
                raise HTTPException(status_code=400, detail="非法日志文件名")
            candidates = [logs_dir / file]
            if not candidates[0].is_file():
                raise HTTPException(status_code=404, detail=f"日志文件不存在：{file}")
        else:
            candidates = _log_files(logs_dir)

        offset = (page - 1) * page_size
        needle = keyword.lower() if keyword else None

        entries: list[dict[str, Any]] = []
        skipped = 0
        has_more = False

        for path in candidates:
            for raw in reversed(_read_lines_tail(path)):
                if not raw.strip():
                    continue
                parsed = _parse(raw, path.name)
                if wanted_level and parsed["level"] != wanted_level:
                    continue
                if needle and needle not in parsed["message"].lower():
                    continue

                if skipped < offset:
                    skipped += 1
                    continue
                if len(entries) >= page_size:
                    # 凑满一页就停：后面还有没有更多不需要全量扫描也能判断。
                    has_more = True
                    break
                entries.append(parsed)
            if has_more:
                break

        return {
            "page": page,
            "pageSize": page_size,
            "hasMore": has_more,
            "level": wanted_level,
            "keyword": keyword or "",
            "file": file or "",
            "entries": entries,
        }

    return router
