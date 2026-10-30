"""WorkBuddy（国内版）适配器。

观察到的登录态结构（2026-10-19 只读探查）：

    <LOCALAPPDATA>/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info

是一个 JSON 文件，含 account / accounts / allAccounts / auth 四个部分。
其中 nickname、phoneNumber、accessToken、refreshToken 都是平台加密后的信封
（形如 {"$wbEncrypted": 1, "envelope": "..."}），本工具**不解密也不解析**。

因此这里的策略是：把整个文件当作不透明凭据原样保存、原样写回。
好处是既不需要了解平台的加密方案，也不会因为解析而破坏字段。
代价是显示名拿不到明文昵称——平台没提供，只能先用账号号段标识，由用户自己加备注。

2026-10-25 客户端升级到 5.9.2（品牌 CodeBuddy → WorkBuddy）后，登录态判定多了一层：

    <info>.logged-out                             存在即「用户显式登出」，客户端**完全不读** info；
    <info>.<sha256(origin)[:16]>.observation.json 观测账本，记录当前身份 uid 与 active。

于是「只写 info」不再等于「切号成功」：标记还在，客户端就当没登录；账本 uid 与
info 里的账号对不上，客户端会按 ``unexpected`` 处理并强制重新登录。写回后必须
删标记、对齐账本，这两步由 :meth:`WorkBuddyAdapter.apply_switch` 统一收口。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import psutil

from chaierww.adapters.base import (
    AdapterError,
    Detection,
    ImportedAccount,
    NotInstalled,
    NotLoggedIn,
    PlannedFile,
    PlatformAdapter,
    Slot,
    StateUnreadable,
    SwitchPlan,
    sanitize_child_env,
)

STATE_FILENAME = "workbuddy-desktop.info"
#: ``~/.workbuddy/keyblob``：包裹 at-rest 主密钥的密钥库，用来判断凭据是否还解得开。
KEYBLOB_FILENAME = "keyblob"
#: 客户端「显式登出」标记：存在它，客户端就会跳过整个 info 文件。
LOGOUT_MARKER_SUFFIX = ".logged-out"
#: 观测账本后缀。文件名 = ``<info>.<sha256(origin)[:16]>.observation.json``。
OBSERVATION_SUFFIX = ".observation.json"
# 只按可执行文件名匹配客户端主程序，禁止用子串匹配整条路径：
# `~/.workbuddy/binaries/python/...` 这类运行时路径同样含 "workbuddy"，
# 子串判断会把 chaierww 自身后端等无关进程一并杀掉。
# Linux 上同一个二进制没有 .exe 后缀，按小写产品名匹配。
CLIENT_EXE_NAMES = ("workbuddy.exe",) if os.name == "nt" else ("workbuddy",)
_BOM = b"\xef\xbb\xbf"


def _is_client_process(name: str, exe: str) -> bool:
    """判断进程是否为 WorkBuddy 客户端主程序（含它派生的 Electron 子进程）。"""
    if name in CLIENT_EXE_NAMES:
        return True
    return os.path.basename(exe).lower() in CLIENT_EXE_NAMES


def _now_ms() -> int:
    return int(time.time() * 1000)


def _parse_expires_at(value: object) -> int | None:
    """把各种形态的过期时间统一成毫秒时间戳；认不出来就返回 None。

    上游/旧工具给过的形态至少三种：毫秒整数、秒整数、ISO 字符串。
    统一不了的宁可返回 None —— 调用方据此**不写** ``expiresIn``，
    而不是像以前那样写 0（0 等于告诉客户端「这个 token 已经过期」）。
    """
    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        if number <= 0:
            return None
        # 10 位是秒、13 位是毫秒；更小的当秒处理（2001 年前的秒级时间戳不现实）。
        return number if number >= 100_000_000_000 else number * 1000
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _parse_expires_at(int(text))
        with contextlib.suppress(ValueError):
            from datetime import datetime

            return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp() * 1000)
    return None


def credential_gaps(account: dict) -> list[str]:
    """这个凭据缺了什么，缺的东西会直接导致切过去之后被要求重新登录。

    只报缺口，不替用户补：refresh token 是上游签发的，本工具造不出来。
    """
    gaps: list[str] = []
    access = account.get("accessToken") or account.get("access_token")
    if not isinstance(access, str) or not access.strip():
        gaps.append("accessToken")
    refresh = account.get("refreshToken") or account.get("refresh_token")
    if not isinstance(refresh, str) or not refresh.strip():
        gaps.append("refreshToken")
    if _parse_expires_at(account.get("expiresAt") or account.get("expires_at")) is None:
        gaps.append("expiresAt")
    return gaps


def _workbuddy_home() -> Path:
    """``~/.workbuddy``：客户端 5.9.2 起的用户级数据根。"""
    raw = os.environ.get("USERPROFILE")
    return (Path(raw) if raw else Path.home()) / ".workbuddy"


def _keyblob_key_ids(path: Path | None = None) -> set[str]:
    """本机 keyblob 里出现过的密钥 id 集合；读不到就返回空集（视为「无法判断」）。"""
    target = path or _workbuddy_home() / KEYBLOB_FILENAME
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()

    ids: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"keyId", "protectorKeyId"} and isinstance(value, str) and value.strip():
                    ids.add(value.strip())
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return ids


def _envelope_key_id(value: object) -> str | None:
    """``{$wbEncrypted: 1, envelope: <base64 JSON>}`` 里的 keyId。"""
    if not isinstance(value, dict):
        return None
    envelope = value.get("envelope")
    if not isinstance(envelope, str) or not envelope.strip():
        return None
    try:
        body = json.loads(base64.b64decode(envelope, validate=True).decode("utf-8", "replace"))
    except (ValueError, json.JSONDecodeError):
        return None
    key_id = body.get("keyId") if isinstance(body, dict) else None
    return key_id.strip() if isinstance(key_id, str) and key_id.strip() else None


def credential_key_id(raw: bytes) -> str | None:
    """凭据里令牌信封使用的密钥 id；没有信封（旧版明文）返回 None。"""
    body = raw[3:] if raw.startswith(_BOM) else raw
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    auth = data.get("auth") if isinstance(data, dict) else None
    if not isinstance(auth, dict):
        return None
    return _envelope_key_id(auth.get("accessToken")) or _envelope_key_id(auth.get("refreshToken"))


def stale_reason(raw: bytes, key_ids: set[str] | None = None) -> str | None:
    """凭据是不是本机当前密钥能解开的；能解开或无法判断时返回 None。

    5.9.2 起 token 全部是 ``$wbEncrypted`` 信封，能不能解只取决于信封里的 keyId
    是否还在本机 keyblob 里。客户端换过密钥后，旧号「切过去还是登录页」是最难查的
    故障之一，所以在切号前就把结论说出来。
    """
    key_id = credential_key_id(raw)
    if key_id is None:
        return None
    known = key_ids if key_ids is not None else _keyblob_key_ids()
    if not known:
        # 读不到 keyblob（没装新版客户端/权限不足）时不做判断，避免误报吓人。
        return None
    if key_id in known:
        return None
    return (
        f"这份凭据的令牌由另一套密钥加密（keyId={key_id}），当前客户端的 keyblob 解不开，"
        "切换后大概率仍停在登录页；建议先在客户端登录该账号，再重新导入"
    )


def _local_appdata() -> Path:
    raw = os.environ.get("LOCALAPPDATA")
    if not raw:
        raise NotInstalled("未找到 LOCALAPPDATA 环境变量，无法定位客户端数据目录")
    return Path(raw)


def _config_home() -> Path:
    """Linux 的配置根：XDG_CONFIG_HOME，缺省 ``~/.config``。"""
    raw = os.environ.get("XDG_CONFIG_HOME")
    return Path(raw) if raw else Path.home() / ".config"


@dataclass(frozen=True, slots=True)
class WorkBuddyPaths:
    state_dir: Path
    state_file: Path
    app_candidates: tuple[Path, ...]


def _registry_app_candidates() -> tuple[Path, ...]:
    """从卸载项里找客户端 exe。

    装到自定义盘符（例如 ``C:\\CBRoad\\WorkBuddy``）时，固定路径一个都猜不中；
    但安装程序一定会把真实路径登记到 ``DisplayIcon`` / ``InstallLocation``。
    找不到就让调用方回落固定路径，不要让异常冒出去。
    """
    if os.name != "nt":
        return ()
    import winreg

    roots = (
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"Software\Microsoft\Windows\CurrentVersion\Uninstall"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
        ),
    )
    found: list[Path] = []
    for root, subkey in roots:
        try:
            parent = winreg.OpenKey(root, subkey)
        except OSError:
            continue
        with parent:
            for index in range(winreg.QueryInfoKey(parent)[0]):
                try:
                    with winreg.OpenKey(parent, winreg.EnumKey(parent, index)) as item:
                        display = str(winreg.QueryValueEx(item, "DisplayName")[0]).lower()
                        if "workbuddy" not in display and "codebuddy" not in display:
                            continue
                        values = {}
                        for name in ("DisplayIcon", "InstallLocation"):
                            try:
                                values[name] = str(winreg.QueryValueEx(item, name)[0])
                            except OSError:
                                values[name] = ""
                except OSError:
                    continue
                icon = values["DisplayIcon"].split(",")[0].strip().strip('"')
                location = values["InstallLocation"].strip().strip('"')
                for candidate in (
                    Path(icon) if icon else None,
                    Path(location) / "WorkBuddy.exe" if location else None,
                ):
                    if (
                        candidate is not None
                        and candidate.suffix.lower() == ".exe"
                        and candidate not in found
                    ):
                        found.append(candidate)
    return tuple(found)


def default_paths() -> WorkBuddyPaths:
    if os.name == "nt":
        return _windows_paths()
    return _linux_paths()


def _windows_paths() -> WorkBuddyPaths:
    state_dir = _local_appdata() / "CodeBuddyExtension" / "Data" / "Public" / "auth"
    # 装在非默认位置（比如自选盘符）的用户用这个变量指定，比猜路径可靠。
    override = os.environ.get("CHAIERWW_WORKBUDDY_APP")
    candidates = tuple(
        c
        for c in (
            Path(override) if override else None,
            *_registry_app_candidates(),
            _local_appdata() / "Programs" / "WorkBuddy" / "WorkBuddy.exe",
            (
                Path(os.environ.get("PROGRAMFILES", "C:/Program Files"))
                / "WorkBuddy"
                / "WorkBuddy.exe"
            ),
        )
        if c is not None
    )
    return WorkBuddyPaths(
        state_dir=state_dir,
        state_file=state_dir / STATE_FILENAME,
        app_candidates=candidates,
    )


def _linux_paths() -> WorkBuddyPaths:
    """Linux：Electron 的 appData 落在 ``~/.config``，与 Windows 的 LocalAppData 对齐。

    客户端若没有 Linux 版，这里会表现为「未安装」——检测只看路径存不存在，
    不会因为猜不到目录就报错；路径确实不同时用 ``CHAIERWW_WORKBUDDY_STATE_DIR`` 覆盖。
    """
    override_dir = os.environ.get("CHAIERWW_WORKBUDDY_STATE_DIR")
    state_dir = (
        Path(override_dir)
        if override_dir
        else _config_home() / "CodeBuddyExtension" / "Data" / "Public" / "auth"
    )
    override_app = os.environ.get("CHAIERWW_WORKBUDDY_APP")
    candidates = tuple(
        c
        for c in (
            Path(override_app) if override_app else None,
            Path("/usr/bin/workbuddy"),
            Path("/opt/WorkBuddy/WorkBuddy"),
            Path("/opt/workbuddy/workbuddy"),
        )
        if c is not None
    )
    return WorkBuddyPaths(
        state_dir=state_dir,
        state_file=state_dir / STATE_FILENAME,
        app_candidates=candidates,
    )


#: 除桌面端以外的程序位：**尚未适配**，所以连状态文件路径都不声明 ——
#: 未适配与未安装是两回事，编一个不存在的路径比什么都不写更糟。
#: 适配某一位时，把它改成有 `state_file` / `writable=True` 的实位即可（一位一测）。
_UNADAPTED_SLOTS: tuple[tuple[str, str, str], ...] = (
    ("codebuddy-cn-ide", "CodeBuddy 国内版 IDE", "尚未适配：该程序位的登录态文件布局未实测"),
    ("codebuddy-cli", "CodeBuddy CLI", "尚未适配：命令行宿主的凭据位置未实测"),
    ("vscode-extension", "VS Code 插件", "尚未适配：插件宿主的登录态路径未实测"),
    ("jetbrains-plugin", "JetBrains 插件", "尚未适配：插件宿主的登录态路径未实测"),
)


def parse_identity(raw: bytes) -> tuple[str, str, dict[str, str]]:
    """从登录态文件里取出稳定标识与可展示名称。

    只读 account 段里的非加密字段；任何加密信封都不碰。
    """
    body = raw[3:] if raw.startswith(_BOM) else raw
    text = body.decode("utf-8", "replace")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateUnreadable("登录态文件不是有效的 JSON，可能已损坏") from exc

    if not isinstance(data, dict):
        raise StateUnreadable("登录态文件结构异常")

    account = data.get("account")
    if not isinstance(account, dict) or not account:
        raise NotLoggedIn("客户端当前未登录，请先在客户端登录后再导入")

    uid = str(account.get("uid") or "").strip()
    uin = str(account.get("uin") or "").strip()
    external_id = uid or uin
    if not external_id:
        raise NotLoggedIn("登录态里没有可用的账号标识")

    display_name = f"用户 {uin}" if uin else f"账号 {external_id[:8]}"

    detail = {"uin": uin, "accountType": str(account.get("type") or "")}
    return external_id, display_name, detail


def _uid_of(raw: bytes) -> str | None:
    """尽力取出 info 里的 uid，结构不认识就返回 None（不抛错）。

    与 :func:`parse_identity` 的区别：这里只是「写回之后顺手对齐账本」的辅助信息，
    拿不到最多是少做一步对齐，不该让已经成功的写回变成失败。
    """
    body = raw[3:] if raw.startswith(_BOM) else raw
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    account = data.get("account")
    if not isinstance(account, dict):
        return None
    uid = str(account.get("uid") or "").strip()
    return uid or None


class WorkBuddyAdapter:
    scope = "workbuddy-cn"

    def __init__(self, paths: WorkBuddyPaths | None = None) -> None:
        self._paths = paths or default_paths()

    # ---------- 探测 ----------

    def state_dir(self) -> Path:
        return self._paths.state_dir

    # ---------- 登录态文件族（5.9.2 起由一个文件变成「文件 + 标记 + 账本」） ----------

    def _logout_marker(self) -> Path:
        state_file = self._paths.state_file
        return state_file.with_name(state_file.name + LOGOUT_MARKER_SUFFIX)

    def _observation_files(self) -> list[Path]:
        state_file = self._paths.state_file
        if not state_file.parent.is_dir():
            return []
        return sorted(state_file.parent.glob(f"{state_file.name}.*{OBSERVATION_SUFFIX}"))

    def _clear_logout_marker(self) -> bool:
        """删掉「显式登出」标记；返回是否真的删掉了一个。

        删不掉必须抛错而不是忽略：标记还在，客户端就完全不读我们刚写的登录态，
        等于这次切号白做，还会让用户以为「切成功了但界面还是登录页」。
        """
        marker = self._logout_marker()
        try:
            marker.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise StateUnreadable(
                f"无法删除登出标记 {marker.name}（{exc.strerror or exc}），"
                "客户端仍会按「已登出」忽略新写入的登录态，请确认客户端已完全退出后重试"
            ) from exc
        return True

    def _realign_observation(self, uid: str) -> int:
        """删掉 uid 对不上的观测账本，让客户端按新身份重建；返回删掉的份数。

        账本里只有身份与活跃标记，不含凭据；留着旧 uid 反而会被客户端判成一次
        ``unexpected`` 身份变更并强制重新登录。
        """
        removed = 0
        for path in self._observation_files():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                # 读不懂就不动它：破坏一份看不懂的文件，比少对齐一次更糟。
                continue
            identity = data.get("identity") if isinstance(data, dict) else None
            ledger_uid = ""
            if isinstance(identity, dict):
                ledger_uid = str(identity.get("userId") or "").strip()
            if ledger_uid == uid:
                continue
            with contextlib.suppress(OSError):
                path.unlink()
                removed += 1
        return removed

    def app_path(self) -> Path | None:
        for candidate in self._paths.app_candidates:
            if candidate.is_file():
                return candidate
        return None

    def is_running(self) -> bool:
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                name = (proc.info.get("name") or "").lower()
                exe = (proc.info.get("exe") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if _is_client_process(name, exe):
                return True
        return False

    def detect(self) -> Detection:
        app = self.app_path()
        state_file = self._paths.state_file
        running = self.is_running()

        if app is None and not state_file.exists():
            return Detection(
                installed=False,
                running=False,
                logged_in=False,
                state_dir=None,
                app_path=None,
                message="未找到 WorkBuddy 客户端，请先安装或手动指定安装路径",
            )

        logged_in = False
        current_external_id: str | None = None
        message = ""
        if self._logout_marker().exists():
            # 5.9.2 起：标记存在 = 用户显式登出，客户端根本不读 info，不能再按「已登录」报。
            message = "客户端已退出登录（.logged-out 标记存在），请先在客户端登录"
        elif state_file.exists():
            try:
                current_external_id, _, _ = parse_identity(state_file.read_bytes())
                logged_in = True
            except NotLoggedIn:
                message = "客户端当前未登录"
            except StateUnreadable as exc:
                message = str(exc)
            except OSError as exc:
                # 客户端运行时会把登录态文件锁住（Windows 独占读）。
                # 登录态文件只在登录后才会生成，所以「文件存在但读不到」=
                # 客户端正在运行且已登录（只是读不到是哪个账号）。绝不该误判成「未登录」。
                logged_in = True
                message = (
                    f"客户端运行中，登录态文件暂时锁定（{exc.strerror or exc}），"
                    "已按已登录处理；退出客户端后可读取具体账号"
                )
        else:
            message = "登录态文件不存在"

        return Detection(
            installed=True,
            running=running,
            logged_in=logged_in,
            state_dir=self._paths.state_dir,
            app_path=app,
            message=message,
            current_external_id=current_external_id,
        )

    # ---------- 多程序位 ----------

    def slots(self) -> tuple[Slot, ...]:
        """五位程序位：桌面端（唯一已适配、可写）+ 四位未适配（只展示、写明原因）。

        `slot_adapter()` 对未适配位一律报错 —— 宁可不切，也不往没实测过的路径写。
        """
        desktop = Slot(
            key="desktop",
            label="桌面端",
            writable=True,
            installed=self.app_path() is not None or self._paths.state_file.exists(),
            state_file=self._paths.state_file,
            app_path=self.app_path(),
        )
        unadapted = tuple(
            Slot(key=key, label=label, writable=False, installed=False, reason=reason)
            for key, label, reason in _UNADAPTED_SLOTS
        )
        return (desktop, *unadapted)

    def slot_adapter(self, slot: str) -> PlatformAdapter:
        matched = next((item for item in self.slots() if item.key == slot), None)
        if matched is None:
            raise AdapterError(f"未知程序位：{slot}")
        if not matched.writable:
            raise AdapterError(f"程序位「{matched.label}」{matched.reason or '暂不支持写入'}")
        return self

    # ---------- 导入 ----------

    def import_current(self) -> ImportedAccount:
        state_file = self._paths.state_file
        if self._logout_marker().exists():
            # 标记存在时客户端不会使用 info，导出的会是一份「已失效」的凭据。
            raise NotLoggedIn(
                "客户端已退出登录（存在 .logged-out 标记），请先在客户端重新登录再导入"
            )
        if not state_file.is_file():
            if self.app_path() is None:
                raise NotInstalled("未找到客户端，请先安装并登录")
            raise NotLoggedIn("未找到登录态文件，请先在客户端登录")

        try:
            raw = state_file.read_bytes()
        except OSError as exc:
            raise StateUnreadable(
                f"无法读取登录态文件（{exc.strerror or exc}）："
                "客户端正在运行时会锁住该文件，请先退出 WorkBuddy 再导入"
            ) from exc

        external_id, display_name, detail = parse_identity(raw)
        return ImportedAccount(
            external_id=external_id,
            display_name=display_name,
            payload=raw,
            detail=detail,
        )

    def import_json(self, raw: bytes) -> ImportedAccount:
        """JSON 粘贴导入：内容 = 登录态文件（workbuddy-desktop.info）的原文。

        能解析出账号标识就收下，整份原文作为凭据保存。
        """
        external_id, display_name, detail = parse_identity(raw)
        return ImportedAccount(
            external_id=external_id,
            display_name=display_name,
            payload=raw,
            detail=detail,
        )

    # ---------- 切换 ----------

    def plan_switch(self, payload: bytes) -> SwitchPlan:
        state_file = self._paths.state_file
        warnings: list[str] = []

        if state_file.exists():
            current_size = state_file.stat().st_size
            files = (
                PlannedFile(
                    path=state_file,
                    action="override",
                    size=current_size,
                    note="当前登录态，将被备份后覆盖",
                ),
            )
        else:
            warnings.append("登录态文件不存在，将新建")
            files = (
                PlannedFile(path=state_file, action="create", size=0, note="当前没有登录态文件"),
            )

        if not state_file.parent.exists():
            warnings.append(f"目录不存在，将创建：{state_file.parent}")

        for sibling in self._history_files():
            files += (
                PlannedFile(
                    path=sibling,
                    action="keep",
                    size=sibling.stat().st_size,
                    note="同目录其他文件，不动",
                ),
            )

        marker = self._logout_marker()
        if marker.exists():
            warnings.append("客户端当前处于「已登出」状态，切换时会一并清除 .logged-out 标记")
        issue = stale_reason(payload)
        if issue:
            warnings.append(issue)
        files += self._auxiliary_plan()

        return SwitchPlan(
            files=files,
            requires_app_exit=self.is_running(),
            warnings=tuple(warnings),
        )

    def _auxiliary_plan(self) -> tuple[PlannedFile, ...]:
        """登录态文件之外、但同属「5.9.2 登录态」的几份文件：登出标记与观测账本。"""
        items: list[PlannedFile] = []
        marker = self._logout_marker()
        if marker.exists():
            items.append(
                PlannedFile(
                    path=marker,
                    action="delete",
                    size=marker.stat().st_size,
                    note="登出标记，不删客户端就不会读新写入的登录态",
                )
            )
        for ledger in self._observation_files():
            items.append(
                PlannedFile(
                    path=ledger,
                    action="realign",
                    size=ledger.stat().st_size,
                    note="观测账本，切号后按目标账号重建",
                )
            )
        return tuple(items)

    def _history_files(self) -> list[Path]:
        if not self._paths.state_dir.is_dir():
            return []
        current = self._paths.state_file.name
        marker = self._logout_marker().name
        return sorted(
            p
            for p in self._paths.state_dir.glob(f"{current.split('.')[0]}.*")
            if p.is_file()
            and p.name != current
            and p.name != marker
            and not p.name.endswith(OBSERVATION_SUFFIX)
        )

    def apply_switch(self, payload: bytes) -> None:
        target = self._paths.state_file
        target.parent.mkdir(parents=True, exist_ok=True)

        # 先写同目录临时文件再原子替换：避免写到一半留下损坏的登录态。
        tmp = target.with_name(target.name + ".chaierww.tmp")
        try:
            with open(tmp, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except PermissionError as exc:
            # 客户端正在运行时独占锁住登录态文件，`os.replace` 会「拒绝访问」。
            raise StateUnreadable(
                "客户端正在运行，登录态文件被占用，无法切换。"
                "请先退出客户端再重试（会话/记忆等数据迁移不受影响）"
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                if tmp.exists():
                    tmp.unlink()

        # 写入成功后还有两件必做的事，缺一件这次切号就是「假的」：
        # 1. 删 .logged-out：5.9.2 起标记存在时客户端完全不读 info；
        # 2. 对齐观测账本：账本 uid 与 info 不一致会被判成 unexpected 身份变更。
        self._clear_logout_marker()
        uid = _uid_of(payload)
        if uid:
            self._realign_observation(uid)

    # ---------- token 凭据切换（扫码采集的账号） ----------

    def plan_token_switch(self, account: dict) -> SwitchPlan:
        """token 账号切换预演：与文件切换同样只写认证文件，但内容是构造出来的。"""
        state_file = self._paths.state_file
        warnings: list[str] = []
        files = (
            PlannedFile(
                path=state_file,
                action="override" if state_file.exists() else "create",
                size=state_file.stat().st_size if state_file.exists() else 0,
                note="用扫码采集的 token 重构认证文件",
            ),
        )
        if not state_file.parent.exists():
            warnings.append(f"目录不存在，将创建：{state_file.parent}")
        if self._logout_marker().exists():
            warnings.append("客户端当前处于「已登出」状态，切换时会一并清除 .logged-out 标记")
        return SwitchPlan(
            files=(*files, *self._auxiliary_plan()),
            requires_app_exit=self.is_running(),
            warnings=tuple(warnings),
        )

    def apply_token_switch(self, account: dict) -> None:
        """把扫码 token 账号写回客户端登录态。

        对照旧项目 `auth_file::write_account_to_auth_file`：明文 token 直接写进
        `auth.accessToken`/`auth.refreshToken`，account 段从 profile_raw 重建，
        客户端能读明文 token（5.6 起客户端会自行加密成信封）。
        """
        uid = str(account.get("uid") or "").strip()
        if not uid:
            raise StateUnreadable("扫码账号缺少 uid，无法切换客户端")

        account_obj = self._build_account_obj(account)
        auth_obj = self._build_auth_obj(account)
        all_accounts = self._merge_all_accounts(account_obj)

        session = {
            "account": account_obj,
            "auth": auth_obj,
            "accounts": all_accounts,
            "allAccounts": all_accounts,
        }
        content = json.dumps(session, ensure_ascii=False, indent=2).encode("utf-8")
        self.apply_switch(content)

    def _build_account_obj(self, account: dict) -> dict:
        """从 profile_raw 重建 account 段（复刻旧项目 build_account_obj）。"""
        profile = account.get("profile_raw")
        obj: dict = dict(profile) if isinstance(profile, dict) else {}
        obj["uid"] = account.get("uid") or ""
        obj["nickname"] = account.get("nickname") or ""
        defaults = {
            "type": "personal",
            "accountType": "",
            "idp": "",
            "oneidAccountId": "",
            "areaInfoComplete": False,
            "isCurrentOneIdEnterprise": False,
            "isCurrentOneIdPersonal": False,
            "isFirstLogin": False,
            "isCreator": False,
            "isAdmin": False,
            "uin": "",
            "phoneNumber": "",
            "lastLogin": True,
            "pluginEnabled": True,
            "deployStatus": {"statusCode": 0, "statusMsg": "", "detailMsg": ""},
            "sso": {"domain": "", "domainModifiedTimes": 0},
        }
        for key, value in defaults.items():
            obj.setdefault(key, value)
        return obj

    def _build_auth_obj(self, account: dict) -> dict:
        """构造 auth 段：明文 token 直接写入（复刻旧项目 build_auth_obj）。"""
        obj: dict = {}
        raw = account.get("auth_raw")
        if isinstance(raw, dict):
            inner = raw.get("auth")
            if isinstance(inner, dict):
                obj.update(inner)
            else:
                obj.update(raw)
        obj["accessToken"] = account.get("accessToken") or account.get("access_token") or ""
        obj["refreshToken"] = account.get("refreshToken") or account.get("refresh_token") or ""
        obj["tokenType"] = account.get("tokenType") or account.get("token_type") or "Bearer"
        obj["domain"] = account.get("domain") or ""
        obj["lastRefreshTime"] = _now_ms()
        obj.setdefault("scope", "openid profile offline_access email")
        # 过期时间只写能确定的：以前缺 expiresAt 时写 0，等于明说「已经过期」，
        # 客户端读到就会立刻要求重新登录 —— 这是「导入/切号后还要登录」的直接原因。
        # 认不出来就整段省略，把判断权交回客户端。
        expires_at = _parse_expires_at(account.get("expiresAt") or account.get("expires_at"))
        if expires_at is not None:
            obj["expiresAt"] = expires_at
            obj["expiresIn"] = max(0, (expires_at - _now_ms()) // 1000)
        refresh_expires = _parse_expires_at(
            account.get("refreshExpiresAt") or account.get("refresh_expires_at")
        )
        if refresh_expires is not None:
            obj["refreshExpiresAt"] = refresh_expires
            obj["refreshExpiresIn"] = max(0, (refresh_expires - _now_ms()) // 1000)
        obj.setdefault("notBeforePolicy", 0)
        obj.setdefault("sessionState", "")
        return obj

    def _merge_all_accounts(self, account_obj: dict) -> list[dict]:
        """把目标账号并入 allAccounts（按 uid 去重），保留客户端既有账号。"""
        existing = self._read_existing()
        raw_all = (
            existing.get("allAccounts")
            or existing.get("accounts")
            or []
        )
        all_accounts = [a for a in raw_all if isinstance(a, dict)]
        target_uid = str(account_obj.get("uid") or "")
        all_accounts = [
            a for a in all_accounts
            if str(a.get("uid") or a.get("id") or "") != target_uid
        ]
        all_accounts.append(account_obj)
        return all_accounts

    def _read_existing(self) -> dict:
        state_file = self._paths.state_file
        if not state_file.exists():
            return {}
        try:
            body = state_file.read_bytes()
            if body.startswith(_BOM):
                body = body[3:]
            return json.loads(body.decode("utf-8", "replace"))
        except (OSError, json.JSONDecodeError):
            return {}

    def verify_switch(self, expected_external_id: str) -> bool:
        # 标记还在 = 客户端会忽略 info，即使文件内容对也不能算切换成功。
        if self._logout_marker().exists():
            return False
        try:
            raw = self._paths.state_file.read_bytes()
            external_id, _, _ = parse_identity(raw)
        except (OSError, StateUnreadable, NotLoggedIn):
            return False
        return external_id == expected_external_id

    def credential_issue(self, payload: bytes) -> str | None:
        """切号前体检：这份凭据为什么可能用不了；没问题返回 None。

        目前只覆盖「密钥换代」这一种：信封 keyId 不在本机 keyblob 里。
        其余缺口由 :func:`credential_gaps` 在导入侧报告。
        """
        return stale_reason(payload)

    def launch(self) -> bool:
        app = self.app_path()
        if app is None:
            return False
        try:
            # 同 Trae：用 ShellExecute（`os.startfile`）而非 `Popen`。真机排障确认，
            # 从 WorkBuddy 派生的进程用 `Popen` 启动 Electron 客户端，渲染进程必崩
            # （renderer gone, -2147483645），而 ShellExecute（等价双击）能稳定拉起。
            if os.name == "nt":
                os.startfile(str(app))  # type: ignore[attr-defined]
            else:
                import subprocess

                env = sanitize_child_env(os.environ)
                subprocess.Popen(
                    [str(app)],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
        except OSError:
            return False
        return True

    def terminate(self) -> bool:
        """退出正在运行的 WorkBuddy 进程。切换账号后必须重启才能重新加载登录态。"""
        killed = False
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                name = (proc.info.get("name") or "").lower()
                exe = (proc.info.get("exe") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if _is_client_process(name, exe):
                try:
                    proc.terminate()
                    killed = True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        return killed

    def wait_exit(self, timeout: float = 5.0) -> bool:
        """等待 WorkBuddy 进程退出（terminate 后进程不会立刻消失）。"""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_running():
                return True
            time.sleep(0.2)
        return not self.is_running()

    def snapshot_root(self) -> Path:
        """备份范围 = 整个 auth 目录（含客户端自己的历史备份）。"""
        return self._paths.state_dir

    def restore_from(self, source_dir: Path) -> None:
        """用备份目录精确还原 auth 目录。

        先把当前文件挪开（而不是直接删），出错还能拿回来。
        """
        target = self._paths.state_dir
        target.mkdir(parents=True, exist_ok=True)
        staging = target / ".chaierww-restore-old"
        if staging.exists():
            shutil.rmtree(staging)
        staging.mkdir()

        for item in list(target.iterdir()):
            if item.name == staging.name:
                continue
            if item.is_file():
                shutil.move(str(item), str(staging / item.name))

        for item in source_dir.iterdir():
            # 备份清单是备份自己的元数据，不属于客户端目录。
            if item.is_file() and item.name != "manifest.json":
                shutil.copy2(item, target / item.name)
