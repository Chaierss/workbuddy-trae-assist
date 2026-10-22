# -*- coding: utf-8 -*-
"""用 Windows Restart Manager API 查出到底是哪个进程锁住了文件。

用法：
    python scripts/who_locks.py <文件路径>

原理：RmStartSession + RmRegisterResources + RmGetList 是 Windows 官方的
「谁占用了这个文件」查询接口，比遍历进程句柄可靠得多。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

CCH_SESSION_KEY = 33
ERROR_SUCCESS = 0
ERROR_MORE_DATA = 234

RmRebootReason = ctypes.c_int
RmUniqueProcess = ctypes.c_void_p


class RM_UNIQUE_PROCESS(ctypes.Structure):
    _fields_ = [
        ("dwProcessId", wintypes.DWORD),
        ("ProcessStartTime", wintypes.FILETIME),
    ]


class RM_PROCESS_INFO(ctypes.Structure):
    _fields_ = [
        ("Process", RM_UNIQUE_PROCESS),
        ("strAppName", wintypes.WCHAR * 256),
        ("strServiceShortName", wintypes.WCHAR * 64),
        ("ApplicationType", ctypes.c_int),
        ("AppStatus", wintypes.ULONG),
        ("TSSessionId", wintypes.DWORD),
        ("bRestartable", wintypes.BOOL),
    ]


def who_locks(path: str) -> list[tuple[int, str]]:
    rstrtmgr = ctypes.WinDLL("rstrtmgr")
    session = wintypes.DWORD()
    key = ctypes.create_unicode_buffer(CCH_SESSION_KEY)
    if rstrtmgr.RmStartSession(ctypes.byref(session), 0, key) != ERROR_SUCCESS:
        raise RuntimeError("RmStartSession 失败")

    try:
        files = (wintypes.LPCWSTR * 1)(path)
        if rstrtmgr.RmRegisterResources(
            session, 1, files, 0, None, 0, None
        ) != ERROR_SUCCESS:
            raise RuntimeError("RmRegisterResources 失败")

        needed = wintypes.UINT(0)
        count = wintypes.UINT(0)
        reasons = wintypes.DWORD(0)
        # 先问需要多少
        r = rstrtmgr.RmGetList(
            session, ctypes.byref(needed), ctypes.byref(count), None,
            ctypes.byref(reasons),
        )
        if r == ERROR_SUCCESS and needed.value == 0:
            return []
        if r != ERROR_MORE_DATA and r != ERROR_SUCCESS:
            raise RuntimeError(f"RmGetList 失败 rc={r}")

        arr = (RM_PROCESS_INFO * needed.value)()
        count = wintypes.UINT(needed.value)
        r = rstrtmgr.RmGetList(
            session, ctypes.byref(needed), ctypes.byref(count), arr,
            ctypes.byref(reasons),
        )
        if r != ERROR_SUCCESS:
            raise RuntimeError(f"RmGetList(2) 失败 rc={r}")

        out: list[tuple[int, str]] = []
        for i in range(count.value):
            info = arr[i]
            out.append((int(info.Process.dwProcessId), str(info.strAppName)))
        return out
    finally:
        rstrtmgr.RmEndSession(session)


def main() -> None:
    if len(sys.argv) < 2:
        print("用法: python scripts/who_locks.py <文件路径>")
        raise SystemExit(2)
    target = sys.argv[1]
    holders = who_locks(target)
    if not holders:
        print(f"没有任何进程锁定：{target}")
        return
    print(f"锁定 {target} 的进程：")
    import psutil

    for pid, app in holders:
        detail = app
        try:
            p = psutil.Process(pid)
            detail = f"{p.name()} | {' '.join(p.cmdline())[:120]}"
        except Exception:
            pass
        print(f"  PID {pid}: {detail}")


if __name__ == "__main__":
    main()
