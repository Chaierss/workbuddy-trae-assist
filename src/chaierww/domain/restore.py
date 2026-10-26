"""全局备份的「准备恢复 → 重启 → 启动时应用」。

原地恢复必须发生在**数据库连接打开之前**，所以分两步：
1. 界面/接口只写 ``restore-pending.json``（准备恢复）；
2. 下次启动时 ``apply_pending_restore`` 校验备份、留安全副本、覆盖、写结果。

覆盖前一定先备份当前文件（``backups/restore-safety/<时间>/``）；
任何一步失败都会按安全副本回滚，并把原因写进 ``restore-result.json``。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chaierww.infra.fsutil import write_json_atomic

PENDING_NAME = "restore-pending.json"
RESULT_NAME = "restore-result.json"

_ALLOWED_SCOPES = {"global"}


class RestoreError(RuntimeError):
    pass


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _snapshot_dir(home: Path, snapshot_id: str) -> Path:
    if not snapshot_id or "/" in snapshot_id or "\\" in snapshot_id:
        raise RestoreError("备份 ID 不合法")
    target = (home / "backups" / "global" / snapshot_id).resolve()
    root = (home / "backups" / "global").resolve()
    if not target.is_relative_to(root):
        raise RestoreError("备份路径越界")
    if not target.is_dir():
        raise RestoreError("备份不存在或已被删除")
    return target


def _load_manifest(directory: Path) -> dict[str, Any]:
    path = directory / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RestoreError(f"备份清单不可读：{exc}") from exc
    if not isinstance(data, dict) or data.get("scope") not in _ALLOWED_SCOPES:
        raise RestoreError("不是全局备份，不能原地恢复")
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise RestoreError("备份清单里没有文件")
    return data


def _verify(directory: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    files: list[dict[str, Any]] = []
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "")
        # 只接受单层文件名：不允许 ../ 或子目录，防止被构造的清单带出目录。
        if not name or Path(name).name != name:
            raise RestoreError(f"备份清单里的文件名不合法：{name}")
        path = directory / name
        if not path.is_file():
            raise RestoreError(f"备份文件缺失：{name}")
        expected = str(entry.get("sha256") or "")
        if expected and _sha256(path) != expected:
            raise RestoreError(f"备份文件校验失败：{name}")
        files.append({"name": name, "path": str(path)})
    if not files:
        raise RestoreError("备份清单里没有可用文件")
    return files


def _copy_history(home: Path, files: list[dict[str, Any]]) -> Path:
    """覆盖前把「当前版本」复制到安全目录，失败时从这里回滚。"""
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    history = home / "backups" / "restore-safety" / f"{stamp}-{uuid.uuid4().hex[:6]}"
    history.mkdir(parents=True, exist_ok=True)
    for entry in files:
        source = home / entry["name"]
        if source.is_file():
            shutil.copy2(source, history / entry["name"])
    write_json_atomic(
        history / "manifest.json",
        {"at": _now(), "files": [entry["name"] for entry in files]},
    )
    return history


def _rollback(home: Path, history: Path, files: list[dict[str, Any]]) -> None:
    for entry in files:
        backup = history / entry["name"]
        target = home / entry["name"]
        if backup.is_file():
            shutil.copy2(backup, target)


def _database_sidecars(home: Path, name: str) -> list[Path]:
    if name != "chaierww.db":
        return []
    return [home / f"{name}-wal", home / f"{name}-shm"]


def stage_restore(home: Path, snapshot_id: str) -> dict[str, Any]:
    """只写 pending 文件，不碰任何数据；重启后由启动流程执行。"""
    directory = _snapshot_dir(home, snapshot_id)
    manifest = _load_manifest(directory)
    files = _verify(directory, manifest)
    pending = {
        "snapshotId": snapshot_id,
        "directory": str(directory),
        "at": _now(),
        "files": [entry["name"] for entry in files],
    }
    write_json_atomic(home / PENDING_NAME, pending)
    return pending


def read_pending(home: Path) -> dict[str, Any] | None:
    path = home / PENDING_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cancel_pending(home: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        (home / PENDING_NAME).unlink()


def read_result(home: Path) -> dict[str, Any] | None:
    path = home / RESULT_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def apply_pending_restore(home: Path) -> dict[str, Any] | None:
    """启动时执行 pending 恢复；没有 pending 返回 ``None``。

    必须在 ``open_database`` 之前调用：覆盖数据库时不能有活动连接。
    """
    pending = read_pending(home)
    if pending is None:
        return None
    result: dict[str, Any]
    try:
        snapshot_id = str(pending.get("snapshotId") or "")
        directory = _snapshot_dir(home, snapshot_id)
        manifest = _load_manifest(directory)
        files = _verify(directory, manifest)
        history = _copy_history(home, files)
        try:
            for entry in files:
                target = home / entry["name"]
                for sidecar in _database_sidecars(home, entry["name"]):
                    with contextlib.suppress(FileNotFoundError):
                        sidecar.unlink()
                shutil.copy2(Path(entry["path"]), target)
        except Exception:
            _rollback(home, history, files)
            raise
        result = {
            "ok": True,
            "snapshotId": snapshot_id,
            "at": _now(),
            "files": [entry["name"] for entry in files],
            "safetyDir": str(history),
        }
    except Exception as exc:  # noqa: BLE001 - 恢复失败必须落盘成可读结果
        result = {
            "ok": False,
            "snapshotId": str(pending.get("snapshotId") or ""),
            "at": _now(),
            "error": str(exc),
        }
    write_json_atomic(home / RESULT_NAME, result)
    cancel_pending(home)
    return result
