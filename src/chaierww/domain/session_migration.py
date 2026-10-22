"""会话迁移：把源账号的会话完整复制到目标账号名下（路径 B：生成新 id）。

对照旧项目 Rust 实现（``modules/session.rs`` 的 ``run_copy_batch`` 三段管线）的**协议**，
按规格优先重写。只覆盖 WorkBuddy（Trae 客户端无对应会话数据）。

## 机制（三段管线，精简版）

1. **去重（血缘）**：读复制账本 ``buddy-switch-copy-ledger.json``，沿血统回溯到**根会话**，
   只要目标账号已收到过同一条血统（含往返链路上复制出来的副本）就跳过——A→B→A 往返
   不会再让副本翻倍；批内同一根只排一次。
2. **读源行**：``SELECT * FROM sessions WHERE id=? AND user_id=?``，顺带做 Claw 工作区判定
   （账号绑定的 IM 渠道工作区，复制过去目标账号也用不了，拒绝）。
3. **复制正文**：``projects/{workspace}/{old_cid}.jsonl`` → 同目录 ``{new_cid}.jsonl``，
   正文里 replace(旧 cid → 新 cid)。
3.5 **伴生文件**：``{cid}.meta.json``（会话元数据，改名并替换 cid）与
   ``{cid}.file-rollback.ndjson``（回滚流水，仅改名不改内容）一并复制，避免复制过去的
   会话缺元数据、显示成旧样子。
4. **写索引行**：``INSERT OR REPLACE INTO sessions``（动态列；覆盖 id=新 cid、
   user_id=目标 uid、created_at/updated_at=now、deleted_at=NULL）。
5. **注册云端映射**：``INSERT OR REPLACE INTO edge_sync_mapping
   (session_id, conversation_id, msg_channel, created_at)``
   = (新 cid, 新 cid, ``convmsg:{target_uid}``, now 秒)。
6. **台账落盘**：``source_uid \x1f target_uid \x1f source_cid -> new_cid``，并记录血统
   ``new_cid -> (root_uid, root_cid)``。

## 重复清理

``find_duplicate_copies`` / ``cleanup_duplicates``：识别同一账号下同一血统的多条会话，
保留最早一条，其余软删除（``deleted_at``）并把 jsonl 及伴生文件移入备份目录。

## 安全纪律（对齐旧项目）

- **改 db 前整库备份一次**（SQLite 在线备份 API）；备份失败不阻断复制，但报告里显式标
  ``backupError``（绝不谎报有备份点）。
- 正文复制 / 索引行 / 云端映射各自软失败：jsonl 成功但索引行失败 → 至少正文落到目标账号，
  客户端重启建索引即可见；绝不因一个会话失败拖垮整批。
- Claw 工作区 → 显式拒绝，给原因。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from chaierww.domain.sessions import (
    build_history_index,
    is_claw_workspace,
    projects_dir,
    workbuddy_data_dir,
    workbuddy_db_path,
)

#: 复制账本文件名（键 ``source_uid \x1f target_uid \x1f source_cid -> new_cid``）。
COPY_LEDGER_FILE = "buddy-switch-copy-ledger.json"
LEDGER_SEP = "\x1f"

#: 血缘回溯最大深度（防环）。
MAX_LINEAGE_DEPTH = 8


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def _now_secs() -> int:
    return int(datetime.now().timestamp())


def _edge_sync_db_path() -> Path | None:
    """扫数据目录取 ``edge-sync-mapping-v{N}.db`` 里最大的 N；没有返回 None。"""
    data_dir = workbuddy_data_dir()
    if not data_dir.is_dir():
        return None
    best: tuple[int, Path] | None = None
    for path in data_dir.glob("edge-sync-mapping-v*.db"):
        stem = path.name
        if not stem.startswith("edge-sync-mapping-v") or not stem.endswith(".db"):
            continue
        try:
            num = int(stem[len("edge-sync-mapping-v"):-len(".db")])
        except ValueError:
            continue
        if best is None or num > best[0]:
            best = (num, path)
    return best[1] if best else None


def _ledger_path() -> Path:
    return workbuddy_data_dir() / COPY_LEDGER_FILE


@dataclass
class LedgerDoc:
    """复制台账：``entries``（三元组→新 id）与 ``lineage``（新 id→(根uid,根id)）。

    顶层是版本化结构 ``{"version":2,"ledger":{…},"lineage":{…}}``；读取时兼容旧格式
    （顶层直接是扁平 entries）——旧台账继续有效，血缘靠 ``entries`` 反推。
    """

    entries: dict[str, str] = field(default_factory=dict)
    lineage: dict[str, tuple[str, str]] = field(default_factory=dict)

    def reverse(self) -> dict[str, tuple[str, str]]:
        """新 id → (源 uid, 源会话 id)，用于沿血统回溯。"""
        out: dict[str, tuple[str, str]] = {}
        for key, new_cid in self.entries.items():
            parts = key.split(LEDGER_SEP)
            if len(parts) == 3:
                out[new_cid] = (parts[0], parts[2])
        return out

    def received_keys(self) -> set[tuple[str, str, str]]:
        """目标账号已收到的血统键 ``(target_uid, root_uid, root_cid)``。"""
        reverse = self.reverse()
        out: set[tuple[str, str, str]] = set()
        for key in self.entries:
            parts = key.split(LEDGER_SEP)
            if len(parts) != 3:
                continue
            src_uid, target_uid, src_cid = parts
            root_uid, root_cid = _root_of(src_cid, src_uid, reverse, self.lineage)
            out.add((target_uid, root_uid, root_cid))
        return out


def _read_ledger() -> LedgerDoc:
    path = _ledger_path()
    if not path.is_file():
        return LedgerDoc()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return LedgerDoc()
    if not isinstance(data, dict):
        return LedgerDoc()
    if "ledger" in data or "lineage" in data:
        raw_entries = data.get("ledger")
        raw_lineage = data.get("lineage")
        entries = (
            {str(k): str(v) for k, v in raw_entries.items()}
            if isinstance(raw_entries, dict)
            else {}
        )
        lineage: dict[str, tuple[str, str]] = {}
        if isinstance(raw_lineage, dict):
            for key, value in raw_lineage.items():
                if isinstance(value, (list, tuple)) and len(value) == 2:
                    lineage[str(key)] = (str(value[0]), str(value[1]))
        return LedgerDoc(entries=entries, lineage=lineage)
    # 旧格式：顶层就是扁平 entries（无血缘，靠 reverse 反推）。
    return LedgerDoc(entries={str(k): str(v) for k, v in data.items()})


def _write_ledger(doc: LedgerDoc) -> None:
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 2,
        "ledger": doc.entries,
        "lineage": {key: [root[0], root[1]] for key, root in doc.lineage.items()},
    }
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _ledger_key(source_uid: str, target_uid: str, source_cid: str) -> str:
    return f"{source_uid}{LEDGER_SEP}{target_uid}{LEDGER_SEP}{source_cid}"


def _root_of(
    cid: str,
    uid: str,
    reverse: dict[str, tuple[str, str]],
    lineage: dict[str, tuple[str, str]],
    depth: int = 0,
) -> tuple[str, str]:
    """沿血统回溯：``cid`` 若是副本，返回其根 ``(root_uid, root_cid)``；否则返回自身。"""
    if cid in lineage:
        return lineage[cid]
    if depth >= MAX_LINEAGE_DEPTH or cid not in reverse:
        return uid, cid
    src_uid, src_cid = reverse[cid]
    return _root_of(src_cid, src_uid, reverse, lineage, depth + 1)


def _dedupe_of(
    cid: str,
    source_uid: str,
    target_uid: str,
    *,
    entries: dict[str, str],
    reverse: dict[str, tuple[str, str]],
    lineage: dict[str, tuple[str, str]],
    received: set[tuple[str, str, str]],
    planned_roots: set[tuple[str, str]],
) -> tuple[str | None, tuple[str, str]]:
    """去重判定：返回 ``(跳过理由 或 None, (root_uid, root_cid))``。

    复制与预检共用这一条判定，避免两处逻辑漂移。
    """
    if _ledger_key(source_uid, target_uid, cid) in entries:
        return "已复制过", (source_uid, cid)
    root_uid, root_cid = _root_of(cid, source_uid, reverse, lineage)
    if root_uid == target_uid:
        return "本就是目标账号的会话", (root_uid, root_cid)
    if (target_uid, root_uid, root_cid) in received:
        return "目标账号已有该会话（含副本血统）", (root_uid, root_cid)
    if (root_uid, root_cid) in planned_roots:
        return "批内同一血统", (root_uid, root_cid)
    return None, (root_uid, root_cid)


@dataclass
class CopiedSession:
    id: str
    new_id: str
    title: str
    jsonl_copied: bool
    warning: str | None = None


@dataclass
class SessionCopyReport:
    copied: list[dict[str, Any]] = field(default_factory=list)
    skipped: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    backup: str | None = None
    backup_error: str | None = None

    @property
    def total(self) -> int:
        return len(self.copied) + len(self.skipped) + len(self.errors)

    def payload(self) -> dict[str, Any]:
        return {
            "copied": self.copied,
            "skipped": self.skipped,
            "errors": self.errors,
            "backup": self.backup,
            "backupError": self.backup_error,
            "copiedCount": len(self.copied),
            "skippedCount": len(self.skipped),
            "errorCount": len(self.errors),
        }


def list_sessions_for_user(uid: str) -> list[dict[str, Any]]:
    """列出某账号未删除的会话（id + 标题），供复制时选择与展示。"""
    db = workbuddy_db_path()
    if not db.is_file():
        return []
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return []
    try:
        cols = _table_columns(conn, "sessions")
        if not cols:
            return []
        has_custom = "custom_title" in cols
        custom = "custom_title" if has_custom else "NULL"
        sql = (
            f"SELECT id, title, {custom}, cwd FROM sessions "
            "WHERE user_id = ? AND deleted_at IS NULL ORDER BY updated_at DESC"
        )
        rows = conn.execute(sql, (uid,)).fetchall()
        out: list[dict[str, Any]] = []
        for cid, title, custom_title, cwd in rows:
            out.append(
                {
                    "id": cid,
                    "title": (custom_title or title or "(无标题)").strip(),
                    "cwd": cwd or "",
                }
            )
        return out
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def copy_sessions_for_switch(
    source_uid: str, target_uid: str, session_ids: list[str]
) -> SessionCopyReport:
    """把 source_uid 的指定会话复制到 target_uid 名下。返回报告。

    source_uid 与 target_uid 相同 → 拒绝。空 session_ids → 复制源账号全部会话。
    """
    source_uid = source_uid.strip()
    target_uid = target_uid.strip()
    if not source_uid or not target_uid:
        raise SessionMigrationError("源账号或目标账号为空")
    if source_uid == target_uid:
        raise SessionMigrationError("源账号与目标账号相同")

    report = SessionCopyReport()
    doc = _read_ledger()
    entries = doc.entries
    lineage = doc.lineage
    reverse = doc.reverse()
    received = doc.received_keys()

    db = workbuddy_db_path()
    src_conn = _open_db(db)
    if src_conn is None:
        report.errors.append({"id": "", "error": "会话数据库不可读，无法复制会话"})
        return report

    # 目标会话列表：未指定则取全部。
    ids = session_ids if session_ids else [s["id"] for s in list_sessions_for_user(source_uid)]
    if not ids:
        return report

    build_history_index(projects_dir())
    planned: dict[str, str] = {}  # source_cid -> new_cid（批内去重）
    planned_roots: dict[tuple[str, str], str] = {}  # 根血统 -> new_cid（批内同根去重）
    plans: list[dict[str, Any]] = []

    # ---- phase 0：去重 + Claw + 读行 + 定路径 ----
    for cid in ids:
        if cid in planned:
            report.skipped.append({"id": cid, "reason": "批内重复", "newId": planned[cid]})
            continue
        reason, (root_uid, root_cid) = _dedupe_of(
            cid,
            source_uid,
            target_uid,
            entries=entries,
            reverse=reverse,
            lineage=lineage,
            received=received,
            planned_roots=set(planned_roots),
        )
        if reason is not None:
            skip: dict[str, Any] = {"id": cid, "reason": reason}
            if reason == "已复制过":
                skip["newId"] = entries[_ledger_key(source_uid, target_uid, cid)]
            elif reason == "批内同一血统":
                skip["newId"] = planned_roots[(root_uid, root_cid)]
            report.skipped.append(skip)
            continue
        row = _read_session_row(src_conn, cid, source_uid)
        if row is None:
            # 源行缺失：降级从 jsonl 推断 cwd 做 Claw 检查
            cwd = _find_jsonl_cwd(cid)
            if cwd and is_claw_workspace(cwd):
                report.errors.append(
                    {"id": cid, "error": "Claw 工作区绑定当前账号渠道，不支持复制"}
                )
                continue
            new_cid = str(uuid.uuid4())
            title = "(无标题)"
            jsonl_src = _find_jsonl(cid)
        else:
            cols, vals = row
            cwd = _col_value(cols, vals, "cwd")
            if cwd and is_claw_workspace(str(cwd)):
                report.errors.append(
                    {"id": cid, "error": "Claw 工作区绑定当前账号渠道，不支持复制"}
                )
                continue
            new_cid = str(uuid.uuid4())
            title = _row_title(cols, vals)
            jsonl_src = _find_jsonl(cid)

        planned[cid] = new_cid
        planned_roots[(root_uid, root_cid)] = new_cid
        plans.append(
            {
                "cid": cid,
                "new_cid": new_cid,
                "title": title,
                "row": row,
                "root_uid": root_uid,
                "root_cid": root_cid,
                "jsonl_src": jsonl_src,
                "jsonl_dst": _target_jsonl_path(jsonl_src, new_cid) if jsonl_src else None,
            }
        )

    if not plans:
        _finalize_ledger(doc)
        return report

    # 改库前整库备份一次。
    backup = _backup_db(db)
    if isinstance(backup, str):
        report.backup = backup
    else:
        report.backup_error = backup

    dst_conn = src_conn  # 同一库（本机单账号数据目录，源库即目标库）
    dst_cols = _table_columns(dst_conn, "sessions")
    edge_path = _edge_sync_db_path()
    edge_conn = _open_db(edge_path) if edge_path else None

    # ---- phase A + B：复制正文 + 写索引行 + 云端映射 + 台账 ----
    for plan in plans:
        cid = plan["cid"]
        new_cid = plan["new_cid"]

        # 复制 jsonl 正文 + 伴生文件（.meta.json / .file-rollback.ndjson）
        jsonl_ok = False
        companions: dict[str, bool] = {"meta": False, "rollback": False}
        if plan["jsonl_src"] and plan["jsonl_dst"]:
            jsonl_ok = _copy_jsonl(plan["jsonl_src"], plan["jsonl_dst"], cid, new_cid)
            if jsonl_ok:
                companions = _copy_companions(plan["jsonl_src"], cid, new_cid)

        # 写索引行（源行存在时）
        session_row_written = False
        if plan["row"] is not None and dst_conn is not None and dst_cols:
            cols, vals = plan["row"]
            session_row_written = _insert_session_row(
                dst_conn, dst_cols, cols, vals, new_cid, target_uid
            )

        # 注册云端映射
        mapping_written = False
        if edge_conn is not None:
            mapping_written = _insert_edge_sync_mapping(edge_conn, new_cid, target_uid)

        # 台账：记录三元组 + 血统（供往返去重）
        entries[_ledger_key(source_uid, target_uid, cid)] = new_cid
        lineage[new_cid] = (plan["root_uid"], plan["root_cid"])

        warning: str | None = None
        if report.backup_error:
            warning = (
                f"复制前备份失败（{report.backup_error}）；本次复制没有可回滚的备份点，请手动备份后重试"
            )
        elif jsonl_ok and not session_row_written:
            warning = "聊天记录已复制，但会话索引未写入（数据库可能被占用）；重启客户端后可重建索引"
        elif session_row_written and not mapping_written:
            warning = "会话已复制，但云端同步映射未注册（重启客户端后可能自动补齐）"

        if jsonl_ok or session_row_written:
            report.copied.append(
                {
                    "id": cid,
                    "newId": new_cid,
                    "title": plan["title"],
                    "companions": companions,
                    "warning": warning,
                }
            )
        else:
            report.errors.append({"id": cid, "error": warning or "复制失败（无正文也无索引）"})

    _finalize_ledger(doc)
    _close(src_conn, dst_conn, edge_conn)
    return report


def preview_sessions_for_switch(
    source_uid: str, target_uid: str, session_ids: list[str] | None = None
) -> dict[str, Any]:
    """预检：只读地推演一次会话复制，**不写任何文件 / 数据库 / 台账**。

    复用 ``_dedupe_of`` 与真实复制同一条判定，保证预检结论与实际执行一致。
    返回 ``wouldCopy`` / ``wouldSkip`` / ``duplicates`` 三组明细。
    """
    source_uid = source_uid.strip()
    target_uid = target_uid.strip()
    out: dict[str, Any] = {"wouldCopy": [], "wouldSkip": [], "duplicates": []}
    if not source_uid or not target_uid or source_uid == target_uid:
        return out

    doc = _read_ledger()
    entries = doc.entries
    reverse = doc.reverse()
    received = doc.received_keys()

    db = workbuddy_db_path()
    src_conn = _open_db(db)
    if src_conn is None:
        out["wouldSkip"].append({"id": "", "reason": "会话数据库不可读"})
        return out
    try:
        ids = (
            list(session_ids)
            if session_ids
            else [s["id"] for s in list_sessions_for_user(source_uid)]
        )
        planned: set[str] = set()
        planned_roots: set[tuple[str, str]] = set()
        for cid in ids:
            if cid in planned:
                out["wouldSkip"].append({"id": cid, "reason": "批内重复"})
                continue
            reason, (root_uid, root_cid) = _dedupe_of(
                cid,
                source_uid,
                target_uid,
                entries=entries,
                reverse=reverse,
                lineage=doc.lineage,
                received=received,
                planned_roots=planned_roots,
            )
            if reason is not None:
                out["wouldSkip"].append({"id": cid, "reason": reason, "rootId": root_cid})
                continue
            row = _read_session_row(src_conn, cid, source_uid)
            if row is None:
                cwd = _find_jsonl_cwd(cid)
                title = "(无标题)"
            else:
                cols, vals = row
                cwd = str(_col_value(cols, vals, "cwd") or "")
                title = _row_title(cols, vals)
            if cwd and is_claw_workspace(cwd):
                out["wouldSkip"].append({"id": cid, "reason": "Claw 工作区，不支持复制"})
                continue
            planned.add(cid)
            planned_roots.add((root_uid, root_cid))
            out["wouldCopy"].append(
                {"id": cid, "title": title, "rootId": root_cid, "rootUid": root_uid}
            )
    finally:
        src_conn.close()

    out["duplicates"] = find_duplicate_copies(target_uid, doc)
    return out


# ---------- 内部 ----------


class SessionMigrationError(RuntimeError):
    """会话迁移层可预期失败，消息面向用户。"""


def _open_db(path: Path | None) -> sqlite3.Connection | None:
    if path is None or not path.is_file():
        return None
    try:
        return sqlite3.connect(str(path), timeout=5)
    except sqlite3.Error:
        return None


def _close(*conns: sqlite3.Connection | None) -> None:
    for conn in conns:
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def _read_session_row(
    conn: sqlite3.Connection, cid: str, source_uid: str
) -> tuple[list[str], list[Any]] | None:
    cols = _table_columns(conn, "sessions")
    if not cols:
        return None
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE id = ? AND user_id = ?", (cid, source_uid)
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return cols, list(row)


def _col_value(cols: list[str], vals: list[Any], name: str) -> Any:
    if name in cols:
        return vals[cols.index(name)]
    return None


def _row_title(cols: list[str], vals: list[Any]) -> str:
    custom = _col_value(cols, vals, "custom_title")
    title = _col_value(cols, vals, "title")
    for value in (custom, title):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "(无标题)"


def _find_jsonl(cid: str) -> Path | None:
    # 用本模块的 ``projects_dir``（测试会 monkeypatch 它），不要转发到 sessions 模块。
    projects = projects_dir()
    if not projects.is_dir():
        return None
    for workspace in projects.iterdir():
        if not workspace.is_dir():
            continue
        path = workspace / f"{cid}.jsonl"
        if path.is_file():
            return path
    return None


def _find_jsonl_cwd(cid: str) -> str:
    path = _find_jsonl(cid)
    if path is None:
        return ""
    try:
        first = path.open(encoding="utf-8", errors="replace").readline()
        data = json.loads(first)
        return str(data.get("cwd") or "")
    except (OSError, json.JSONDecodeError):
        return ""


def _target_jsonl_path(src_jsonl: Path | None, new_cid: str) -> Path:
    if src_jsonl is None:
        return projects_dir() / f"{new_cid}.jsonl"
    return src_jsonl.with_name(f"{new_cid}.jsonl")


def _copy_jsonl(src: Path, dst: Path, old_cid: str, new_cid: str) -> bool:
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        text = src.read_text(encoding="utf-8", errors="replace")
        text = text.replace(old_cid, new_cid)
        dst.write_text(text, encoding="utf-8")
        return True
    except OSError:
        return False


def _copy_companions(src_jsonl: Path, old_cid: str, new_cid: str) -> dict[str, bool]:
    """复制会话伴生文件：``.meta.json``（替换 cid）与 ``.file-rollback.ndjson``（仅改名）。

    回滚流水里含真实文件系统操作记录，改写 id 会破坏其语义，所以只改名不改内容。
    两者各自软失败，不影响主复制。
    """
    out = {"meta": False, "rollback": False}
    meta_src = src_jsonl.with_name(f"{old_cid}.meta.json")
    if meta_src.is_file():
        try:
            text = meta_src.read_text(encoding="utf-8", errors="replace")
            meta_src.with_name(f"{new_cid}.meta.json").write_text(
                text.replace(old_cid, new_cid), encoding="utf-8"
            )
            out["meta"] = True
        except OSError:
            out["meta"] = False
    rollback_src = src_jsonl.with_name(f"{old_cid}.file-rollback.ndjson")
    if rollback_src.is_file():
        try:
            rollback_src.with_name(f"{new_cid}.file-rollback.ndjson").write_bytes(
                rollback_src.read_bytes()
            )
            out["rollback"] = True
        except OSError:
            out["rollback"] = False
    return out


def _insert_session_row(
    conn: sqlite3.Connection,
    dst_cols: list[str],
    cols: list[str],
    vals: list[Any],
    new_cid: str,
    target_uid: str,
) -> bool:
    insert_cols: list[str] = []
    insert_vals: list[Any] = []
    for col, val in zip(cols, vals, strict=False):
        if col not in dst_cols:
            continue
        if col == "id":
            insert_vals.append(new_cid)
        elif col == "user_id":
            insert_vals.append(target_uid)
        elif col in ("created_at", "updated_at"):
            insert_vals.append(_now_ms())
        elif col == "deleted_at":
            insert_vals.append(None)
        else:
            insert_vals.append(val)
        insert_cols.append(col)
    if not insert_cols:
        return False
    placeholders = ", ".join(["?"] * len(insert_cols))
    colnames = ", ".join(insert_cols)
    sql = f"INSERT OR REPLACE INTO sessions ({colnames}) VALUES ({placeholders})"
    try:
        conn.execute(sql, insert_vals)
        conn.commit()
        return True
    except sqlite3.Error:
        return False


def _insert_edge_sync_mapping(
    conn: sqlite3.Connection, new_cid: str, target_uid: str
) -> bool:
    if not _table_columns(conn, "edge_sync_mapping"):
        return False
    try:
        conn.execute(
            "INSERT OR REPLACE INTO edge_sync_mapping "
            "(session_id, conversation_id, msg_channel, created_at) VALUES (?, ?, ?, ?)",
            (new_cid, new_cid, f"convmsg:{target_uid}", _now_secs()),
        )
        conn.commit()
        return True
    except sqlite3.Error:
        return False


def _backup_db(db: Path) -> str | str:
    """整库备份到 backups/sessions/{时间戳}/workbuddy.db。返回备份路径或错误文案。"""
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    dest_dir = workbuddy_data_dir() / "backups" / "sessions" / stamp
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "workbuddy.db"
        # 用 SQLite 在线备份 API，保证一致性。
        src = sqlite3.connect(str(db))
        try:
            dst = sqlite3.connect(str(dest))
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
        return str(dest)
    except (OSError, sqlite3.Error) as exc:
        return f"{exc}"


def _finalize_ledger(doc: LedgerDoc) -> None:
    if doc.entries or doc.lineage:
        _write_ledger(doc)


# ---------- 重复检测与清理 ----------


def _list_user_rows(uid: str) -> list[dict[str, Any]]:
    """列出某账号未删除会话的 id/标题/时间，供重复检测。"""
    db = workbuddy_db_path()
    if not db.is_file():
        return []
    conn = _open_db(db)
    if conn is None:
        return []
    try:
        cols = _table_columns(conn, "sessions")
        if not cols:
            return []
        custom = "custom_title" if "custom_title" in cols else "NULL"
        created = "created_at" if "created_at" in cols else "NULL"
        sql = (
            f"SELECT id, title, {custom}, cwd, {created}, updated_at FROM sessions "
            "WHERE user_id = ? AND deleted_at IS NULL"
        )
        out: list[dict[str, Any]] = []
        for cid, title, custom_title, cwd, created_at, updated_at in conn.execute(sql, (uid,)):
            out.append(
                {
                    "id": cid,
                    "title": (custom_title or title or "(无标题)").strip(),
                    "cwd": cwd or "",
                    "created_at": created_at if isinstance(created_at, int) else None,
                    "updated_at": updated_at if isinstance(updated_at, int) else 0,
                }
            )
        return out
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def find_duplicate_copies(uid: str, doc: LedgerDoc | None = None) -> list[dict[str, Any]]:
    """找出某账号下「同一血统」的多条会话（重复）。

    按根血统分组，每组保留最早一条（``created_at`` 优先，否则 ``updated_at``），
    其余列为重复项。返回分组列表，供前端展示与清理。
    """
    doc = doc or _read_ledger()
    reverse = doc.reverse()
    lineage = doc.lineage
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in _list_user_rows(uid):
        root = _root_of(row["id"], uid, reverse, lineage)
        groups.setdefault(root, []).append(row)

    out: list[dict[str, Any]] = []
    for root, rows in groups.items():
        if len(rows) < 2:
            continue
        rows.sort(
            key=lambda r: (
                r["created_at"] if r["created_at"] is not None else r["updated_at"],
                r["updated_at"],
            )
        )
        out.append({"rootUid": root[0], "rootId": root[1], "kept": rows[0], "dups": rows[1:]})
    return out


def _quarantine_session_files(cid: str, dest_dir: Path) -> list[str]:
    """把会话的三件套移入备份目录，返回已移动的文件名。"""
    src = _find_jsonl(cid)
    if src is None:
        return []
    names = [f"{cid}.jsonl", f"{cid}.meta.json", f"{cid}.file-rollback.ndjson"]
    moved: list[str] = []
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in names:
        candidate = src.with_name(name)
        if not candidate.is_file():
            continue
        try:
            candidate.replace(dest_dir / name)
            moved.append(name)
        except OSError:
            continue
    return moved


def cleanup_duplicates(uid: str, dup_ids: list[str]) -> dict[str, Any]:
    """把重复会话软删除（``deleted_at``）并把正文移入备份目录。返回报告。"""
    uid = uid.strip()
    if not uid or not dup_ids:
        raise SessionMigrationError("缺少账号或待清理会话")
    db = workbuddy_db_path()
    conn = _open_db(db)
    if conn is None:
        raise SessionMigrationError("会话数据库不可读，无法清理")

    result: dict[str, Any] = {"removed": [], "errors": [], "backup": None}
    try:
        cols = _table_columns(conn, "sessions")
        if "deleted_at" not in cols:
            raise SessionMigrationError("会话表无 deleted_at 字段，无法软删除")
        backup = _backup_db(db)
        if isinstance(backup, str):
            result["backup"] = backup
        else:
            result["backupError"] = backup

        stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
        dest_dir = workbuddy_data_dir() / "backups" / "sessions" / stamp / "dup-jsonl"
        for cid in dup_ids:
            try:
                cur = conn.execute(
                    "UPDATE sessions SET deleted_at = ? WHERE id = ? AND user_id = ?",
                    (_now_ms(), cid, uid),
                )
                conn.commit()
                moved = _quarantine_session_files(cid, dest_dir)
                result["removed"].append(
                    {"id": cid, "softDeleted": cur.rowcount > 0, "moved": moved}
                )
            except sqlite3.Error as exc:
                result["errors"].append({"id": cid, "error": str(exc)})
        return result
    finally:
        conn.close()
