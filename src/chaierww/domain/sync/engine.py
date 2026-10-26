"""会话同步引擎：采集 → 对齐 → 应用 → 校验。

## 边界（写死在能力矩阵里，不靠调用方自觉）

- 只有 WorkBuddy 国内版有本地会话库（``workbuddy.db`` + ``projects/*.jsonl``）；
  Trae 与其余 scope 一律返回 ``unavailable``，不产生任何写操作。
- 采集是**只读**的，客户端运行中也能跑；应用会写 ``workbuddy.db`` 与正文目录，
  因此必须在客户端退出后执行（与切号流程同一个前提）。
- 一期的「同步」是**本机内的账号级镜像**：不跨设备、不跨平台、不实时。

## 稳定映射

一条会话的血统根决定 ``stableKey``；同一 ``(account_id, stableKey)`` 只会落地一次。
因此 A→B→A 往返不会增殖副本 —— 这是与「每次切号生成新 cid」最大的区别。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from chaierww.adapters.base import AdapterError, PlatformAdapter
from chaierww.domain.accounts.repository import AccountRepository
from chaierww.domain.scopes import require_implemented
from chaierww.domain.session_migration import (
    _insert_edge_sync_mapping,
    _insert_session_row,
    backup_session_db,
    lineage_root,
    register_copy,
)
from chaierww.domain.sessions import (
    find_jsonl,
    is_claw_workspace,
    projects_dir,
    session_rows_for_user,
    workbuddy_db_path,
)
from chaierww.domain.sync.canonical import (
    canonicalize,
    line_count,
    localize,
    stable_key,
)
from chaierww.domain.sync.repository import (
    SYNC_APPLIED,
    SYNC_BLOCKED,
    SYNC_FAILED,
    SYNC_IDLE,
    SyncRepository,
)
from chaierww.domain.sync.store import BlobStore
from chaierww.infra.db import now_iso, transaction

#: 哪些 scope 有本地会话数据可同步。Trae 是 IDE，没有对应会话库。
CONVERSATION_SUPPORT = {"workbuddy-cn": True, "trae-cn": False}

#: 单条会话正文上限（防一次性读入超大文件）。超过则跳过并在报告里如实说明。
MAX_SESSION_BYTES = 64 * 1024 * 1024

#: 伴生文件存储上限（meta / rollback 通常很小）。
MAX_COMPANION_BYTES = 2 * 1024 * 1024

#: 单次应用最多复核多少条已落地会话的冲突（避免 O(全部会话) 的重复哈希）。
MAX_CONFLICT_CHECKS = 200

#: 采集行里不随镜像走的列（由落地目标重新决定）。
RESERVED_COLUMNS = frozenset({"id", "user_id", "created_at", "updated_at", "deleted_at"})


class SyncError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SyncStep:
    step: str
    state: str  # ok | warn | failed | skipped | blocked | unavailable
    detail: str = ""


@dataclass
class SyncReport:
    direction: str
    scope: str
    account_id: str
    state: str = "ok"  # ok | partial | failed | blocked | unavailable | planned
    steps: list[SyncStep] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    plan: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    backup: str | None = None
    error: str | None = None
    dry_run: bool = False

    @property
    def succeeded(self) -> bool:
        return self.state in ("ok", "planned")

    def add(self, step: str, state: str, detail: str = "") -> None:
        self.steps.append(SyncStep(step=step, state=state, detail=detail))

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "direction": self.direction,
            "scope": self.scope,
            "accountId": self.account_id,
            "dryRun": self.dry_run,
            "succeeded": self.succeeded,
            "backup": self.backup,
            "error": self.error,
            "counts": self.counts,
            "plan": self.plan,
            "errors": self.errors,
            "steps": [
                {"step": step.step, "state": step.state, "detail": step.detail}
                for step in self.steps
            ],
        }


def conversation_supported(scope: str) -> bool:
    return CONVERSATION_SUPPORT.get(scope, False)


def _now_ms() -> int:
    return int(datetime.now().timestamp() * 1000)


def _open_rw() -> sqlite3.Connection | None:
    db = workbuddy_db_path()
    if not db.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error:
        return None


def _table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > MAX_SESSION_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _read_companion(path: Path) -> str | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_COMPANION_BYTES:
            return None
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _json_safe(value: Any) -> Any:
    return value if isinstance(value, (str, int, float, bool)) or value is None else None


class SyncEngine:
    def __init__(
        self,
        accounts: AccountRepository,
        sync: SyncRepository,
        home: Path,
        adapters: dict[str, PlatformAdapter] | None = None,
    ) -> None:
        self._repo = accounts
        self._sync = sync
        self._home = home
        self._adapters = adapters or {}
        self._blobs = BlobStore(home / "sync")
        self._locks: dict[str, threading.RLock] = {}
        self._locks_guard = threading.Lock()

    # ---------- 基础设施 ----------

    def lock_for(self, scope: str) -> threading.RLock:
        """作用域级可重入锁：调度器与切号引擎共用，保证两个流程不交叉。"""
        with self._locks_guard:
            lock = self._locks.get(scope)
            if lock is None:
                lock = threading.RLock()
                self._locks[scope] = lock
            return lock

    def adapter(self, scope: str) -> PlatformAdapter | None:
        return self._adapters.get(scope)

    def _account(self, scope: str, account_id: str) -> Any:
        require_implemented(scope)
        account = self._repo.get(account_id)
        if account.scope != scope:
            raise SyncError(f"账号 {account_id} 不属于作用域 {scope}")
        if account.is_deleted:
            raise SyncError("账号已在回收站，不能同步")
        return account

    def _ensure_state(self, account: Any) -> None:
        self._sync.ensure_state(account.id, account.scope, account.external_id)

    # ---------- 采集 ----------

    def capture(self, scope: str, account_id: str, *, record_job: bool = True) -> SyncReport:
        """把该账号在客户端里的全部会话采进镜像（只读，客户端运行中也安全）。"""
        with self.lock_for(scope):
            account = self._account(scope, account_id)
            self._ensure_state(account)
            report = SyncReport(direction="capture", scope=scope, account_id=account_id)
            report.counts = {
                "scanned": 0,
                "captured": 0,
                "changed": 0,
                "unchanged": 0,
                "skippedClaw": 0,
                "tombstones": 0,
                "failed": 0,
            }

            if not conversation_supported(scope):
                report.state = "unavailable"
                report.error = "该平台没有本地会话库，一期不支持会话同步"
                report.add("能力检查", "unavailable", report.error)
                self._sync.update_state(
                    account_id, state=SYNC_IDLE, last_error=report.error
                )
                return report

            job_id = (
                self._sync.start_job(scope, account_id, "capture") if record_job else None
            )
            rows = session_rows_for_user(account.external_id)
            if rows is None:
                report.state = "failed"
                report.error = "读不到会话库（客户端运行中或数据库损坏）"
                report.add("读取会话库", "failed", report.error)
                self._fail_job(job_id, "读取会话库", report.error)
                self._sync.update_state(
                    account_id, state=SYNC_FAILED, last_error=report.error
                )
                return report
            report.add("读取会话库", "ok", f"{len(rows)} 条会话索引")

            items: list[dict[str, Any]] = []
            existing_map = {
                row["stable_key"]: row
                for row in self._sync.items(account.id, include_deleted=True)
            }
            for row in rows:
                cid = str(row.get("id") or "").strip()
                if not cid:
                    continue
                report.counts["scanned"] += 1
                cwd = str(row.get("cwd") or "")
                if is_claw_workspace(cwd):
                    report.counts["skippedClaw"] += 1
                    continue
                try:
                    item = self._capture_one(scope, account, row, cid)
                except (OSError, sqlite3.Error, ValueError) as exc:
                    report.counts["failed"] += 1
                    report.errors.append({"cid": cid, "error": str(exc)})
                    continue
                if item is None:
                    report.counts["unchanged"] += 1
                    continue
                previous = existing_map.get(item["stable_key"])
                if (
                    previous is not None
                    and previous["content_sha256"] == item["content_sha256"]
                    and previous["deleted_at"] == item["deleted_at"]
                ):
                    report.counts["unchanged"] += 1
                    continue
                items.append(item)
                if item["deleted_at"]:
                    report.counts["tombstones"] += 1

            changed = self._sync.upsert_many(account.id, items)
            report.counts["captured"] = len(items)
            report.counts["changed"] = changed
            self._sync.refresh_counts(account.id)

            # 把本次采到的会话分发给同一作用域里的其它账号（各一份「待应用」记录）。
            # 这样目标账号在切过去之前就已经知道自己该补哪些会话。
            if items:
                pending = [
                    {**item, "local_cid": None, "applied_at": None} for item in items
                ]
                for other in self._repo.list_accounts(scope):
                    if other.id == account.id:
                        continue
                    self._sync.ensure_state(other.id, other.scope, other.external_id)
                    self._sync.upsert_many(other.id, pending)
                    self._sync.refresh_counts(other.id)

            report.add(
                "采集会话",
                "ok",
                f"新增/更新 {report.counts['captured']} 条"
                f"（其中内容变化 {report.counts['changed']} 条），"
                f"未变 {report.counts['unchanged']} 条",
            )
            if report.counts["skippedClaw"]:
                report.add("Claw 工作区", "skipped", f"{report.counts['skippedClaw']} 条不支持复制")
            if report.counts["failed"]:
                report.add("失败条目", "warn", f"{report.counts['failed']} 条采集失败")

            total, pending = self._sync.refresh_counts(account_id)
            self._sync.update_state(
                account_id,
                state=SYNC_IDLE if not report.counts["failed"] else SYNC_FAILED,
                last_captured_at=now_iso(),
                last_error="",
            )
            report.add("镜像清单", "ok", f"共 {total} 条，待应用 {pending} 条")
            with contextlib.suppress(Exception):
                removed = self._blobs.gc(self._sync.referenced_blobs())
                if removed:
                    report.add("清理镜像", "ok", f"回收 {removed} 份无引用正文")

            if report.counts["failed"]:
                report.state = "partial"
                self._finish_job(job_id, "partial", report, error="部分会话采集失败")
            else:
                report.state = "ok"
                self._finish_job(job_id, "succeeded", report)
            return report

    def _capture_one(
        self, scope: str, account: Any, row: dict[str, Any], cid: str
    ) -> dict[str, Any] | None:
        """采集一条会话。返回 ``None`` 表示内容未变，否则返回待写入的镜像条目。"""
        uid = account.external_id
        root_uid, root_cid = lineage_root(uid, cid)
        key = stable_key(scope, root_uid, root_cid)

        jsonl = find_jsonl(cid)
        text = ""
        workspace = ""
        companions: dict[str, str | None] = {"meta": None, "rollback": None}
        if jsonl is not None:
            raw = _read_text(jsonl)
            if raw is None:
                raise OSError(f"正文过大或不可读：{jsonl.name}")
            text = raw
            workspace = jsonl.parent.name
            companions["meta"] = _read_companion(jsonl.with_name(f"{cid}.meta.json"))
            companions["rollback"] = _read_companion(
                jsonl.with_name(f"{cid}.file-rollback.ndjson")
            )

        canonical_text = canonicalize(text, cid, key)
        sha, size = self._blobs.put(canonical_text) if canonical_text else ("", 0)

        columns = {
            name: _json_safe(value)
            for name, value in row.items()
            if name not in RESERVED_COLUMNS
        }
        meta = {
            "sourceUid": uid,
            "sourceCid": cid,
            "rootUid": root_uid,
            "rootCid": root_cid,
            "workspace": workspace,
            "columns": columns,
            "sessionMeta": companions["meta"],
            "rollback": companions["rollback"],
        }
        item = {
            "stable_key": key,
            "local_cid": cid,
            "workspace": workspace,
            "title": _display_title(row),
            "content_sha256": sha,
            "content_bytes": size,
            "content_lines": line_count(canonical_text),
            "meta_json": json.dumps(meta, ensure_ascii=False),
            "source_updated_at": str(row.get("updated_at") or ""),
            "captured_at": now_iso(),
            "deleted_at": str(row.get("deleted_at")) if row.get("deleted_at") else None,
        }
        return item

    def capture_all(self, scope: str) -> list[SyncReport]:
        """采集该 scope 下所有未删除账号（定时任务用）。"""
        reports: list[SyncReport] = []
        for account in self._repo.list_accounts(scope):
            with contextlib.suppress(Exception):
                reports.append(self.capture(scope, account.id))
        return reports

    # ---------- 应用 ----------

    def apply(
        self,
        scope: str,
        account_id: str,
        *,
        dry_run: bool = False,
        record_job: bool = True,
    ) -> SyncReport:
        """把该账号镜像里「尚未落地」的会话补进客户端（需客户端已退出）。"""
        with self.lock_for(scope):
            account = self._account(scope, account_id)
            self._ensure_state(account)
            report = SyncReport(
                direction="apply", scope=scope, account_id=account_id, dry_run=dry_run
            )
            report.counts = {
                "pending": 0,
                "created": 0,
                "reapplied": 0,
                "appended": 0,
                "conflicts": 0,
                "removed": 0,
                "failed": 0,
            }

            if not conversation_supported(scope):
                report.state = "unavailable"
                report.error = "该平台没有本地会话库，一期不支持会话同步"
                report.add("能力检查", "unavailable", report.error)
                return report

            adapter = self.adapter(scope)
            running = False
            if adapter is not None:
                with contextlib.suppress(AdapterError, OSError):
                    running = adapter.is_running()
            if running and not dry_run:
                report.state = "blocked"
                report.error = "客户端正在运行：写入会话库会与客户端抢写，请先退出客户端"
                report.add("客户端状态", "blocked", report.error)
                self._sync.update_state(
                    account_id, state=SYNC_BLOCKED, last_error=report.error
                )
                return report

            items = self._sync.items(account_id, include_deleted=True)
            pending = [
                item
                for item in items
                if item.get("local_cid") is None and not item.get("deleted_at")
            ]
            landed = [
                item
                for item in items
                if item.get("local_cid") and not item.get("deleted_at")
            ]
            tombstones = [item for item in items if item.get("deleted_at")]

            report.counts["pending"] = len(pending)
            for item in pending:
                report.plan.append(_plan_entry("create", item))
            for item in tombstones:
                report.plan.append(_plan_entry("remove", item))

            conn = _open_rw()
            if conn is None and not dry_run:
                report.state = "failed"
                report.error = "找不到 workbuddy.db，无法落地会话"
                report.add("打开会话库", "failed", report.error)
                return report

            try:
                reapply, conflicts, appends = self._reconcile_landed(
                    conn, account, landed, report
                )
                for item in reapply:
                    report.plan.append(_plan_entry("reapply", item))
                for item, _tail in appends:
                    report.plan.append(_plan_entry("append", item))
                for item in conflicts:
                    report.plan.append(_plan_entry("conflict", item))

                if dry_run:
                    report.state = "planned"
                    report.add(
                        "预演",
                        "ok",
                        f"将新增 {len(pending)} 条、重新落地 {len(reapply)} 条、"
                        f"追加 {len(appends)} 条、保留冲突副本 {len(conflicts)} 条、"
                        f"软删除 {len(tombstones)} 条",
                    )
                    return report

                assert conn is not None
                job_id = (
                    self._sync.start_job(scope, account_id, "apply") if record_job else None
                )
                backup = backup_session_db()
                if backup is None:
                    report.state = "failed"
                    report.error = "会话库备份失败，已中止（未写入任何会话）"
                    report.add("备份", "failed", report.error)
                    self._fail_job(job_id, "备份", report.error)
                    self._sync.update_state(
                        account_id, state=SYNC_FAILED, last_error=report.error
                    )
                    return report
                report.backup = backup
                report.add("备份", "ok", f"备份点 {backup}")

                for item in pending:
                    self._materialize(conn, account, item, report, kind="create")
                for item in reapply:
                    self._materialize(conn, account, item, report, kind="reapply")
                for item, tail in appends:
                    self._append_tail(item, tail, report)
                for item in conflicts:
                    self._materialize_conflict(conn, account, item, report)
                for item in tombstones:
                    self._soft_delete(conn, account, item, report)

                conn.commit()
            finally:
                if conn is not None:
                    conn.close()

            total, pending_count = self._sync.refresh_counts(account_id)
            failed = report.counts["failed"]
            report.state = "partial" if failed else "ok"
            self._sync.update_state(
                account_id,
                state=SYNC_APPLIED if not failed else SYNC_FAILED,
                last_applied_at=now_iso(),
                last_error="" if not failed else "部分会话落地失败",
            )
            report.add(
                "落地会话",
                "ok" if not failed else "warn",
                f"新增 {report.counts['created']} 条、重新落地 {report.counts['reapplied']} 条、"
                f"追加 {report.counts['appended']} 条、"
                f"冲突副本 {report.counts['conflicts']} 条、软删除 {report.counts['removed']} 条；"
                f"镜像共 {total} 条，仍待应用 {pending_count} 条",
            )
            self._finish_job(job_id, "partial" if failed else "succeeded", report)
            return report

    def _reconcile_landed(
        self,
        conn: sqlite3.Connection | None,
        account: Any,
        landed: list[dict[str, Any]],
        report: SyncReport,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[dict[str, Any], str]]]:
        """复核「已落地」的会话。

        三种结果，语义刻意分开：

        - **重新落地**：文件或索引行丢了（客户端清理过缓存）；
        - **追加**：本地是镜像的旧前缀（这条会话是别的号在继续写），只补尾段，
          避免把整条会话重新复制成新 id；
        - **冲突**：两端都改过（分叉），保留本地并把镜像版本另存副本。
        """
        reapply: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        appends: list[tuple[dict[str, Any], str]] = []
        if conn is None:
            return reapply, conflicts, appends
        uid = account.external_id
        checked = 0
        for item in landed:
            local_cid = item.get("local_cid")
            if not local_cid:
                continue
            path = find_jsonl(local_cid)
            row = conn.execute(
                "SELECT deleted_at FROM sessions WHERE id = ? AND user_id = ?",
                (local_cid, uid),
            ).fetchone()
            if path is None or row is None:
                reapply.append(item)
                continue
            if checked >= MAX_CONFLICT_CHECKS:
                continue
            checked += 1
            if item.get("conflict_sha256") == item.get("content_sha256"):
                continue
            local = _read_text(path)
            if local is None or not item.get("content_sha256"):
                continue
            mirror = self._blobs.get(item["content_sha256"])
            if mirror is None:
                continue
            local_canonical = canonicalize(local, local_cid, item["stable_key"])
            if local_canonical == mirror:
                continue
            if mirror.startswith(local_canonical):
                appends.append((item, mirror[len(local_canonical):]))
                continue
            if local_canonical.startswith(mirror):
                # 本地比镜像新：这个号在镜像之后继续聊过，等它自己被采集时自然收敛。
                continue
            conflicts.append(item)
        return reapply, conflicts, appends

    def _append_tail(
        self, item: dict[str, Any], tail: str, report: SyncReport
    ) -> None:
        """把镜像尾段追加到本地正文（仅追加，绝不覆盖已有内容）。"""
        local_cid = item.get("local_cid")
        if not local_cid or not tail:
            return
        path = find_jsonl(local_cid)
        if path is None:
            report.counts["failed"] += 1
            report.errors.append(
                {"stableKey": item["stable_key"], "error": "本地正文不存在，无法追加"}
            )
            return
        try:
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(localize(tail, item["stable_key"], local_cid))
        except OSError as exc:
            report.counts["failed"] += 1
            report.errors.append(
                {"stableKey": item["stable_key"], "error": f"追加失败：{exc}"}
            )
            return
        report.counts["appended"] += 1

    def _materialize(
        self,
        conn: sqlite3.Connection,
        account: Any,
        item: dict[str, Any],
        report: SyncReport,
        *,
        kind: str,
    ) -> None:
        new_cid = str(uuid.uuid4())
        ok, error = self._write_conversation(conn, account, item, new_cid)
        if not ok:
            report.counts["failed"] += 1
            report.errors.append({"stableKey": item["stable_key"], "error": error})
            return
        self._sync.mark_applied(account.id, item["stable_key"], new_cid)
        if kind == "reapply":
            report.counts["reapplied"] += 1
        else:
            report.counts["created"] += 1

    def _materialize_conflict(
        self,
        conn: sqlite3.Connection,
        account: Any,
        item: dict[str, Any],
        report: SyncReport,
    ) -> None:
        new_cid = str(uuid.uuid4())
        title = item.get("title") or "(无标题)"
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        ok, error = self._write_conversation(
            conn,
            account,
            item,
            new_cid,
            title_override=f"{title}（冲突副本 {stamp}）",
            ledger_source=(
                account.external_id,
                str(item.get("local_cid") or ""),
            ),
        )
        if not ok:
            report.counts["failed"] += 1
            report.errors.append({"stableKey": item["stable_key"], "error": error})
            return
        self._sync.mark_conflict(
            account.id, item["stable_key"], item["content_sha256"], item["stable_key"]
        )
        report.counts["conflicts"] += 1

    def _write_conversation(
        self,
        conn: sqlite3.Connection,
        account: Any,
        item: dict[str, Any],
        new_cid: str,
        *,
        title_override: str | None = None,
        ledger_source: tuple[str, str] | None = None,
    ) -> tuple[bool, str]:
        meta = SyncRepository.meta(item)
        mirror = self._blobs.get(item.get("content_sha256") or "")
        if mirror is None:
            return False, "镜像正文缺失或校验失败"

        workspace = str(meta.get("workspace") or item.get("workspace") or "").strip()
        if not workspace:
            return False, "缺少工作区目录，无法定位正文存放位置"
        dest = projects_dir() / workspace / f"{new_cid}.jsonl"
        try:
            _atomic_write(dest, localize(mirror, item["stable_key"], new_cid))
        except OSError as exc:
            return False, f"写正文失败：{exc.strerror or exc}"

        session_meta = meta.get("sessionMeta")
        if isinstance(session_meta, str) and session_meta:
            with contextlib.suppress(OSError):
                _atomic_write(
                    dest.with_name(f"{new_cid}.meta.json"),
                    session_meta.replace(item["stable_key"], new_cid).replace(
                        str(meta.get("sourceCid") or ""), new_cid
                    ),
                )
        rollback = meta.get("rollback")
        if isinstance(rollback, str) and rollback:
            with contextlib.suppress(OSError):
                _atomic_write(dest.with_name(f"{new_cid}.file-rollback.ndjson"), rollback)

        columns = meta.get("columns")
        if not isinstance(columns, dict):
            columns = {}
        if title_override:
            # 标题可能落在 title 或 custom_title（客户端优先显示 custom_title），
            # 两列都在时一起改写，冲突副本才会在列表里一眼可辨。
            for key in ("custom_title", "title"):
                if key in columns:
                    columns[key] = title_override

        dst_cols = _table_columns(conn, "sessions")
        if not dst_cols:
            return True, "会话库无 sessions 表，仅写入了正文"

        source_uid = str(meta.get("sourceUid") or "")
        source_cid = str(meta.get("sourceCid") or "")
        if ledger_source is not None:
            source_uid, source_cid = ledger_source
        root_uid = str(meta.get("rootUid") or source_uid)
        root_cid = str(meta.get("rootCid") or source_cid)

        cols = ["id", "user_id", "created_at", "updated_at", "deleted_at", *columns.keys()]
        vals: list[Any] = [new_cid, account.external_id, _now_ms(), _now_ms(), None]
        vals.extend(columns.values())
        inserted = _insert_session_row(
            conn, dst_cols, cols, vals, new_cid, account.external_id
        )
        if not inserted:
            return False, "写入会话索引失败"
        _insert_edge_sync_mapping(conn, new_cid, account.external_id)
        if source_uid and source_cid:
            with contextlib.suppress(Exception):
                register_copy(
                    source_uid,
                    source_cid,
                    account.external_id,
                    new_cid,
                    root_uid,
                    root_cid,
                )
        return True, ""

    def _soft_delete(
        self,
        conn: sqlite3.Connection,
        account: Any,
        item: dict[str, Any],
        report: SyncReport,
    ) -> None:
        local_cid = item.get("local_cid")
        if not local_cid:
            self._sync.mark_tombstone(account.id, item["stable_key"], item.get("deleted_at"))
            return
        try:
            with transaction(conn):
                conn.execute(
                    "UPDATE sessions SET deleted_at = ? WHERE id = ? AND user_id = ?",
                    (_now_ms(), local_cid, account.external_id),
                )
        except sqlite3.Error as exc:
            report.counts["failed"] += 1
            report.errors.append({"stableKey": item["stable_key"], "error": str(exc)})
            return
        self._sync.mark_tombstone(account.id, item["stable_key"], item.get("deleted_at"))
        report.counts["removed"] += 1

    # ---------- 冲突解决 ----------

    def resolve_conflict(self, scope: str, account_id: str, key: str, action: str) -> SyncReport:
        """解决一条冲突。``action``：keepSource / keepMirror / keepBoth。"""
        with self.lock_for(scope):
            self._account(scope, account_id)
            report = SyncReport(
                direction="resolve", scope=scope, account_id=account_id
            )
            item = self._sync.get_item(account_id, key)
            if item is None:
                raise SyncError("找不到该会话")
            local_cid = item.get("local_cid")
            if action == "keepMirror" and local_cid:
                mirror = self._blobs.get(item.get("content_sha256") or "")
                path = find_jsonl(local_cid)
                if mirror is None or path is None:
                    report.state = "failed"
                    report.error = "镜像或本地正文缺失，无法覆盖"
                    return report
                try:
                    _atomic_write(
                        path, localize(mirror, item["stable_key"], local_cid)
                    )
                except OSError as exc:
                    report.state = "failed"
                    report.error = f"写本地正文失败：{exc.strerror or exc}"
                    return report
                report.add("覆盖本地", "ok", "已用镜像版本覆盖本地会话")
            elif action == "keepSource":
                report.add("保留本地", "ok", "保留客户端版本，镜像不再提示冲突")
            elif action == "keepBoth":
                report.add("两者都留", "ok", "冲突副本已保留，镜像不再提示冲突")
            else:
                raise SyncError(f"未知的解决方式：{action}")

            self._sync.clear_conflict(account_id, key)
            total, pending = self._sync.refresh_counts(account_id)
            self._sync.update_state(
                account_id, last_error="", state=SYNC_APPLIED if not pending else SYNC_IDLE
            )
            report.state = "ok"
            report.add("清单", "ok", f"共 {total} 条，待应用 {pending} 条")
            return report

    # ---------- 查询 ----------

    def overview(self, scope: str) -> dict[str, Any]:
        """账号页 / 设置页用的同步概况：号池账号 + 同步状态合并。"""
        require_implemented(scope)
        states = {row["account_id"]: row for row in self._sync.state_rows(scope)}
        accounts = self._repo.list_accounts(scope)
        rows: list[dict[str, Any]] = []
        totals = {"accounts": len(accounts), "items": 0, "pending": 0, "conflicts": 0}
        for account in accounts:
            state = states.get(account.id, {})
            items = self._sync.items(account.id)
            conflicts = len([item for item in items if item.get("conflict_sha256")])
            total = int(state.get("items_total") or len(items))
            pending = int(state.get("items_pending") or 0)
            totals["items"] += total
            totals["pending"] += pending
            totals["conflicts"] += conflicts
            rows.append(
                {
                    "accountId": account.id,
                    "displayName": account.display_name,
                    "externalId": account.external_id,
                    "enabled": bool(state.get("enabled", 1)),
                    "state": state.get("state", "idle"),
                    "items": total,
                    "pending": pending,
                    "conflicts": conflicts,
                    "lastCapturedAt": state.get("last_captured_at"),
                    "lastAppliedAt": state.get("last_applied_at"),
                    "lastError": state.get("last_error", ""),
                }
            )
        return {
            "scope": scope,
            "supported": {
                "conversations": conversation_supported(scope),
                "webview": scope == "workbuddy-cn",
            },
            "totals": totals,
            "blobBytes": self._blobs.total_bytes(),
            "accounts": rows,
        }

    def forget_account(self, scope: str, account_id: str) -> int:
        """永久删除账号后清理镜像：删行 + 回收因此失去引用的正文。

        账号行此刻已经删掉（``AccountPurger`` 的顺序是「先删库、再清外设」），
        所以这里不再调 ``_account`` 校验存在性。清理失败由调用方降级成警告
        —— 账号已经删了，不值得为一份没人引用的镜像回滚。
        """
        with self.lock_for(scope):
            removed = self._sync.forget_account(account_id)
            self._blobs.gc(self._sync.referenced_blobs())
        return removed

    def items(self, scope: str, account_id: str) -> list[dict[str, Any]]:
        self._account(scope, account_id)
        out = []
        for item in self._sync.items(account_id):
            out.append(
                {
                    "stableKey": item["stable_key"],
                    "title": item["title"],
                    "workspace": item["workspace"],
                    "bytes": item["content_bytes"],
                    "lines": item["content_lines"],
                    "localCid": item["local_cid"],
                    "capturedAt": item["captured_at"],
                    "appliedAt": item["applied_at"],
                    "conflict": bool(item.get("conflict_sha256")),
                    "pending": item["local_cid"] is None,
                }
            )
        return out

    # ---------- 任务收尾 ----------

    def _finish_job(
        self,
        job_id: str | None,
        state: str,
        report: SyncReport,
        *,
        error: str = "",
    ) -> None:
        if not job_id:
            return
        self._sync.finish_job(
            job_id,
            state,
            items_total=sum(report.counts.values()),
            items_changed=int(report.counts.get("changed") or report.counts.get("created") or 0),
            error=error or report.error or "",
        )

    def _fail_job(self, job_id: str | None, step: str, error: str) -> None:
        if not job_id:
            return
        self._sync.finish_job(job_id, "failed", failed_step=step, error=error)


def _display_title(row: dict[str, Any]) -> str:
    for key in ("custom_title", "title"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "(无标题)"


def _plan_entry(action: str, item: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": action,
        "stableKey": item["stable_key"],
        "title": item["title"],
        "workspace": item["workspace"],
        "bytes": item["content_bytes"],
    }


__all__ = [
    "CONVERSATION_SUPPORT",
    "SyncEngine",
    "SyncError",
    "SyncReport",
    "conversation_supported",
]
