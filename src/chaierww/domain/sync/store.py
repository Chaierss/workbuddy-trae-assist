"""内容寻址存储（CAS）：同一份会话正文全盘只存一份。

目录：``~/.chaierww/sync/blobs/<前两位>/<sha256>``。

- 写入是原子的（临时文件 → ``os.replace``），与 ``infra/fsutil`` 同款纪律；
- 读取按 sha256 校验，磁盘层静默损坏能被发现；
- GC 只删除「没有任何 ``sync_items`` 引用」的 blob，绝不按时间瞎删。
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import uuid
from pathlib import Path

BLOB_DIRNAME = "blobs"


class BlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root / BLOB_DIRNAME

    @property
    def root(self) -> Path:
        return self._root

    def _path(self, sha: str) -> Path:
        if len(sha) < 3 or any(ch not in "0123456789abcdef" for ch in sha):
            raise ValueError("非法的内容哈希")
        return self._root / sha[:2] / sha

    def put(self, text: str) -> tuple[str, int]:
        """存一份文本，返回 ``(sha256, 字节数)``。已存在则直接复用。"""
        data = text.encode("utf-8", "replace")
        sha = hashlib.sha256(data).hexdigest()
        target = self._path(sha)
        if target.is_file():
            return sha, len(data)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        finally:
            with contextlib.suppress(FileNotFoundError):
                tmp.unlink()
        return sha, len(data)

    def get(self, sha: str) -> str | None:
        """读回文本并校验哈希；缺失或损坏都返回 ``None``（绝不返回可疑内容）。"""
        if not sha:
            return None
        try:
            path = self._path(sha)
        except ValueError:
            return None
        if not path.is_file():
            return None
        try:
            data = path.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(data).hexdigest() != sha:
            return None
        return data.decode("utf-8", "replace")

    def exists(self, sha: str) -> bool:
        try:
            return bool(sha) and self._path(sha).is_file()
        except ValueError:
            return False

    def gc(self, referenced: set[str]) -> int:
        """删掉未被引用的 blob，返回删除数。只处理合法命名的文件。"""
        removed = 0
        if not self._root.is_dir():
            return 0
        for bucket in self._root.iterdir():
            if not bucket.is_dir():
                continue
            for path in bucket.iterdir():
                if not path.is_file():
                    continue
                if path.name in referenced:
                    continue
                with contextlib.suppress(OSError):
                    path.unlink()
                    removed += 1
            with contextlib.suppress(OSError):
                if not any(bucket.iterdir()):
                    bucket.rmdir()
        return removed

    def total_bytes(self) -> int:
        total = 0
        if not self._root.is_dir():
            return total
        for path in self._root.rglob("*"):
            with contextlib.suppress(OSError):
                if path.is_file():
                    total += path.stat().st_size
        return total
