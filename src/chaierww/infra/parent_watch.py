"""盯着父进程：父进程没了，后端自己退出。

为什么需要这个 —— 桌面端重装时的「chaierww 无法关闭」就是它引起的：

1. Electron 主进程被安装程序 taskkill 掉时（`taskkill /f` 不给清理机会），
   或者主进程直接崩溃，`will-quit` 里的清理根本来不及跑。
2. 后端是 PyInstaller `--onefile` 打的：引导器 + 真正干活的子进程两层。
   把引导器干掉，子进程会变成孤儿继续活着。
3. 孤儿后端还占着 `resources/backend/chaierww-backend.exe`（文件被锁）和
   57910 端口。安装程序要覆盖这个 exe，7z 解压失败，于是报「chaierww 无法关闭」，
   而它重试时只查 `chaierww.exe`（早就没了），所以永远重试不成功。

主进程通过环境变量 `CHAIERWW_PARENT_PID` 把 PID 传下来，这里起一个守护线程
每秒探一次。没设这个变量（比如命令行直接 `python -m chaierww.main`）就什么都不做。
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time

logger = logging.getLogger("chaierww.parent_watch")

PARENT_PID_ENV = "CHAIERWW_PARENT_PID"
_POLL_SECONDS = 1.0


def _alive(pid: int) -> bool:
    """进程是否还活着。

    Windows 上不能用 `os.kill(pid, 0)`——CPython 在 Windows 上会把它翻译成
    TerminateProcess，等于「探测即击杀」。所以走 OpenProcess + GetExitCodeProcess。
    """
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == still_active
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # 存在但不属于当前用户，算活着
        return True
    return True


def watch_parent(pid: int | None = None) -> bool:
    """起守护线程盯父进程。返回是否真的盯上了。"""
    if pid is None:
        raw = os.environ.get(PARENT_PID_ENV, "").strip()
        if not raw:
            return False
        try:
            pid = int(raw)
        except ValueError:
            logger.warning("环境变量 %s 不是合法 PID：%r", PARENT_PID_ENV, raw)
            return False

    if pid <= 0 or not _alive(pid):
        # 父进程已经不在了，没必要再跑
        logger.warning("父进程 %s 不存在，后端不启动", pid)
        return False

    def _loop() -> None:
        while True:
            time.sleep(_POLL_SECONDS)
            if _alive(pid):
                continue
            logger.warning("父进程 %s 已退出，后端随之关闭", pid)
            _die()

    threading.Thread(target=_loop, name="parent-watch", daemon=True).start()
    logger.info("已开始监视父进程 %s", pid)
    return True


def _die() -> None:
    """立刻退出。不走 atexit：uvicorn 的收尾可能又慢又爱报错，这里要的是「马上放开文件锁」。"""
    try:
        logging.shutdown()
    finally:
        os._exit(0)
