"""Trae（国内版）适配器。

Trae 是 VSCode 内核的 IDE，登录态与 WorkBuddy 完全不同：

    <APPDATA>/Trae CN/User/globalStorage/storage.json

是一个 JSON 文件，登录凭据**不在** `state.vscdb`（README 曾据此判断「未开始」），
而在 `storage.json` 里的 `iCubeAuthInfo*` 系列 key：

    iCubeAuthInfo://icube-dc:<uid>   ← 登录凭据信封（DPAPI 加密），<uid> 即账号唯一标识
    iCubeAuthInfo://icube.cloudide   ← 云端 IDE 会话信封
    iCubeAuthInfo://usertag          ← 用户标签信封

这些值都是平台 DPAPI 加密后的信封（形如 `dGMFEAA...` 的 base64），本工具**不解密也不解析**，
与 WorkBuddy 一致：原样保存、原样写回。

与 WorkBuddy 的差异：storage.json 里除了 `iCubeAuthInfo*` 还有大量用户 IDE 设置
（主题、窗口、遥测等）。切换账号时**只替换 `iCubeAuthInfo*` 前缀的 key**，其余设置原样保留，
避免切换账号把用户的界面偏好一并覆盖掉。
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
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
from chaierww.domain.trae_crypto import TraeCryptoError, tc_decrypt, tc_encrypt

STORAGE_FILENAME = "storage.json"
AUTH_PREFIX = "iCubeAuthInfo"
DC_PREFIX = "iCubeAuthInfo://icube-dc:"
ICUBE_CLOUDIDE_KEY = "iCubeAuthInfo://icube.cloudide"
ICUBE_USERTAG_KEY = "iCubeAuthInfo://usertag"
# 同 WorkBuddy：只按文件名匹配客户端，禁止子串匹配整条路径
# （`~/.trae-cn/...` 这类路径同样含 "trae"，会误杀 IDE 等无关进程）。
CLIENT_EXE_NAMES = ("trae cn.exe",)


def _is_client_process(name: str, exe: str) -> bool:
    """判断进程是否为 Trae CN 客户端主程序（含它派生的 Electron 子进程）。"""
    if name in CLIENT_EXE_NAMES:
        return True
    return os.path.basename(exe).lower() in CLIENT_EXE_NAMES


def _appdata() -> Path:
    raw = os.environ.get("APPDATA")
    if not raw:
        raise NotInstalled("未找到 APPDATA 环境变量，无法定位客户端数据目录")
    return Path(raw)


@dataclass(frozen=True, slots=True)
class TraePaths:
    state_dir: Path
    storage_file: Path
    app_candidates: tuple[Path, ...]


def default_paths() -> TraePaths:
    state_dir = _appdata() / "Trae CN" / "User" / "globalStorage"
    local_raw = os.environ.get("LOCALAPPDATA")
    local_appdata = Path(local_raw) if local_raw else None
    local_candidate = (
        local_appdata / "Programs" / "Trae CN" / "Trae CN.exe"
        if local_appdata
        else None
    )
    candidates = tuple(
        c
        for c in (
            # 装在非默认位置（比如自选盘符）的用户用这个变量指定，比猜路径可靠。
            Path(os.environ["CHAIERWW_TRAE_APP"])
            if os.environ.get("CHAIERWW_TRAE_APP")
            else None,
            local_candidate,
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "Trae CN" / "Trae CN.exe",
        )
        if c is not None
    )
    return TraePaths(
        state_dir=state_dir,
        storage_file=state_dir / STORAGE_FILENAME,
        app_candidates=candidates,
    )


def _auth_keys(data: dict) -> dict[str, object]:
    """取出 storage.json 里所有 iCubeAuthInfo 前缀的登录凭据 key。"""
    return {k: v for k, v in data.items() if str(k).startswith(AUTH_PREFIX)}


def parse_identity(raw: bytes) -> tuple[str, str, dict[str, str]]:
    """从 storage.json 里取出账号标识与可展示名称。

    账号唯一标识 = ``iCubeAuthInfo://icube.cloudide`` 信封里的 ``userId``（真正的账号 ID）。
    ``iCubeAuthInfo://icube-dc:<deviceId>`` 内嵌的是**设备 ID**（本机客户端固定不变），
    不是账号，不能用它当账号标识，否则「当前登录高亮」与「切换校验」都会错。

    昵称平台未提供明文（信封加密），沿用「用户 <userId>」作为兜底显示名。
    """
    try:
        text = raw.decode("utf-8", "replace")
    except (UnicodeDecodeError, AttributeError) as exc:
        raise StateUnreadable("登录态文件不是有效的 UTF-8，可能已损坏") from exc

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateUnreadable("登录态文件不是有效的 JSON，可能已损坏") from exc

    if not isinstance(data, dict):
        raise StateUnreadable("登录态文件结构异常")

    # 真正账号 ID 在 cloudide 信封里；解不出则回退设备 ID（保证「已登录」判定不误判）。
    external_id = _cloudide_user_id(data)

    if not external_id:
        raise NotLoggedIn("客户端当前未登录，请先在客户端登录后再导入")

    display_name = f"用户 {external_id[-8:]}"
    detail = {"uid": external_id}
    return external_id, display_name, detail


def _cloudide_user_id(data: dict) -> str | None:
    """从 storage.json 里解出 cloudide 信封的 ``userId``（真正账号 ID）。

    解密失败（信封形态变化/损坏）时返回 ``None``，由调用方决定是否回退设备 ID。
    """
    envelope = data.get(ICUBE_CLOUDIDE_KEY)
    if not isinstance(envelope, str) or not envelope:
        return None
    try:
        info = json.loads(tc_decrypt(envelope))
    except (TraeCryptoError, json.JSONDecodeError, ValueError):
        return None
    if not isinstance(info, dict):
        return None
    uid = info.get("userId")
    if isinstance(uid, str) and uid.strip():
        return uid.strip()
    if isinstance(uid, int):
        return str(uid)
    return None


def _device_id_from(data: dict) -> str | None:
    """从 ``iCubeAuthInfo://icube-dc:<deviceId>`` key 名解析设备 ID。"""
    for key in data:
        if str(key).startswith(DC_PREFIX):
            return str(key)[len(DC_PREFIX):]
    return None


def _jwt_times(token: str) -> tuple[int | None, int | None]:
    """从 JWT payload 里解出 ``(exp, iat)``（epoch 秒）。解析失败返回 ``(None, None)``。"""
    try:
        parts = token.split(".")
        if len(parts) < 2:
            return None, None
        payload = parts[1]
        payload += "=" * (-len(payload) % 4)
        obj = json.loads(base64.urlsafe_b64decode(payload))
        exp = obj.get("exp")
        iat = obj.get("iat")
        return (
            int(exp) if isinstance(exp, (int, float)) else None,
            int(iat) if isinstance(iat, (int, float)) else None,
        )
    except (ValueError, json.JSONDecodeError, base64.binascii.Error):  # type: ignore[attr-defined]
        return None, None


def _iso_ms(epoch_seconds: int) -> str:
    """epoch 秒 → ISO 8601 毫秒时间（UTC，含毫秒，与真机信封格式一致）。"""
    return (
        datetime.fromtimestamp(epoch_seconds, tz=UTC)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _build_account_obj(account: dict, display_name: str) -> dict:
    """从凭据构造 cloudide 信封里的 ``account`` 段（profile 快照）。

    扫码/老项目凭据里没有完整 profile（JWT 里只有 id/source/tenant），
    这里填最小可用值：username 用 displayName 兜底，其余留空，
    客户端启动后自会拉取/刷新真实 profile。
    """
    username = str(account.get("username") or account.get("name") or display_name).strip()
    return {
        "username": username,
        "iss": "",
        "iat": 0,
        "organization": "",
        "work_country": "",
        "email": str(account.get("email") or "").strip(),
        "avatar_url": str(account.get("avatarUrl") or account.get("avatar_url") or "").strip(),
        "description": "",
        "scope": "marscode",
        "loginScope": "trae",
        "nonPlainTextMobile": "",
        "storeCountryCode": "",
        "storeCountrySrc": "",
        "storeRegion": "CN",
        "userTag": "cn",
        "migrateToSG": False,
    }


class TraeAdapter:
    scope = "trae-cn"

    def __init__(self, paths: TraePaths | None = None) -> None:
        self._paths = paths or default_paths()

    # ---------- 多程序位 ----------

    def slots(self) -> tuple[Slot, ...]:
        """Trae 国内版只有一个程序位（IDE），它就是我们换根的位置。"""
        return (
            Slot(
                key="ide",
                label="Trae IDE",
                writable=True,
                installed=self.app_path() is not None or self._paths.storage_file.exists(),
                state_file=self._paths.storage_file,
                app_path=self.app_path(),
            ),
        )

    def slot_adapter(self, slot: str) -> PlatformAdapter:
        matched = next((item for item in self.slots() if item.key == slot), None)
        if matched is None:
            raise AdapterError(f"未知程序位：{slot}")
        if not matched.writable:
            raise AdapterError(f"程序位「{matched.label}」{matched.reason or '暂不支持写入'}")
        return self

    # ---------- 探测 ----------

    def state_dir(self) -> Path:
        return self._paths.state_dir

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
        storage = self._paths.storage_file
        running = self.is_running()

        if app is None and not storage.exists():
            return Detection(
                installed=False,
                running=False,
                logged_in=False,
                state_dir=None,
                app_path=None,
                message="未找到 Trae 客户端，请先安装或手动指定安装路径",
            )

        logged_in = False
        current_external_id: str | None = None
        message = ""
        if storage.exists():
            try:
                current_external_id, _, _ = parse_identity(storage.read_bytes())
                logged_in = True
            except NotLoggedIn:
                message = "客户端当前未登录"
            except StateUnreadable as exc:
                message = str(exc)
            except OSError as exc:
                # 客户端运行中可能锁住 storage.json；检测是只读探针，不该整体失败。
                message = (
                    f"登录态文件暂时不可读（{exc.strerror or exc}）；"
                    "客户端正在运行时属正常，退出客户端后可导入"
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

    # ---------- 导入 ----------

    def import_current(self) -> ImportedAccount:
        storage = self._paths.storage_file
        if not storage.is_file():
            if self.app_path() is None:
                raise NotInstalled("未找到客户端，请先安装并登录")
            raise NotLoggedIn("未找到登录态文件，请先在客户端登录")

        try:
            raw = storage.read_bytes()
        except OSError as exc:
            raise StateUnreadable(f"无法读取登录态文件：{exc.strerror or exc}") from exc

        external_id, display_name, detail = parse_identity(raw)

        # 只保存 iCubeAuthInfo* 凭据 key，不把用户 IDE 设置混进凭据。
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise StateUnreadable("登录态文件不是有效的 JSON") from exc
        auth_blob = json.dumps(_auth_keys(data), ensure_ascii=False).encode("utf-8")

        return ImportedAccount(
            external_id=external_id,
            display_name=display_name,
            payload=auth_blob,
            detail=detail,
        )

    def import_json(self, raw: bytes) -> ImportedAccount:
        """JSON 粘贴导入。两种内容都收：

        1. 只含 `iCubeAuthInfo*` 凭据 key 的 JSON（本工具导出的格式）；
        2. 整份 storage.json（iCubeAuthInfo 与 IDE 设置混在一起）。

        无论哪种，凭据只取 `iCubeAuthInfo*` key，其余一律不保存。
        """
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise StateUnreadable("内容不是有效的 JSON") from exc
        if not isinstance(data, dict):
            raise StateUnreadable("内容结构异常")

        external_id = _cloudide_user_id(data)
        if not external_id:
            # 回退：老版本导出的凭据只有 icube-dc（设备 ID）没有 cloudide，仍要能识别。
            external_id = _device_id_from(data)
        if not external_id:
            raise NotLoggedIn("内容里没有 Trae 登录凭据（缺少 iCubeAuthInfo 键）")

        auth_blob = json.dumps(_auth_keys(data), ensure_ascii=False).encode("utf-8")
        return ImportedAccount(
            external_id=external_id,
            display_name=f"用户 {external_id[-8:]}",
            payload=auth_blob,
            detail={"uid": external_id},
        )

    # ---------- 扫码登录辅助 ----------

    def read_storage_json(self) -> dict:
        """读取 storage.json 并解析成对象（供 OAuth 扫码登录取设备身份）。

        文件不存在或损坏时抛出 AdapterError 系异常，由上层转成可读错误。
        """
        storage = self._paths.storage_file
        if not storage.is_file():
            if self.app_path() is None:
                raise NotInstalled("未找到客户端，请先安装并启动一次 Trae")
            raise NotLoggedIn("未找到登录态文件，请先启动一次 Trae 客户端")
        try:
            raw = storage.read_bytes()
            data = json.loads(raw.decode("utf-8", "replace"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StateUnreadable(f"无法读取登录态文件：{exc}") from exc
        if not isinstance(data, dict):
            raise StateUnreadable("登录态文件结构异常")
        return data

    # ---------- 切换 ----------

    def _read_storage(self) -> dict:
        if not self._paths.storage_file.exists():
            return {}
        raw = self._paths.storage_file.read_bytes()
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise StateUnreadable("登录态文件不是有效的 JSON，可能已损坏") from exc

    def _write_storage(self, data: dict) -> None:
        target = self._paths.storage_file
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".chaierww.tmp")
        blob = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        try:
            with open(tmp, "wb") as handle:
                handle.write(blob)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except PermissionError as exc:
            raise StateUnreadable(
                "客户端正在运行，登录态文件被占用，无法切换。请先退出客户端再重试"
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                if tmp.exists():
                    tmp.unlink()

    def plan_switch(self, payload: bytes) -> SwitchPlan:
        storage = self._paths.storage_file
        warnings: list[str] = []

        files = (
            PlannedFile(
                path=storage,
                action="override" if storage.exists() else "create",
                size=storage.stat().st_size if storage.exists() else 0,
                note="仅替换其中的 iCubeAuthInfo 登录凭据，其余设置保留",
            ),
        )
        if not storage.parent.exists():
            warnings.append(f"目录不存在，将创建：{storage.parent}")

        return SwitchPlan(
            files=files,
            requires_app_exit=self.is_running(),
            warnings=tuple(warnings),
        )

    def apply_switch(self, payload: bytes) -> None:
        try:
            incoming = json.loads(payload.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise StateUnreadable("目标账号的登录凭据损坏，请重新导入") from exc

        data = self._read_storage()
        # 清除旧的 iCubeAuthInfo key，写入目标账号的凭据 key。
        for key in list(data):
            if str(key).startswith(AUTH_PREFIX):
                del data[key]
        data.update(incoming)
        self._write_storage(data)

    def verify_incoming(self, payload: bytes, expected_external_id: str) -> bool:
        """写入前校验目标凭据的账号标识是否与账号记录的 externalId 一致。

        历史遗留的坏账号可能把「设备 ID」（icube-dc）误当成账号 ID 存进 externalId，
        而凭据里真正的账号 ID 在 cloudide 信封里。若不一致，切换后校验必然失败，
        与其写入后再回滚，不如在写入前就拦下，给出明确提示。
        """
        try:
            incoming = json.loads(payload.decode("utf-8", "replace"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return True  # 交给 apply_switch 报「凭据损坏」
        if not isinstance(incoming, dict):
            return True
        # 从目标凭据里解出真正的账号 ID（cloudide userId 优先，回退 icube-dc 设备 ID）。
        actual = _cloudide_user_id(incoming)
        if not actual:
            actual = _device_id_from(incoming)
        if not actual:
            return True
        return actual == expected_external_id

    def verify_switch(self, expected_external_id: str) -> bool:
        try:
            raw = self._paths.storage_file.read_bytes()
            external_id, _, _ = parse_identity(raw)
        except (OSError, StateUnreadable, NotLoggedIn):
            return False
        return external_id == expected_external_id

    # ---------- token 凭据切换（扫码/老项目导入的明文账号） ----------

    def plan_token_switch(self, account: dict) -> SwitchPlan:
        """Trae 明文 token 账号切换预演。

        明文 JWT 可以反向构造成客户端能认的 ``iCubeAuthInfo://icube.cloudide`` tc 信封
        （pepper 是公开常量表，``tc_encrypt`` 与 ``tc_decrypt`` 对称），所以**支持**切换。
        与文件切换一样只替换 ``iCubeAuthInfo*`` key，其余 IDE 设置保留。
        """
        storage = self._paths.storage_file
        warnings: list[str] = []
        files = (
            PlannedFile(
                path=storage,
                action="override" if storage.exists() else "create",
                size=storage.stat().st_size if storage.exists() else 0,
                note="用明文 token 重构 iCubeAuthInfo 登录凭据，其余设置保留",
            ),
        )
        if not storage.parent.exists():
            warnings.append(f"目录不存在，将创建：{storage.parent}")
        return SwitchPlan(
            files=files,
            requires_app_exit=self.is_running(),
            warnings=tuple(warnings),
        )

    def apply_token_switch(self, account: dict) -> None:
        """把明文 token 账号写回客户端登录态（真正的 Trae token 切换）。

        关键：``iCubeAuthInfo://icube-dc:<deviceId>`` 是**设备身份**（本机固定），
        切换账号不改设备，所以该 key 原样保留；只把 ``iCubeAuthInfo://icube.cloudide``
        换成目标账号的 token/refreshToken/userId/account，并更新 usertag。
        """
        token = str(account.get("accessToken") or "").strip()
        if not token:
            raise AdapterError("该账号缺少 accessToken，无法切换")
        # 凭据里的 accessToken 可能带「Cloud-IDE-JWT 」前缀，信封里要裸 JWT。
        if token.startswith("Cloud-IDE-JWT "):
            token = token[len("Cloud-IDE-JWT "):]
        refresh_token = str(account.get("refreshToken") or "").strip()
        uid = str(account.get("uid") or "").strip()
        host = str(account.get("host") or "https://api.trae.cn").strip().rstrip("/")
        display_name = str(account.get("displayName") or "").strip() or f"账号{uid[-6:]}"

        if not uid:
            raise AdapterError("该账号缺少 UserID，无法切换")

        exp, iat = _jwt_times(token)
        cloudide = {
            "token": token,
            "refreshToken": refresh_token,
            "expiredAt": _iso_ms(exp) if exp else "",
            "refreshExpiredAt": _iso_ms(iat + 180 * 86400) if iat else "",
            "tokenReleaseAt": _iso_ms(iat) if iat else "",
            "userId": uid,
            "host": host,
            "userRegion": {"region": "CN", "_aiRegion": "CN"},
            "account": _build_account_obj(account, display_name),
        }

        data = self._read_storage()
        # 保留 icube-dc 设备身份 key，只替换 cloudide 与 usertag。
        for key in list(data):
            if str(key).startswith(DC_PREFIX):
                continue
            if str(key) == ICUBE_CLOUDIDE_KEY or str(key) == ICUBE_USERTAG_KEY:
                del data[key]

        data[ICUBE_CLOUDIDE_KEY] = tc_encrypt(json.dumps(cloudide, ensure_ascii=False))

        # usertag：历史登录过的 userId → region 映射，加入/更新目标账号。
        usertag: dict[str, str] = {}
        raw_tag = self._read_usertag()
        if isinstance(raw_tag, dict):
            usertag = {str(k): str(v) for k, v in raw_tag.items()}
        usertag[uid] = "cn"
        data[ICUBE_USERTAG_KEY] = tc_encrypt(json.dumps(usertag, ensure_ascii=False))

        self._write_storage(data)

    def _read_usertag(self) -> dict | None:
        """读当前 storage.json 里的 usertag 明文（解密），失败返回 None。"""
        data = self._read_storage()
        envelope = data.get(ICUBE_USERTAG_KEY)
        if not isinstance(envelope, str) or not envelope:
            return None
        try:
            obj = json.loads(tc_decrypt(envelope))
        except (TraeCryptoError, json.JSONDecodeError, ValueError):
            return None
        return obj if isinstance(obj, dict) else None

    def launch(self) -> bool:
        app = self.app_path()
        if app is None:
            return False
        try:
            # 关键：用 ShellExecute（`os.startfile`）而非 `subprocess.Popen` 启动目标客户端。
            #
            # 真机排障结论：从 WorkBuddy 派生的进程用 `Popen`（哪怕清了环境变量、甚至
            # 最小干净环境）启动 Trae，渲染进程必崩（renderer gone, code -2147483645 =
            # 0x80000003 STATUS_BREAKPOINT），主进程随后退出，表现为「切号后拉不起来」。
            # 而用户双击（ShellExecute）同一 exe 却完全正常。差异不在环境变量，而在
            # `Popen` 的进程创建方式本身（句柄继承 / 创建标志让 Chromium 渲染进程初始化
            # 失败）。`os.startfile` 走 Windows ShellExecute，与双击等价，能稳定拉起。
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
        """退出正在运行的 Trae 进程。切换账号后必须重启才能让客户端重新加载登录态。"""
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
        """等待 Trae 进程退出（terminate 后进程不会立刻消失）。"""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self.is_running():
                return True
            time.sleep(0.2)
        return not self.is_running()

    def snapshot_root(self) -> Path:
        """备份范围 = 整个 globalStorage 目录（含 state.vscdb 与 storage.json）。"""
        return self._paths.state_dir

    def restore_from(self, source_dir: Path) -> None:
        """用备份目录精确还原 globalStorage 目录。"""
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
