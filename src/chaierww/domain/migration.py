"""账号数据迁移：把源账号的记忆 / 连接器合并进目标账号（带去重）。

对照旧项目 Rust 实现（``modules/memory.rs`` / ``modules/connector.rs`` / ``migrate.rs``）
的**协议**，按规格优先重写。只覆盖 WorkBuddy（Trae 客户端无对应本地数据）。

五块数据（都以号池账号的 ``external_id`` = WorkBuddy 用户 UUID 为 key）：

1. **记忆**：``~/.workbuddy/memory/{uid}_memory.md`` —— 追加合并，规范化行去重；
2. **连接器**：``~/.workbuddy/connectors/{uid}/{mcp.json,connector-states.json}`` —— 递归深合并；
3. **会话**：见 ``domain/session_migration.py``（改 workbuddy.db，单独模块）；
4. **个人档案与偏好**：见 ``domain/profile_migration.py``（``user-{uid}-personal/`` 等）；
5. **工作区与项目元数据**：见 ``domain/profile_migration.py``（并集进根级工作区元数据）。

## 设计约束（对齐旧项目）

- 每个范围**独立成败**：一个失败不阻断另一个，报告各自给 ``error``。
- **幂等**：二次执行不产生新增内容（memory 追加 0 行、connector ``changed=false``）。
- 源账号与目标账号相同 → 拒绝，避免自我覆盖。
- 改写目标前先备份原文（备份失败不阻断迁移，报告里 ``backup=null``）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from chaierww.domain.sessions import workbuddy_data_dir

#: 单次 memory 合并追加的段落上限（防一次性导入超大文件撑爆目标）。
MEMORY_MERGE_MAX_APPENDED = 2000

#: 需要合并的连接器配置文件。
CONNECTOR_FILES = ("mcp.json", "connector-states.json")


# ---------- 路径 ----------


def memory_dir() -> Path:
    return workbuddy_data_dir() / "memory"


def memory_file_for(uid: str) -> Path:
    return memory_dir() / f"{uid}_memory.md"


def connectors_dir() -> Path:
    return workbuddy_data_dir() / "connectors"


def connector_account_dir_for(uid: str) -> Path:
    return connectors_dir() / uid


# ---------- 记忆：规范化与合并 ----------


def normalize_memory_line(line: str) -> str | None:
    """规范化一行记忆文本作为去重指纹。返回 ``None`` 表示不参与去重判定。

    规则（对齐旧项目）：
    1. 空行 / 纯分隔线（``---`` / ``***`` / ``___``）→ ``None``；
    2. 折叠行内空白为单个空格；
    3. 剥标题 ``#`` 前缀（``## 偏好`` 与 ``### 偏好`` 同义）；
    4. 剥列表符号（``-`` / ``*`` / ``+`` / ``1.`` / ``2)``）。

    幂等：``normalize(normalize(x)) == normalize(x)``。
    """
    trimmed = line.strip()
    if not trimmed or _is_separator_line(trimmed):
        return None

    text = _collapse_whitespace(trimmed)
    if not text:
        return None

    # 剥标题前缀（`#` 须后跟空白或行尾，避免误伤 `#tag`）。
    while True:
        rest = _strip_leading_hashes(text)
        if rest is None:
            break
        text = _collapse_whitespace(rest)
        if not text or _is_separator_line(text):
            return None

    # 剥列表符号。
    while True:
        rest = _strip_leading_bullet(text)
        if rest is None:
            break
        text = _collapse_whitespace(rest)
        if not text or _is_separator_line(text):
            return None

    if not text or _is_separator_line(text):
        return None
    return text


def _collapse_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _strip_leading_hashes(text: str) -> str | None:
    if not text.startswith("#"):
        return None
    rest = text.lstrip("#")
    if rest == "":
        return ""
    # `#tag`（无空白）不视为标题。
    if rest.startswith(" "):
        return rest
    return None


def _strip_leading_bullet(text: str) -> str | None:
    first = text[0]
    if first in "-*+":
        rest = text[1:]
        return rest if rest.startswith(" ") else None
    match = re.match(r"^(\d+)([.)])\s", text)
    if match:
        return text[match.end():]
    return None


def _is_separator_line(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    if not compact:
        return False
    marker = compact[0]
    if marker not in "-*_":
        return False
    return len(compact) >= 3 and all(c == marker for c in compact)


@dataclass
class MemoryMergeResult:
    target_lines: int = 0
    source_lines: int = 0
    appended: int = 0
    skipped_duplicate: int = 0
    backup: str | None = None

    @property
    def changed(self) -> bool:
        return self.appended > 0


def merge_memory_text(target: str, source: str, source_label: str) -> tuple[str, MemoryMergeResult]:
    """纯函数：把源记忆合并进目标记忆，返回 (合并后文本, 计数)。"""
    target_lines = [ln for ln in target.splitlines() if normalize_memory_line(ln) is not None]
    source_lines = [ln for ln in source.splitlines() if normalize_memory_line(ln) is not None]

    result = MemoryMergeResult(
        target_lines=len(target_lines),
        source_lines=len(source_lines),
    )

    # 目标为空 → 直接采用源内容（保留原始缩进与格式）。
    if not target_lines:
        trimmed = source.strip()
        if not trimmed:
            return target, result
        result.appended = min(len(source_lines), MEMORY_MERGE_MAX_APPENDED)
        return trimmed, result

    seen = {normalize_memory_line(ln) for ln in target_lines}
    accepted: list[str] = []
    for raw in source.splitlines():
        fp = normalize_memory_line(raw)
        if fp is None:
            continue
        if fp in seen:
            result.skipped_duplicate += 1
            continue
        seen.add(fp)
        if len(accepted) >= MEMORY_MERGE_MAX_APPENDED:
            result.skipped_duplicate += 1
            continue
        accepted.append(raw.rstrip())

    if not accepted:
        return target, result

    result.appended = len(accepted)
    label = source_label.strip()
    out = target.rstrip() + "\n\n---\n"
    if label:
        out += f"## 迁移自 {label}\n\n"
    out += "\n".join(accepted) + "\n"
    return out, result


def merge_memory_files(source_uid: str, target_uid: str) -> MemoryMergeResult:
    """合并两个账号的记忆文件：读源 → 合并到目标 → 原子写回。源不存在/空 → 不改写。"""
    source_uid = source_uid.strip()
    target_uid = target_uid.strip()
    if source_uid == target_uid:
        raise MigrationError("源账号与目标账号相同")

    source_path = memory_file_for(source_uid)
    if not source_path.is_file():
        return MemoryMergeResult()
    source = source_path.read_text(encoding="utf-8", errors="replace")
    if not source.strip():
        return MemoryMergeResult()

    target_path = memory_file_for(target_uid)
    target = (
        target_path.read_text(encoding="utf-8", errors="replace")
        if target_path.is_file()
        else ""
    )

    merged, result = merge_memory_text(target, source, _shorten_uid(source_uid))
    if result.changed:
        if target_path.is_file():
            result.backup = _backup_file(target_path, target)
        _atomic_write(target_path, merged)
    return result


def _shorten_uid(uid: str) -> str:
    trimmed = uid.strip()
    return trimmed if len(trimmed) <= 12 else f"{trimmed[:12]}..."


# ---------- 连接器：JSON 深合并 ----------


@dataclass
class FileMergeResult:
    added_keys: int = 0
    kept_target_keys: int = 0
    dropped_duplicate_elements: int = 0
    changed: bool = False

    def absorb(self, other: FileMergeResult) -> None:
        self.added_keys += other.added_keys
        self.kept_target_keys += other.kept_target_keys
        self.dropped_duplicate_elements += other.dropped_duplicate_elements
        self.changed = self.changed or other.changed


@dataclass
class ConnectorMergeResult:
    files: list[tuple[str, FileMergeResult]] = field(default_factory=list)
    skipped: bool = False
    backup: str | None = None

    @property
    def added_keys(self) -> int:
        return sum(r.added_keys for _, r in self.files)

    @property
    def dropped_duplicate_elements(self) -> int:
        return sum(r.dropped_duplicate_elements for _, r in self.files)

    @property
    def changed(self) -> bool:
        return any(r.changed for _, r in self.files)


def _normalize_json(value: Any) -> Any:
    """递归规范化 JSON：对象键排序，其余原样。用于稳定比较与指纹。"""
    if isinstance(value, dict):
        return {k: _normalize_json(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_normalize_json(item) for item in value]
    return value


def _element_fingerprint(value: Any) -> str:
    return json.dumps(_normalize_json(value), ensure_ascii=False, sort_keys=True)


def _merge_arrays(target: list, source: list) -> tuple[list, FileMergeResult]:
    result = FileMergeResult()
    seen: set[str] = set()
    merged: list = []
    for item in target:
        fp = _element_fingerprint(item)
        if fp not in seen:
            seen.add(fp)
            merged.append(item)
        else:
            result.dropped_duplicate_elements += 1
            result.changed = True
    for item in source:
        fp = _element_fingerprint(item)
        if fp not in seen:
            seen.add(fp)
            merged.append(item)
            result.added_keys += 1
            result.changed = True
        else:
            result.dropped_duplicate_elements += 1
    return merged, result


def _count_keys(value: Any) -> int:
    if isinstance(value, dict):
        return sum(1 + _count_keys(v) for v in value.values())
    if isinstance(value, list):
        return sum(1 + _count_keys(v) for v in value)
    return 0


def merge_json(target: Any, source: Any) -> tuple[Any, FileMergeResult]:
    """把源 JSON 合并进目标 JSON。冲突策略：标量冲突保留目标，对象递归，数组元素级去重并集。"""
    if isinstance(target, dict) and isinstance(source, dict):
        out = dict(target)
        result = FileMergeResult()
        for key, src_value in source.items():
            if key not in out:
                out[key] = src_value
                result.added_keys += 1 + _count_keys(src_value)
                result.changed = True
            else:
                dst_value = out[key]
                if isinstance(dst_value, dict) and isinstance(src_value, dict):
                    merged, sub = merge_json(dst_value, src_value)
                    out[key] = merged
                    result.absorb(sub)
                elif isinstance(dst_value, list) and isinstance(src_value, list):
                    merged, sub = _merge_arrays(dst_value, src_value)
                    out[key] = merged
                    result.absorb(sub)
                else:
                    result.kept_target_keys += 1
        return out, result
    # 目标无对象结构 → 采用源值（等价首次复制）。
    added = _count_keys(source)
    return source, FileMergeResult(added_keys=added, changed=added > 0)


def merge_json_text(target_text: str, source_text: str) -> tuple[str, FileMergeResult]:
    """合并单个 JSON 配置文件。目标空/非法 → 直接采用源内容。"""
    try:
        source = json.loads(source_text) if source_text.strip() else None
    except json.JSONDecodeError as exc:
        raise MigrationError(f"源配置不是有效 JSON：{exc}") from exc
    if source is None:
        return target_text, FileMergeResult()

    try:
        target = json.loads(target_text) if target_text.strip() else None
    except json.JSONDecodeError:
        target = None

    if target is None:
        return source_text, FileMergeResult(added_keys=_count_keys(source), changed=True)

    merged, result = merge_json(target, source)
    if not result.changed:
        return target_text, result
    return json.dumps(merged, ensure_ascii=False, indent=2), result


def merge_connector_files(source_uid: str, target_uid: str) -> ConnectorMergeResult:
    """合并两个账号的连接器配置目录（mcp.json / connector-states.json）。"""
    source_uid = source_uid.strip()
    target_uid = target_uid.strip()
    if source_uid == target_uid:
        raise MigrationError("源账号与目标账号相同")

    source_dir = connector_account_dir_for(source_uid)
    if not source_dir.is_dir():
        return ConnectorMergeResult(skipped=True)

    result = ConnectorMergeResult()
    target_dir = connector_account_dir_for(target_uid)
    backup_dir: Path | None = None

    for name in CONNECTOR_FILES:
        source_path = source_dir / name
        if not source_path.is_file():
            continue
        source_text = source_path.read_text(encoding="utf-8", errors="replace")
        target_path = target_dir / name
        target_text = (
            target_path.read_text(encoding="utf-8", errors="replace")
            if target_path.is_file()
            else ""
        )

        merged_text, file_result = merge_json_text(target_text, source_text)
        result.files.append((name, file_result))
        if file_result.changed:
            if backup_dir is None:
                backup_dir = _backup_dir(target_dir)
            _atomic_write(target_path, merged_text)

    if backup_dir is not None:
        result.backup = str(backup_dir)
    return result


# ---------- 通用 ----------


class MigrationError(RuntimeError):
    """迁移层可预期失败，消息面向用户。"""


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".migrate.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _backup_file(path: Path, original: str) -> str | None:
    """备份目标文件改前原文到 backups/memory/{时间戳}/。失败返回 None（不阻断）。"""
    try:
        dir_path = _backup_dir(path.parent) / path.name
        dir_path.parent.mkdir(parents=True, exist_ok=True)
        dir_path.write_text(original, encoding="utf-8")
        return str(dir_path)
    except OSError:
        return None


def _backup_dir(target_dir: Path) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return workbuddy_data_dir() / "backups" / "connectors" / stamp


def _sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def preview_memory(source_uid: str, target_uid: str) -> dict[str, Any]:
    """只读预检：记忆将追加多少行、跳过多少重复行。不写任何文件。"""
    zeros = {
        "targetLines": 0,
        "sourceLines": 0,
        "wouldAppend": 0,
        "wouldSkip": 0,
        "changed": False,
    }
    source_uid = source_uid.strip()
    target_uid = target_uid.strip()
    if not source_uid or not target_uid or source_uid == target_uid:
        return zeros
    source_path = memory_file_for(source_uid)
    if not source_path.is_file():
        return zeros
    source = source_path.read_text(encoding="utf-8", errors="replace")
    if not source.strip():
        return zeros
    target_path = memory_file_for(target_uid)
    target = (
        target_path.read_text(encoding="utf-8", errors="replace")
        if target_path.is_file()
        else ""
    )
    _, result = merge_memory_text(target, source, _shorten_uid(source_uid))
    return {
        "targetLines": result.target_lines,
        "sourceLines": result.source_lines,
        "wouldAppend": result.appended,
        "wouldSkip": result.skipped_duplicate,
        "changed": result.changed,
    }


def preview_connectors(source_uid: str, target_uid: str) -> dict[str, Any]:
    """只读预检：连接器将新增多少键、丢弃多少重复元素。不写任何文件。"""
    source_uid = source_uid.strip()
    target_uid = target_uid.strip()
    if not source_uid or not target_uid or source_uid == target_uid:
        return {"skipped": True, "files": [], "addedKeys": 0, "droppedDupe": 0}
    source_dir = connector_account_dir_for(source_uid)
    if not source_dir.is_dir():
        return {"skipped": True, "files": [], "addedKeys": 0, "droppedDupe": 0}
    target_dir = connector_account_dir_for(target_uid)
    files: list[dict[str, Any]] = []
    added_total = 0
    dropped_total = 0
    for name in CONNECTOR_FILES:
        source_path = source_dir / name
        if not source_path.is_file():
            continue
        target_path = target_dir / name
        target_text = (
            target_path.read_text(encoding="utf-8", errors="replace")
            if target_path.is_file()
            else ""
        )
        try:
            _, file_result = merge_json_text(
                target_text, source_path.read_text(encoding="utf-8", errors="replace")
            )
        except MigrationError as exc:
            files.append({"name": name, "error": str(exc)})
            continue
        added_total += file_result.added_keys
        dropped_total += file_result.dropped_duplicate_elements
        files.append(
            {
                "name": name,
                "addedKeys": file_result.added_keys,
                "droppedDuplicateElements": file_result.dropped_duplicate_elements,
                "changed": file_result.changed,
            }
        )
    return {
        "skipped": False,
        "files": files,
        "addedKeys": added_total,
        "droppedDupe": dropped_total,
    }


# ---------- 编排：五块数据合一 ----------


@dataclass
class MigrationReport:
    """一次「源账号 → 目标账号」迁移的完整报告。各块独立成败。"""

    source_uid: str
    target_uid: str
    memory: dict[str, Any] | None = None
    connectors: dict[str, Any] | None = None
    sessions: dict[str, Any] | None = None
    profile: dict[str, Any] | None = None
    workspaces: dict[str, Any] | None = None

    @property
    def changed(self) -> bool:
        mem_changed = bool(self.memory and self.memory.get("appended", 0) > 0)
        conn_changed = bool(self.connectors and self.connectors.get("changed"))
        sess_changed = bool(self.sessions and self.sessions.get("copiedCount", 0) > 0)
        profile_changed = bool(self.profile and self.profile.get("changed"))
        work_changed = bool(self.workspaces and self.workspaces.get("changed"))
        return mem_changed or conn_changed or sess_changed or profile_changed or work_changed

    def payload(self) -> dict[str, Any]:
        return {
            "sourceUid": self.source_uid,
            "targetUid": self.target_uid,
            "memory": self.memory,
            "connectors": self.connectors,
            "sessions": self.sessions,
            "profile": self.profile,
            "workspaces": self.workspaces,
            "changed": self.changed,
        }


@dataclass(frozen=True, slots=True)
class MigrationSelection:
    """切换时要迁移哪些数据块。

    默认全开；用户在「设置 → 常规」里可以关掉任意一块（例如只想换登录态、
    不想把会话复制过去）。全部关闭时调用方会直接跳过迁移，不产生迁移报告。
    """

    memory: bool = True
    connectors: bool = True
    sessions: bool = True
    profile: bool = True
    workspaces: bool = True

    @property
    def any(self) -> bool:
        return (
            self.memory
            or self.connectors
            or self.sessions
            or self.profile
            or self.workspaces
        )


def migrate_account_data(
    source_uid: str,
    target_uid: str,
    *,
    session_ids: list[str] | None = None,
    selection: MigrationSelection | None = None,
) -> MigrationReport:
    """把源账号的记忆 / 连接器 / 会话迁移到目标账号（三块独立成败，均幂等）。

    - memory / connectors 是纯文件操作，失败不阻断其它块；
    - sessions 涉及改 workbuddy.db，见 ``session_migration``；失败也不阻断前两块。
    - 源与目标相同 → 整体拒绝。
    - ``selection`` 决定迁移哪几块（设置 → 常规 的开关），未选中的块不出现在报告里。
    """
    source_uid = source_uid.strip()
    target_uid = target_uid.strip()
    if source_uid == target_uid:
        raise MigrationError("源账号与目标账号相同")

    chosen = selection or MigrationSelection()
    report = MigrationReport(source_uid=source_uid, target_uid=target_uid)

    # 记忆
    if chosen.memory:
        try:
            mem = merge_memory_files(source_uid, target_uid)
            report.memory = {
                "targetLines": mem.target_lines,
                "sourceLines": mem.source_lines,
                "appended": mem.appended,
                "skippedDuplicate": mem.skipped_duplicate,
                "changed": mem.changed,
                "backup": mem.backup,
            }
        except MigrationError as exc:
            report.memory = {"error": str(exc)}

    # 连接器
    if chosen.connectors:
        try:
            conn = merge_connector_files(source_uid, target_uid)
            report.connectors = {
                "skipped": conn.skipped,
                "addedKeys": conn.added_keys,
                "droppedDuplicateElements": conn.dropped_duplicate_elements,
                "changed": conn.changed,
                "backup": conn.backup,
            }
        except MigrationError as exc:
            report.connectors = {"error": str(exc)}

    # 会话
    if chosen.sessions:
        try:
            from chaierww.domain.session_migration import copy_sessions_for_switch

            sess = copy_sessions_for_switch(
                source_uid, target_uid, session_ids or []
            )
            report.sessions = sess.payload()
        except Exception as exc:  # noqa: BLE001 - 会话迁移失败不阻断其它块
            report.sessions = {"error": f"{type(exc).__name__}: {exc}"}

    # 个人档案与偏好
    if chosen.profile:
        try:
            from chaierww.domain.profile_migration import merge_profile_files

            prof = merge_profile_files(source_uid, target_uid)
            report.profile = prof.payload()
        except MigrationError as exc:
            report.profile = {"error": str(exc)}

    # 工作区与项目元数据
    if chosen.workspaces:
        try:
            from chaierww.domain.profile_migration import merge_workspace_metadata

            work = merge_workspace_metadata(source_uid, target_uid)
            report.workspaces = work.payload()
        except MigrationError as exc:
            report.workspaces = {"error": str(exc)}

    return report


def preview_account_data(
    source_uid: str,
    target_uid: str,
    *,
    session_ids: list[str] | None = None,
    selection: MigrationSelection | None = None,
) -> dict[str, Any]:
    """迁移前预检：只读地推演本次迁移会新增 / 跳过什么，**不写任何文件 / 库 / 台账**。

    与 ``migrate_account_data`` 共用同一套判定（``selection`` 选中的块才预检），
    返回各块的 ``wouldCopy`` / ``willMerge`` 明细，供切换确认弹窗展示。
    """
    source_uid = source_uid.strip()
    target_uid = target_uid.strip()
    chosen = selection or MigrationSelection()
    out: dict[str, Any] = {
        "sourceUid": source_uid,
        "targetUid": target_uid,
        "valid": True,
        "memory": None,
        "connectors": None,
        "sessions": None,
        "profile": None,
        "workspaces": None,
        "duplicates": [],
    }
    if source_uid == target_uid:
        out["valid"] = False
        out["reason"] = "源账号与目标账号相同"
        return out

    if chosen.memory:
        out["memory"] = preview_memory(source_uid, target_uid)

    if chosen.connectors:
        out["connectors"] = preview_connectors(source_uid, target_uid)

    if chosen.sessions:
        try:
            from chaierww.domain.session_migration import preview_sessions_for_switch

            sess = preview_sessions_for_switch(source_uid, target_uid, session_ids)
            out["sessions"] = {
                "wouldCopy": sess["wouldCopy"],
                "wouldSkip": sess["wouldSkip"],
                "copyCount": len(sess["wouldCopy"]),
                "skipCount": len(sess["wouldSkip"]),
            }
            out["duplicates"] = sess["duplicates"]
        except Exception as exc:  # noqa: BLE001 - 预检失败不阻断其它块
            out["sessions"] = {"error": f"{type(exc).__name__}: {exc}"}

    if chosen.profile:
        try:
            from chaierww.domain.profile_migration import preview_profile

            prof = preview_profile(source_uid, target_uid)
            out["profile"] = {**prof, "mergeCount": len(prof["willMerge"])}
        except MigrationError as exc:
            out["profile"] = {"error": str(exc)}

    if chosen.workspaces:
        try:
            from chaierww.domain.profile_migration import preview_workspaces

            work = preview_workspaces(source_uid)
            out["workspaces"] = {**work, "mergeCount": len(work["willMerge"])}
        except MigrationError as exc:
            out["workspaces"] = {"error": str(exc)}

    return out
