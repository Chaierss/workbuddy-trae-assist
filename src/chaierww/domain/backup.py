"""备份与恢复。

原则（来自 SPEC-M1a）：
- 备份必须先校验完整性，校验不过就不算备份成功；
- 备份失败则中止切换，不允许"备份失败但继续切"；
- 恢复前先把当前状态再备份一次，避免恢复本身变成不可逆操作。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import threading
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from chaierww.config import DB_FILENAME
from chaierww.domain.gateway import CONFIG_FILENAME as GATEWAY_CONFIG_FILENAME
from chaierww.domain.gateway import KEYS_FILENAME as GATEWAY_KEYS_FILENAME
from chaierww.domain.gateway import POOL_FILENAME as GATEWAY_POOL_FILENAME
from chaierww.domain.preferences import FILENAME as PREFERENCES_FILENAME
from chaierww.domain.scheduler import FILENAME as SCHEDULES_FILENAME
from chaierww.infra.db import now_iso, transaction

MANIFEST_NAME = "manifest.json"

#: 每个作用域保留的快照**份数**（不是天数）。
#:
#: 滚动口径：正常每天自动落一份；你每点一次「立即备份」也算一份，
#: 新旧一起排队，超过 7 份就从最旧的开始删 —— 手动快照同样参与滚动。
KEEP_SNAPSHOTS = 7

#: 全局备份用的伪作用域：它跨作用域，不属于任何一个客户端。
GLOBAL_SCOPE = "global"

#: 全局备份的分区 → 收录数据目录根下的哪些文件。
#: `accounts` 走 SQLite 在线备份（`_copy_database`），不是文件拷贝。
GLOBAL_PARTS: dict[str, tuple[str, ...]] = {
    "config": (PREFERENCES_FILENAME, SCHEDULES_FILENAME),
    "gateway": (GATEWAY_CONFIG_FILENAME, GATEWAY_KEYS_FILENAME, GATEWAY_POOL_FILENAME),
    "accounts": (DB_FILENAME,),
}

#: 含凭据/密钥的分区：勾选它们必须显式二次确认（对齐「导出默认不含 token」）。
CREDENTIAL_PARTS = frozenset({"accounts", "gateway"})

GLOBAL_PART_LABELS: dict[str, str] = {
    "config": "常规偏好与计划任务",
    "gateway": "网关配置与 Key",
    "accounts": "账号库与登录凭据",
}

#: 默认勾选：只有不含凭据的分区。
DEFAULT_GLOBAL_PARTS: tuple[str, ...] = ("config",)


class BackupError(RuntimeError):
    pass


class SnapshotNotFound(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class Snapshot:
    id: str
    scope: str
    created_at: str
    reason: str
    account_id: str | None
    directory: Path
    total_bytes: int
    file_count: int
    verified: bool
    note: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_id() -> str:
    return f"{now_iso().replace(':', '').replace('-', '')[:15]}-{uuid.uuid4().hex[:8]}"


class BackupService:
    def __init__(self, conn: sqlite3.Connection, backups_dir: Path) -> None:
        self._conn = conn
        self._root = backups_dir
        self._lock = threading.RLock()

    def _scope_dir(self, scope: str) -> Path:
        return self._root / scope

    def create(
        self,
        *,
        scope: str,
        source_dir: Path,
        reason: str,
        account_id: str | None = None,
        note: str = "",
    ) -> Snapshot:
        if not source_dir.is_dir():
            raise BackupError(f"待备份目录不存在：{source_dir}")

        snapshot_id = _snapshot_id()
        dest = self._scope_dir(scope) / snapshot_id
        dest.mkdir(parents=True, exist_ok=False)

        try:
            entries: list[dict[str, object]] = []
            total = 0
            for source in sorted(source_dir.iterdir()):
                if not source.is_file():
                    continue
                if source.name == MANIFEST_NAME:
                    continue
                target = dest / source.name
                shutil.copy2(source, target)
                size = target.stat().st_size
                total += size
                entries.append(
                    {"name": source.name, "size": size, "sha256": _sha256(target)}
                )

            snapshot = self._finalize(
                dest=dest,
                snapshot_id=snapshot_id,
                scope=scope,
                reason=reason,
                source_dir=source_dir,
                entries=entries,
                total=total,
                account_id=account_id,
                note=note,
            )
        except Exception as exc:
            shutil.rmtree(dest, ignore_errors=True)
            raise BackupError(f"备份失败：{exc}") from exc

        # 每次新落一份都按份数滚动：7 份之外最旧的自动让位（手动也算一份）。
        # 放在 try 之外：清理失败不该反过来把刚落好的这份删掉。
        self.prune(scope, KEEP_SNAPSHOTS)
        return snapshot

    def create_global(
        self,
        home: Path,
        parts: Iterable[str],
        *,
        note: str = "",
        confirm_credentials: bool = False,
        reason: str = "manual",
        keep: int = KEEP_SNAPSHOTS,
    ) -> Snapshot:
        """把我们自己数据目录里的**登记文件**按分区做一份快照（scope = `global`）。

        与 `create()`（备份客户端登录态目录）的区别有两点：
        - 这里的分区是「我们的数据」，只收录 `home` 根下的文件，
          子目录（backups / logs / avatars）不在分区内；
        - `accounts` / `gateway` 含凭据，必须显式 `confirm_credentials=True` 才收录。

        `reason` 决定快照来源（manual / auto）：后台每日自动备份传 `auto`，
        它靠这个值判断「今天备过没有」，别混用。`keep` 是滚动保留份数。

        入参不合法一律抛 `ValueError`（接口层映射 400），落盘失败才抛 `BackupError`。
        """
        wanted = list(dict.fromkeys(parts))
        if not wanted:
            raise ValueError("至少要勾选一个分区")
        unknown = [item for item in wanted if item not in GLOBAL_PARTS]
        if unknown:
            raise ValueError(f"未知分区：{'、'.join(unknown)}")
        risky = [item for item in wanted if item in CREDENTIAL_PARTS]
        if risky and not confirm_credentials:
            labels = "、".join(GLOBAL_PART_LABELS[item] for item in risky)
            raise ValueError(f"{labels} 含凭据，必须二次确认后才写进备份")

        snapshot_id = _snapshot_id()
        dest = self._scope_dir(GLOBAL_SCOPE) / snapshot_id
        dest.mkdir(parents=True, exist_ok=False)

        try:
            entries: list[dict[str, object]] = []
            total = 0
            for part in wanted:
                for name in GLOBAL_PARTS[part]:
                    source = home / name
                    if not source.is_file():
                        continue
                    target = dest / name
                    if part == "accounts":
                        self._copy_database(target)
                    else:
                        shutil.copy2(source, target)
                    size = target.stat().st_size
                    total += size
                    entries.append(
                        {
                            "name": name,
                            "part": part,
                            "size": size,
                            "sha256": _sha256(target),
                        }
                    )

            if not entries:
                raise BackupError("勾选的分区里一个文件都没有，没什么可备份")
            snapshot = self._finalize(
                dest=dest,
                snapshot_id=snapshot_id,
                scope=GLOBAL_SCOPE,
                reason=reason,
                source_dir=home,
                entries=entries,
                total=total,
                account_id=None,
                note=note,
                extra={"parts": wanted},
            )
        except Exception as exc:
            shutil.rmtree(dest, ignore_errors=True)
            if isinstance(exc, BackupError):
                raise
            raise BackupError(f"备份失败：{exc}") from exc

        # 全局备份同样按份数滚动（默认不含凭据的那几份也一起算）。
        self.prune(GLOBAL_SCOPE, keep)
        return snapshot

    def _finalize(
        self,
        *,
        dest: Path,
        snapshot_id: str,
        scope: str,
        reason: str,
        source_dir: Path,
        entries: list[dict[str, object]],
        total: int,
        account_id: str | None,
        note: str,
        extra: dict[str, object] | None = None,
    ) -> Snapshot:
        """写清单 → 逐文件自校验 → 落库。清单校验不过就不算备份成功。"""
        manifest: dict[str, object] = {
            "version": 1,
            "scope": scope,
            "createdAt": now_iso(),
            "reason": reason,
            "sourceDir": str(source_dir),
            "files": entries,
        }
        if extra:
            manifest.update(extra)
        (dest / MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._verify_manifest(dest, manifest)

        snapshot = Snapshot(
            id=snapshot_id,
            scope=scope,
            created_at=now_iso(),
            reason=reason,
            account_id=account_id,
            directory=dest,
            total_bytes=total,
            file_count=len(entries),
            verified=True,
            note=note,
        )
        with self._lock, transaction(self._conn):
            self._conn.execute(
                """
                INSERT INTO snapshots (
                    id, scope, created_at, reason, account_id, directory,
                    total_bytes, file_count, verified, note
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.id,
                    snapshot.scope,
                    snapshot.created_at,
                    snapshot.reason,
                    snapshot.account_id,
                    str(snapshot.directory),
                    snapshot.total_bytes,
                    snapshot.file_count,
                    1,
                    snapshot.note,
                ),
            )
        return snapshot

    def _copy_database(self, target: Path) -> None:
        """用 SQLite 在线备份接口复制库文件。

        库跑在 WAL 模式下，直接拷 `.db` 会漏掉 `-wal` 里尚未落盘的事务，
        所以这里让 SQLite 自己产出一致性副本。
        """
        out = sqlite3.connect(str(target))
        try:
            with self._lock:
                self._conn.backup(out)
        finally:
            out.close()

    def _verify_manifest(self, directory: Path, manifest: dict[str, object]) -> None:
        files = manifest.get("files")
        if not isinstance(files, list):
            raise BackupError("备份清单格式异常")

        for entry in files:
            if not isinstance(entry, dict):
                raise BackupError("备份清单条目格式异常")
            name = entry.get("name")
            expected_hash = entry.get("sha256")
            expected_size = entry.get("size")
            path = directory / str(name)
            if not path.is_file():
                raise BackupError(f"备份缺失文件：{name}")
            if path.stat().st_size != expected_size:
                raise BackupError(f"备份文件大小不符：{name}")
            if _sha256(path) != expected_hash:
                raise BackupError(f"备份文件校验不通过：{name}")

    # ---------- 查询 ----------

    def get(self, snapshot_id: str) -> Snapshot:
        row = self._conn.execute(
            "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            raise SnapshotNotFound(snapshot_id)
        return self._row(row)

    def list(self, scope: str, limit: int = 50) -> list[Snapshot]:
        rows = self._conn.execute(
            "SELECT * FROM snapshots WHERE scope = ? ORDER BY created_at DESC LIMIT ?",
            (scope, limit),
        ).fetchall()
        return [self._row(row) for row in rows]

    def latest(self, scope: str, reason: str) -> Snapshot | None:
        """某种原因下最近的一份快照（自动备份用它判断「今天跑过没有」）。"""
        row = self._conn.execute(
            "SELECT * FROM snapshots WHERE scope = ? AND reason = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (scope, reason),
        ).fetchone()
        return self._row(row) if row is not None else None

    @staticmethod
    def _row(row: sqlite3.Row) -> Snapshot:
        return Snapshot(
            id=row["id"],
            scope=row["scope"],
            created_at=row["created_at"],
            reason=row["reason"],
            account_id=row["account_id"],
            directory=Path(row["directory"]),
            total_bytes=row["total_bytes"],
            file_count=row["file_count"],
            verified=bool(row["verified"]),
            note=row["note"],
        )

    def verify(self, snapshot_id: str) -> bool:
        snapshot = self.get(snapshot_id)
        manifest_path = snapshot.directory / MANIFEST_NAME
        if not manifest_path.is_file():
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        try:
            self._verify_manifest(snapshot.directory, manifest)
        except BackupError:
            return False
        return True

    # ---------- 维护 ----------

    def prune(self, scope: str, keep: int = KEEP_SNAPSHOTS) -> int:
        """按**份数**滚动清理：只留最近 `keep` 份，多余的最旧先删。

        不分 reason —— 自动、切换前、手动、恢复前一起排队。也就是用户说的
        「正常每天一次，手动刷新也滚动覆盖 7 次」。
        """
        if keep < 1:
            raise ValueError("保留份数至少为 1")
        rows = self._conn.execute(
            "SELECT id FROM snapshots WHERE scope = ? "
            "ORDER BY created_at DESC, rowid DESC",
            (scope,),
        ).fetchall()
        stale = [row["id"] for row in rows[keep:]]
        for snapshot_id in stale:
            snapshot = self.get(snapshot_id)
            shutil.rmtree(snapshot.directory, ignore_errors=True)
            with self._lock, transaction(self._conn):
                self._conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
        return len(stale)

    def delete(self, snapshot_id: str) -> None:
        snapshot = self.get(snapshot_id)
        shutil.rmtree(snapshot.directory, ignore_errors=True)
        with self._lock, transaction(self._conn):
            self._conn.execute("DELETE FROM snapshots WHERE id = ?", (snapshot_id,))
