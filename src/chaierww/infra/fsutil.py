"""配置型文件的可靠落盘：原子写 + 损坏文件隔离。

设计约束：
- 不引入第三方依赖；只用标准库。
- 原子写必须在**同一目录**内完成（``os.replace`` 跨盘不原子）。
- 损坏文件不删除、不覆盖，改名成 ``<name>.corrupt-<时间戳>`` 留证据，
  下一次保存仍然写回原路径 —— 用户配置不会因为一次半截文件就永久丢失。
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

_LOGGER = logging.getLogger("chaierww.fs")


def write_text_atomic(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """写文本：临时文件 → fsync → 替换。

    Windows 上杀毒/索引服务偶尔短暂占用目标文件，``os.replace`` 会抛
    ``PermissionError``；这里重试 3 次（50ms 间隔），仍失败则抛出并保留原文件。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w", encoding=encoding, newline="") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        for attempt in range(3):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.05)
    finally:
        # 成功替换后 tmp 已不存在；失败时清理，避免在数据目录里堆残片。
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def write_json_atomic(path: Path, data: Any) -> None:
    """JSON 原子写：固定 ``ensure_ascii=False, indent=2``，保持人可读与 diff 稳定。"""
    write_text_atomic(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def quarantine_corrupt(path: Path) -> Path | None:
    """把损坏文件改名隔离；返回隔离后的路径（失败返回 ``None``）。"""
    stamp = time.strftime("%Y%m%d%H%M%S")
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    index = 1
    while target.exists():
        target = path.with_name(f"{path.name}.corrupt-{stamp}-{index}")
        index += 1
    try:
        path.replace(target)
    except OSError:
        return None
    return target


def read_json_or_default(
    path: Path,
    default: T,
    *,
    logger: logging.Logger | None = None,
) -> T:
    """读 JSON；缺失返回默认值。

    - 解析失败 / 编码错误 → 隔离原文件、记 WARNING、返回默认值；
    - 其它 IO 错误 → 记 WARNING、**不改名**、返回默认值（可能只是暂时占用）。
    """
    log = logger or _LOGGER
    if not path.exists():
        return default
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        isolated = quarantine_corrupt(path)
        log.warning("配置文件编码异常，已隔离：%s → %s", path, isolated)
        return default
    except OSError as exc:
        log.warning("读取配置文件失败（保留原文件）：%s（%s）", path, exc)
        return default
    try:
        return json.loads(raw)  # type: ignore[no-any-return]
    except json.JSONDecodeError:
        isolated = quarantine_corrupt(path)
        log.warning("配置文件不是合法 JSON，已隔离：%s → %s", path, isolated)
        return default


def scan_corrupt_files(home: Path) -> list[Path]:
    """数据目录里被隔离的文件清单（供诊断包展示，最多列 50 个）。"""
    try:
        return sorted(home.glob("*.corrupt-*"))[:50]
    except OSError:
        return []
