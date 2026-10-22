"""客户端个人档案与工作区元数据的迁移（账号级）。

对照旧项目只迁「账号级」数据的纪律：全局客户端缓存与加密目录刻意不动。

## 覆盖范围

1. **个人档案与偏好 `profile`**
   - ``user-{uid}-personal/``：``IDENTITY.md`` / ``SOUL.md`` / ``USER.md`` / ``BOOTSTRAP.md``
     按行去重合并；``*.json``（如 ``workspace-state.json``）JSON 深合并；
   - ``{uid}/``：客户端界面状态（``sidebar-list-snapshot.json`` / ``startup-route.json``）深合并；
   - ``storage/user-{uid}/``、``storage/user-{uid}-personal/``：目录内 ``*.json`` 深合并；
   - ``security/{uid}/``：**明确跳过**（加密口令库，抄过去无意义，报告里列出原因）。
2. **工作区与项目元数据 `workspaces`**
   - 源账号的 ``user-{uid}-personal/workspace-state.json`` → 根级活动
     ``workspace-state.json``（JSON 并集深合并）：切换后目标账号能看到源账号的工作区列表。
   - 根 ``workspace-display-names.json`` 若源侧个人目录里有同名文件，一并并集合并。

会话正文与伴生文件（``{cid}.meta.json`` / ``{cid}.file-rollback.ndjson``）由
``domain/session_migration.py`` 负责，这里不重复。

改动前备份目标文件到 ``backups/{profile|workspaces}/{时间戳}/``；备份失败不阻断。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from chaierww.domain.migration import MigrationError, merge_json_text, merge_memory_text
from chaierww.domain.sessions import workbuddy_data_dir

#: 源账号个人目录里、需要并集进根级工作区元数据的文件名。
WORKSPACE_SOURCE_FILES = ("workspace-state.json", "workspace-display-names.json")


# ---------- 路径 ----------


def personal_dir(uid: str) -> Path:
    return workbuddy_data_dir() / f"user-{uid}-personal"


def account_ui_dir(uid: str) -> Path:
    return workbuddy_data_dir() / uid


def storage_dir(uid: str) -> Path:
    return workbuddy_data_dir() / "storage" / f"user-{uid}"


def storage_personal_dir(uid: str) -> Path:
    return workbuddy_data_dir() / "storage" / f"user-{uid}-personal"


def security_dir(uid: str) -> Path:
    return workbuddy_data_dir() / "security" / uid


# ---------- 数据结构 ----------


@dataclass
class MergePlanItem:
    kind: str  # "text" | "json" | "copy"
    src: Path
    dst: Path
    where: str  # 给人看的相对位置，如 ``user-*-personal/SOUL.md``


@dataclass
class ProfileMergeResult:
    merged_files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    backup: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.merged_files)

    def payload(self) -> dict[str, object]:
        return {
            "mergedFiles": self.merged_files,
            "mergedCount": len(self.merged_files),
            "skipped": self.skipped,
            "errors": self.errors,
            "changed": self.changed,
            "backup": self.backup,
        }


@dataclass
class WorkspaceMergeResult:
    merged_files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    backup: str | None = None

    @property
    def changed(self) -> bool:
        return bool(self.merged_files)

    def payload(self) -> dict[str, object]:
        return {
            "mergedFiles": self.merged_files,
            "mergedCount": len(self.merged_files),
            "skipped": self.skipped,
            "errors": self.errors,
            "changed": self.changed,
            "backup": self.backup,
        }


# ---------- 通用 ----------


def _iter_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    try:
        return sorted(p for p in directory.iterdir() if p.is_file())
    except OSError:
        return []


def _backup_root(kind: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return workbuddy_data_dir() / "backups" / kind / stamp


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".migrate.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _backup_text(path: Path, original: str, backup_dir: Path) -> None:
    try:
        dest = backup_dir / path.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(original, encoding="utf-8")
    except OSError:
        return


def _item_plan_result(item: MergePlanItem) -> tuple[bool, str | None]:
    """纯计算：返回 ``(是否会改动, 合并后文本)``；不改任何文件。"""
    source_text = item.src.read_text(encoding="utf-8", errors="replace")
    target_text = (
        item.dst.read_text(encoding="utf-8", errors="replace") if item.dst.is_file() else ""
    )
    if item.kind == "text":
        merged, outcome = merge_memory_text(target_text, source_text, item.where)
        return outcome.changed, merged
    if item.kind == "json":
        try:
            merged, outcome = merge_json_text(target_text, source_text)
        except MigrationError as exc:
            raise MigrationError(f"{item.where}：{exc}") from exc
        return outcome.changed, merged
    # copy：目标不存在才复制
    if item.dst.is_file():
        return False, None
    return True, source_text


def _apply_item(item: MergePlanItem, backup_dir: Path) -> bool:
    would_change, merged = _item_plan_result(item)
    if not would_change or merged is None:
        return False
    if item.dst.is_file():
        original = item.dst.read_text(encoding="utf-8", errors="replace")
        _backup_text(item.dst, original, backup_dir)
    _atomic_write(item.dst, merged)
    return True


# ---------- 个人档案与偏好 ----------


def _plan_profile(source_uid: str, target_uid: str) -> list[MergePlanItem]:
    items: list[MergePlanItem] = []

    src_personal = personal_dir(source_uid)
    dst_personal = personal_dir(target_uid)
    for src in _iter_files(src_personal):
        suffix = src.suffix.lower()
        kind = "text" if suffix == ".md" else "json" if suffix == ".json" else "copy"
        items.append(
            MergePlanItem(kind, src, dst_personal / src.name, f"{src.parent.name}/{src.name}")
        )

    src_ui = account_ui_dir(source_uid)
    dst_ui = account_ui_dir(target_uid)
    for src in _iter_files(src_ui):
        if src.suffix.lower() != ".json":
            continue
        items.append(MergePlanItem("json", src, dst_ui / src.name, f"{src.parent.name}/{src.name}"))

    for src_dir, dst_dir in (
        (storage_dir(source_uid), storage_dir(target_uid)),
        (storage_personal_dir(source_uid), storage_personal_dir(target_uid)),
    ):
        for src in _iter_files(src_dir):
            if src.suffix.lower() != ".json":
                continue
            items.append(
                MergePlanItem("json", src, dst_dir / src.name, f"{src.parent.name}/{src.name}")
            )
    return items


def preview_profile(source_uid: str, target_uid: str) -> dict[str, object]:
    """只读预检：返回将合并 / 将跳过 / 将被排除的清单，不写任何文件。"""
    source_uid = source_uid.strip()
    target_uid = target_uid.strip()
    if not source_uid or not target_uid:
        return {"willMerge": [], "willSkip": [], "skipped": []}
    will_merge: list[str] = []
    will_skip: list[str] = []
    for item in _plan_profile(source_uid, target_uid):
        try:
            changed, _ = _item_plan_result(item)
        except (MigrationError, OSError):
            will_skip.append(item.where)
            continue
        (will_merge if changed else will_skip).append(item.where)
    skipped = _security_skip_note(source_uid)
    return {"willMerge": will_merge, "willSkip": will_skip, "skipped": skipped}


def _security_skip_note(source_uid: str) -> list[str]:
    if security_dir(source_uid).is_dir():
        return ["security/{uid}（加密口令库，刻意不迁移）"]
    return []


def merge_profile_files(source_uid: str, target_uid: str) -> ProfileMergeResult:
    """把源账号的个人档案与偏好合并进目标账号（幂等）。"""
    source_uid = source_uid.strip()
    target_uid = target_uid.strip()
    if source_uid == target_uid:
        raise MigrationError("源账号与目标账号相同")

    result = ProfileMergeResult()
    items = _plan_profile(source_uid, target_uid)
    backup_dir = _backup_root("profile")
    for item in items:
        try:
            if _apply_item(item, backup_dir):
                result.merged_files.append(item.where)
                if result.backup is None:
                    result.backup = str(backup_dir)
        except (MigrationError, OSError) as exc:
            result.errors.append({"where": item.where, "error": str(exc)})
    result.skipped.extend(_security_skip_note(source_uid))
    return result


# ---------- 工作区与项目元数据 ----------


def _workspace_items(source_uid: str) -> list[MergePlanItem]:
    root = workbuddy_data_dir()
    personal = personal_dir(source_uid)
    items: list[MergePlanItem] = []
    for name in WORKSPACE_SOURCE_FILES:
        src = personal / name
        if src.is_file():
            items.append(
                MergePlanItem("json", src, root / name, f"user-*-personal/{name} → {name}")
            )
    return items


def preview_workspaces(source_uid: str) -> dict[str, object]:
    """只读预检：工作区元数据的将合并 / 将跳过清单。"""
    will_merge: list[str] = []
    will_skip: list[str] = []
    for item in _workspace_items(source_uid.strip()):
        try:
            changed, _ = _item_plan_result(item)
        except (MigrationError, OSError):
            will_skip.append(item.where)
            continue
        (will_merge if changed else will_skip).append(item.where)
    return {"willMerge": will_merge, "willSkip": will_skip, "skipped": []}


def merge_workspace_metadata(source_uid: str, target_uid: str) -> WorkspaceMergeResult:
    """把源账号的工作区状态并集进根级活动工作区元数据（幂等）。

    切换后根级文件通常已被客户端改写成目标账号的版本；这里把源账号 per-user 的
    ``workspace-state.json`` 并集回根级，让目标账号也能看到源账号的工作区列表。
    """
    source_uid = source_uid.strip()
    target_uid = target_uid.strip()
    if source_uid == target_uid:
        raise MigrationError("源账号与目标账号相同")

    result = WorkspaceMergeResult()
    backup_dir = _backup_root("workspaces")
    for item in _workspace_items(source_uid):
        try:
            if _apply_item(item, backup_dir):
                result.merged_files.append(item.where)
                if result.backup is None:
                    result.backup = str(backup_dir)
        except (MigrationError, OSError) as exc:
            result.errors.append({"where": item.where, "error": str(exc)})
    return result
