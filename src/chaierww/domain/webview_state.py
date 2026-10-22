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
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: 分区名前缀与分隔。形如 ``wb-webview-<uid>-<purpose>``。
PREFIX = "wb-webview-"

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


def snapshot(uid: str) -> int:
    """把某个 uid 的分区存到本机快照目录，返回复制的分区目录数。

    快照是「覆盖式」的：每次都整份重写，避免旧 cookie 与新 cookie 混在一起。
    """
    parts = [item for item in discover() if item.uid == uid]
    if not parts:
        return 0
    target = snapshot_root() / uid
    # 先删旧快照：只补不清会让已登出的账号在快照里残留登录态。
    with contextlib.suppress(OSError):
        shutil.rmtree(target)
    count = 0
    for item in parts:
        if _copy_partition(item.path, target / item.purpose):
            count += 1
    return count


def has_snapshot(uid: str) -> bool:
    root = snapshot_root() / uid
    if not root.is_dir():
        return False
    with contextlib.suppress(OSError):
        return any(root.iterdir())
    return False


def restore(uid: str) -> int:
    """把某个 uid 的快照铺回客户端分区目录，返回铺回的分区目录数。"""
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
    for item in discover():
        if item.uid != uid:
            continue
        with contextlib.suppress(OSError):
            shutil.rmtree(item.path)
            removed += 1
    return removed


def snapshot_bytes(uid: str) -> int:
    """快照占用的字节数（诊断用）。"""
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
