"""客户端「网页版」登录态：按账号隔离的 Chromium 分区。

## 为什么需要单独搬

切号只重写一个文件（``workbuddy-desktop.info``），但客户端里的网页——
乐享、腾讯文档、频道应用、预览——跑在**按账号隔离**的 Chromium 分区里：

    ~/.workbuddy/app/session/Partitions/wb-webview-<uid>-<purpose>

分区名里的 ``<uid>`` 就是号池账号的 ``external_id``（已在本机核对：
``wb-webview-2f5ad572-...-lexiang-auth`` ↔ 号池 ``180****2993``）。

于是出现这种现象：切到一个从没在本机网页版登录过的号，该 uid 的分区不存在，
桌面端能用、网页版没有登录态；切回旧号又好了。本模块负责把这块随账号一起搬。

## 边界（诚实说明）

1. **只在同机有效**。Chromium 的 cookie 用 ``Local State`` 里 DPAPI 保护的密钥加密，
   密钥绑当前 Windows 用户；换台机器复制过去解不开。所以网页版登录态**不进凭据表、
   不进导出包**，只在本机 ``~/.chaierww/webview/<uid>/`` 留快照。
2. **只搬白名单**。缓存（Cache / Code Cache / GPUCache 等）体积大且与登录无关，
   一律不搬 —— 一个 browser-preview 分区光缓存就 50 MB 起。
3. **客户端必须已退出**。切号流程本来就先退出客户端，这里依赖那个前提；
   分区文件被占用时复制会失败，调用方按「不阻断切号」处理。
"""

from __future__ import annotations

import contextlib
import json
import logging
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 分区名前缀与分隔。形如 ``wb-webview-<uid>-<purpose>``。
PREFIX = "wb-webview-"

#: 快照清单文件名。写在 ``~/.chaierww/webview/<uid>/manifest.json``。
#:
#: 目的不是「描述快照」，而是**代际校验**：号池里的 token 可能已经续期过 N 次，
#: 而快照还是 M 次之前留下的。清单让调用方能判断「这份 cookie 是不是同一代」。
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1

#: 要搬的条目（登录态真正落在这里）。
KEEP_NAMES: frozenset[str] = frozenset(
    {
        "Network",  # Cookies / TransportSecurity / Trust Tokens
        "Local Storage",
        "Session Storage",
        "IndexedDB",
        "WebStorage",
        "SharedStorage",
        "Preferences",
    }
)

#: 明确不搬的：缓存、GPU 缓存、blob 等，体积大且与登录无关。
SKIP_NAMES: frozenset[str] = frozenset(
    {
        "Cache",
        "Code Cache",
        "GPUCache",
        "DawnGraphiteCache",
        "DawnWebGPUCache",
        "blob_storage",
        "Shared Dictionary",
        "DIPS",
        "VideoDecodeStats",
    }
)


@dataclass(frozen=True, slots=True)
class WebviewPartition:
    """一个 ``wb-webview-*`` 分区。"""

    uid: str
    purpose: str
    path: Path

    @property
    def name(self) -> str:
        return partition_name(self.uid, self.purpose)


def session_dir() -> Path:
    """客户端 Chromium 会话目录（网页版的存储都在它下面）。"""
    return Path.home() / ".workbuddy" / "app" / "session"


def partitions_dir() -> Path:
    return session_dir() / "Partitions"


def snapshot_root() -> Path:
    """本机网页版登录态快照目录（不进凭据库、不进导出包）。"""
    return Path.home() / ".chaierww" / "webview"


def partition_name(uid: str, purpose: str) -> str:
    return f"{PREFIX}{uid}-{purpose}"


def parse_partition_name(name: str) -> tuple[str, str] | None:
    """``wb-webview-<uid>-<purpose>`` → ``(uid, purpose)``。

    uid 是带连字符的 UUID，不能简单按 ``-`` 切；用「前 36 个字符是 UUID」这个
    Chromium 侧的既有格式来定位分隔点。
    """
    if not name.startswith(PREFIX):
        return None
    rest = name[len(PREFIX) :]
    if len(rest) <= 37:
        return None
    uid, sep, purpose = rest[:36], rest[36], rest[37:]
    if sep != "-" or not purpose:
        return None
    return uid, purpose


def discover() -> list[WebviewPartition]:
    """列出磁盘上真实存在的网页版分区。目录不在（客户端没装/没跑过）就返回空。"""
    root = partitions_dir()
    if not root.is_dir():
        return []
    found: list[WebviewPartition] = []
    try:
        children = list(root.iterdir())
    except OSError as exc:  # pragma: no cover - 权限异常按「没有」处理
        logger.warning("读取网页版分区目录失败：%s", exc)
        return []
    for child in children:
        parsed = parse_partition_name(child.name)
        if parsed is None or not child.is_dir():
            continue
        found.append(WebviewPartition(uid=parsed[0], purpose=parsed[1], path=child))
    return sorted(found, key=lambda item: (item.uid, item.purpose))


def uids_on_disk() -> set[str]:
    return {item.uid for item in discover()}


def _copy_entry(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True, symlinks=True)
    else:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _copy_partition(src: Path, dst: Path) -> int:
    """只复制白名单条目，返回复制的条目数。"""
    copied = 0
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        if entry.name in SKIP_NAMES or entry.name not in KEEP_NAMES:
            continue
        with contextlib.suppress(OSError):
            _copy_entry(entry, dst / entry.name)
            copied += 1
    return copied


def _unsafe_uid(uid: str) -> bool:
    """uid 要拼进路径，所以先确认它能当「单层目录名」用。

    uid 来自账号的 ``external_id``（客户端登录态解析而来），正常是 UUID。但既然
    要拼路径，就不能接受 ``.`` / ``..`` / 带分隔符或盘符的形状 —— 这类值会让
    ``snapshot_root() / uid`` 指到快照目录之外去（例如 ``..`` 会变成 ``~/.chaierww``）。
    正常账号不会被这条拦到，它只是把「拼路径必须先校验」这条规矩落成代码。
    """
    if not uid or uid in {".", ".."}:
        return True
    return any(token in uid for token in ("/", "\\", ":", "\x00"))


def snapshot(uid: str, *, generation: int = 0) -> int:
    """把某个 uid 的分区存到本机快照目录，返回复制的分区目录数。

    快照是「覆盖式」的：每次都整份重写，避免旧 cookie 与新 cookie 混在一起。
    ``generation`` 记下本次快照对应的登录态代际，供 ``restore_checked`` 校验。
    """
    if _unsafe_uid(uid):
        return 0
    parts = [item for item in discover() if item.uid == uid]
    if not parts:
        return 0
    target = snapshot_root() / uid
    # 先删旧快照：只补不清会让已登出的账号在快照里残留登录态。
    with contextlib.suppress(OSError):
        shutil.rmtree(target)
    count = 0
    purposes: list[str] = []
    for item in parts:
        if _copy_partition(item.path, target / item.purpose):
            count += 1
            purposes.append(item.purpose)
    if count:
        _write_manifest(
            target,
            {
                "version": MANIFEST_VERSION,
                "uid": uid,
                "credentialGeneration": int(generation or 0),
                "capturedAt": datetime.now(UTC).replace(microsecond=0).isoformat(),
                "purposes": sorted(purposes),
                "entries": count,
                "bytes": _dir_bytes(target),
            },
        )
    return count


def _write_manifest(root: Path, payload: dict[str, Any]) -> None:
    path = root / MANIFEST_NAME
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def read_manifest(uid: str) -> dict[str, Any] | None:
    """读快照清单；缺失或损坏返回 ``None``（旧版本快照没有清单，属正常情况）。"""
    if _unsafe_uid(uid):
        return None
    path = snapshot_root() / uid / MANIFEST_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _dir_bytes(root: Path) -> int:
    total = 0
    for path in root.rglob("*"):
        with contextlib.suppress(OSError):
            if path.is_file():
                total += path.stat().st_size
    return total


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """网页版快照的铺回结果。

    ``reason`` 只在「没铺」或「铺了但该提醒」时非空；``error=True`` 表示
    明确不能铺（账号标识不符），调用方应把分区清空并提示重登。
    """

    partitions: int = 0
    reason: str | None = None
    error: bool = False
    warning: bool = False


def restore_checked(uid: str, *, generation: int) -> RestoreResult:
    """带清单校验的铺回。

    校验梯度（刻意分级，避免「一次 token 续期就要求用户重登网页版」）：

    1. 快照里没有清单 → 按旧版快照直接铺，返回警告；
    2. 清单里的 uid 与请求不一致 → **拒绝**（这是真的串号风险）；
    3. 代际不一致 → 仍然铺，但返回警告（cookie 与 token 同属一个账号，
       刷新 token 不会让已有 cookie 变成别人的）。
    """
    manifest = read_manifest(uid)
    if manifest is None:
        if not has_snapshot(uid):
            return RestoreResult()
        count = restore(uid)
        return RestoreResult(
            partitions=count,
            reason="该账号的网页版快照来自旧版本（无代际信息），已按原样铺回",
            warning=True,
        )
    recorded_uid = str(manifest.get("uid") or "")
    if recorded_uid and recorded_uid != uid:
        return RestoreResult(
            reason=f"快照属于另一个账号（{recorded_uid[:8]}…），已拒绝铺回",
            error=True,
        )
    count = restore(uid)
    recorded_gen = int(manifest.get("credentialGeneration") or 0)
    if generation and recorded_gen and recorded_gen != generation:
        return RestoreResult(
            partitions=count,
            reason=(
                f"网页版快照与当前登录态不同代（快照 {recorded_gen} → 当前 {generation}）；"
                "已铺回，若网页版显示旧身份请重新登录一次"
            ),
            warning=True,
        )
    return RestoreResult(partitions=count)


def has_snapshot(uid: str) -> bool:
    if _unsafe_uid(uid):
        return False
    root = snapshot_root() / uid
    if not root.is_dir():
        return False
    with contextlib.suppress(OSError):
        return any(root.iterdir())
    return False


def restore(uid: str) -> int:
    """把某个 uid 的快照铺回客户端分区目录，返回铺回的分区目录数。"""
    if _unsafe_uid(uid):
        return 0
    root = snapshot_root() / uid
    if not root.is_dir():
        return 0
    count = 0
    for purpose_dir in root.iterdir():
        if not purpose_dir.is_dir():
            continue
        target = partitions_dir() / partition_name(uid, purpose_dir.name)
        if _copy_partition(purpose_dir, target):
            count += 1
    return count


def drop(uid: str) -> int:
    """删掉某个 uid 在客户端里的分区。

    用在「目标账号没有快照」时：留着旧号的分区只会让网页版继续显示上一个身份，
    比空着更糟。返回删除的分区目录数。
    """
    removed = 0
    if _unsafe_uid(uid):
        return removed
    for item in discover():
        if item.uid != uid:
            continue
        with contextlib.suppress(OSError):
            shutil.rmtree(item.path)
            removed += 1
    return removed


def drop_snapshot(uid: str) -> bool:
    """删掉某个 uid 在本机的网页版登录态快照（永久删除账号时用）。

    与 :func:`drop` 的区别：``drop`` 删的是**客户端里的分区**（切号流程用），
    这里删的是本工具自己的快照副本。快照是「换机无效」的本机数据，账号永久删除后
    留着只会是孤儿，所以一并清掉。

    失败**不吞异常**（对照 ``drop`` 的 ``suppress``）：永久删除需要把「哪些关联
    数据没清干净」如实报给用户，由调用方决定记 warning 而不是静默。
    """
    if _unsafe_uid(uid):
        return False
    target = snapshot_root() / uid
    if not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


def snapshot_bytes(uid: str) -> int:
    """快照占用的字节数（诊断用）。"""
    if _unsafe_uid(uid):
        return 0
    root = snapshot_root() / uid
    if not root.is_dir():
        return 0
    total = 0
    for path in root.rglob("*"):
        with contextlib.suppress(OSError):
            if path.is_file():
                total += path.stat().st_size
    return total


def partition_bytes(path: Path) -> int:
    """分区占用的字节数（诊断用，只算白名单内的条目）。"""
    total = 0
    if not path.is_dir():
        return total
    for entry in path.iterdir():
        if entry.name not in KEEP_NAMES:
            continue
        if entry.is_file():
            with contextlib.suppress(OSError):
                total += entry.stat().st_size
            continue
        for sub in entry.rglob("*"):
            with contextlib.suppress(OSError):
                if sub.is_file():
                    total += sub.stat().st_size
    return total
