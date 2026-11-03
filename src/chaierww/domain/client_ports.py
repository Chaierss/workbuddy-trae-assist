"""客户端本地端口：只读枚举，不连接、不探测协议、不发请求。

回答一个常见疑问：「客户端装好以后，它自己的 Web 端端口是不是不开放」。
本工具不依赖这些端口；这里只把「它到底有没有在监听」如实列出来。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import psutil

logger = logging.getLogger("chaierww")

_PERMISSION_REASON = "权限不足，无法枚举本地端口（不影响导入与切换）"


def _client_pids(matches_process: Callable[[str, str], bool]) -> set[int]:
    pids: set[int] = set()
    for proc in psutil.process_iter(["pid", "name", "exe"]):
        try:
            info = proc.info or {}
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        pid = info.get("pid")
        if pid is None:
            continue
        name = str(info.get("name") or "")
        exe = str(info.get("exe") or "")
        if matches_process(name, exe):
            pids.add(int(pid))
    return pids


def local_listening_ports(
    matches_process: Callable[[str, str], bool],
) -> dict[str, Any]:
    """返回 ``{available, ports, reason}``；任何异常都降级为「无法枚举」，绝不抛出。"""
    try:
        pids = _client_pids(matches_process)
    except (psutil.AccessDenied, PermissionError):
        return {"available": False, "ports": [], "reason": _PERMISSION_REASON}
    except psutil.Error as exc:
        logger.debug("枚举客户端进程失败：%s", exc, exc_info=True)
        return {
            "available": False,
            "ports": [],
            "reason": f"无法枚举客户端进程：{type(exc).__name__}",
        }

    if not pids:
        return {"available": True, "ports": [], "reason": "客户端未运行"}

    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        return {"available": False, "ports": [], "reason": _PERMISSION_REASON}
    except psutil.Error as exc:
        logger.debug("枚举本地端口失败：%s", exc, exc_info=True)
        return {
            "available": False,
            "ports": [],
            "reason": f"无法枚举本地端口：{type(exc).__name__}",
        }

    found: dict[int, str] = {}
    for conn in connections:
        if conn.pid not in pids or conn.status != psutil.CONN_LISTEN or not conn.laddr:
            continue
        port = int(conn.laddr.port)
        if port:
            found.setdefault(port, str(conn.laddr.ip))

    return {
        "available": True,
        "ports": [{"port": port, "address": addr} for port, addr in sorted(found.items())],
        "reason": "",
    }
