"""平台适配器。

每个平台把"客户端把登录态存在哪、怎么换"封装起来，领域层只依赖这里的协议。
新增一个平台 = 新增一个 adapter，不修改切换引擎。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

# 会污染目标 Electron 客户端的 PATH 片段。当本服务运行在 WorkBuddy 等另一个
# Electron 宿主派生的环境里时，PATH 会被塞进这些 shim / 托管运行时路径，且排在
# 最前面：目标客户端（Trae / WorkBuddy 自身）启动时，其渲染进程与 AI 子进程会
# 用这份 PATH 去解析 `node`/`npm` 等命令，拉到 WorkBuddy 的 shim（safe-bin），
# 导致渲染进程崩溃（renderer process gone, code -2147483645）→ 整窗白屏。
# 所以启动目标客户端前必须把这些片段从 PATH 里剔除。
_POLLUTED_PATH_MARKERS = (
    "safe-bin",
    ".workbuddy",
    "app.asar.unpacked",
)


def sanitize_child_env(env: dict[str, str]) -> dict[str, str]:
    """构造一份「干净」的子进程环境：清除会污染目标 Electron 客户端的变量与 PATH。

    需要清除的变量：
    - ``ELECTRON_RUN_AS_NODE``：让目标客户端以 Node 模式启动而非 GUI；
    - ``VSCODE_DEV`` / ``NODE_OPTIONS``：强制目标客户端的 V8 加载宿主的 Node shim，
      引擎初始化失败（日志反复报「NO global context」）而秒退。

    需要净化的 PATH：剔除含 ``safe-bin`` / ``.workbuddy`` / ``app.asar.unpacked``
    的片段，避免目标客户端的渲染进程拉到被劫持的 node 而崩溃白屏。
    返回一个新的 dict，不修改入参。
    """
    out = dict(env)
    out.pop("ELECTRON_RUN_AS_NODE", None)
    out.pop("VSCODE_DEV", None)
    out.pop("NODE_OPTIONS", None)

    raw_path = out.get("PATH", "")
    if raw_path:
        sep = os.pathsep
        kept = [
            seg
            for seg in raw_path.split(sep)
            if seg and not any(marker in seg for marker in _POLLUTED_PATH_MARKERS)
        ]
        out["PATH"] = sep.join(kept)
    return out


class AdapterError(RuntimeError):
    """适配器层的可预期失败。消息面向用户，不暴露底层堆栈。"""


class NotInstalled(AdapterError):
    pass


class NotLoggedIn(AdapterError):
    pass


class StateUnreadable(AdapterError):
    pass


@dataclass(frozen=True, slots=True)
class Detection:
    installed: bool
    running: bool
    logged_in: bool
    state_dir: Path | None
    app_path: Path | None
    message: str = ""
    current_external_id: str | None = None


@dataclass(frozen=True, slots=True)
class Slot:
    """一个「程序位」：某个客户端宿主（桌面端 / IDE / CLI / 编辑器插件）。

    同一个账号可能被好几个宿主客户端读同一份登录态 —— 换号时要换的是**哪一个**
    宿主的登录态，就是这里的 `key`。

    只有 `writable=True` 的位置允许切换写入；其余位置只作探测展示，
    界面上按 `reason` 灰掉并写明原因（「未适配」与「未安装」是两回事：
    未适配的位置连状态文件路径都还没确认，所以 `state_file` 为空）。
    """

    key: str
    label: str
    writable: bool
    installed: bool
    state_file: Path | None = None
    app_path: Path | None = None
    reason: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "writable": self.writable,
            "installed": self.installed,
            "stateFile": str(self.state_file) if self.state_file else None,
            "appPath": str(self.app_path) if self.app_path else None,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ImportedAccount:
    external_id: str
    display_name: str
    payload: bytes
    detail: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlannedFile:
    path: Path
    action: str
    size: int
    note: str = ""


@dataclass(frozen=True, slots=True)
class SwitchPlan:
    """干跑结果：只描述将要发生什么，不产生任何修改。"""

    files: tuple[PlannedFile, ...]
    requires_app_exit: bool
    warnings: tuple[str, ...] = ()

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.files)


class PlatformAdapter(Protocol):
    scope: str

    def detect(self) -> Detection: ...

    def state_dir(self) -> Path: ...

    def import_current(self) -> ImportedAccount: ...

    def plan_switch(self, payload: bytes) -> SwitchPlan: ...

    def apply_switch(self, payload: bytes) -> None: ...

    def verify_switch(self, expected_external_id: str) -> bool: ...

    def is_running(self) -> bool: ...

    def launch(self) -> bool: ...

    def terminate(self) -> bool:
        """退出客户端进程（切换账号后重启前需要先退出，否则登录态不刷新）。

        默认不实现（返回 False），适配器可覆盖。退出失败不应阻断切换本身。
        """
        return False

    def wait_exit(self, timeout: float = 5.0) -> bool:
        """等待客户端进程真正退出（terminate 后进程不会立刻消失）。

        返回是否已退出。默认不等待（返回 False），适配器可覆盖。
        """
        return False

    # ---------- 可选：多程序位 ----------

    def slots(self) -> tuple[Slot, ...]:
        """本平台登记的「程序位」。默认空 = 该平台不区分程序位。"""
        return ()

    def slot_adapter(self, slot: str) -> PlatformAdapter:
        """把某个程序位解析成一个可写的适配器实例。

        只读 / 未适配的位置必须抛 `AdapterError`（消息面向用户），
        绝不返回一个「能写但写错地方」的适配器。
        """
        raise AdapterError(f"{self.scope} 不支持按程序位切换")

    # ---------- 可选：token 凭据切换（扫码采集的账号） ----------

    def plan_token_switch(self, account: dict) -> SwitchPlan:
        """扫码 token 账号的切换预演。默认不支持，返回报错由引擎转成可读错误。"""
        raise AdapterError(f"{self.scope} 暂不支持扫码账号切换客户端")

    def apply_token_switch(self, account: dict) -> None:
        """把扫码 token 账号写回客户端登录态。默认不支持。"""
        raise AdapterError(f"{self.scope} 暂不支持扫码账号切换客户端")
