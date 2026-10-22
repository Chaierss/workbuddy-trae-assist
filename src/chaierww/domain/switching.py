"""切换引擎。

顺序刻意是「先确认客户端已退出 → 再备份 → 再写入」：
若在客户端运行时备份，备份到的是正在变化的状态，回滚时反而会把用户带进更差的状态。
因此备份必须发生在静止状态。
"""

from __future__ import annotations

import base64
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from chaierww.adapters.base import AdapterError, PlatformAdapter, SwitchPlan
from chaierww.domain import webview_state
from chaierww.domain.accounts.models import CredentialState
from chaierww.domain.accounts.repository import AccountRepository
from chaierww.domain.backup import BackupError, BackupService, Snapshot
from chaierww.domain.scopes import ScopeNotImplemented, require_implemented
from chaierww.infra.db import now_iso, transaction


class SwitchError(RuntimeError):
    pass


def encode_payload(raw: bytes) -> dict[str, str]:
    """把客户端登录态原样封进凭据，不做任何解析或改造。"""
    return {
        "format": "raw-file",
        "encoding": "base64",
        "data": base64.b64encode(raw).decode("ascii"),
    }


def encode_token_payload(account: dict[str, Any]) -> dict[str, Any]:
    """把扫码采集的 token 账号封进凭据（明文 token + 原始响应）。

    与 raw-file 的区别：这是「OAuth 采集」得到的明文凭据，切换时不是整份文件写回，
    而是用 build_account_obj / build_auth_obj 构造成认证文件再写。
    """
    return {"format": "oauth-token", **account}


@dataclass(frozen=True, slots=True)
class DecodedPayload:
    """凭据解码结果：按 format 区分形态，切换引擎据此选择写回路径。"""

    format: str
    file_bytes: bytes | None = None
    account: dict[str, Any] | None = None


def decode_payload(credential: dict[str, Any]) -> DecodedPayload:
    """解码凭据。支持两种形态：raw-file（登录态文件原文）与 oauth-token（扫码明文）。"""
    fmt = credential.get("format")
    if fmt == "raw-file":
        if credential.get("encoding") != "base64":
            raise SwitchError("凭据格式不受支持，请重新导入该账号")
        raw = credential.get("data")
        if not isinstance(raw, str):
            raise SwitchError("凭据内容损坏，请重新导入该账号")
        try:
            return DecodedPayload(
                format="raw-file",
                file_bytes=base64.b64decode(raw.encode("ascii"), validate=True),
            )
        except (ValueError, base64.binascii.Error) as exc:  # type: ignore[attr-defined]
            raise SwitchError("凭据内容损坏，请重新导入该账号") from exc
    if fmt == "oauth-token":
        return DecodedPayload(format="oauth-token", account=dict(credential))
    raise SwitchError("凭据格式不受支持，请重新导入该账号")


def _credential_warnings(payload: DecodedPayload, scope: str) -> tuple[str, ...]:
    """切过去之前先看这套凭据能不能**长期**用。

    `oauth-token` 是明文 token：缺 refresh token 就意味着一到 access token 过期就
    必然被踢回登录页，而且本工具补不出来（refresh token 由上游签发）。这种事只告警、
    不拦着切 —— 当下这个 token 可能还能用几小时，拦下来反而更糟。
    """
    if payload.format != "oauth-token" or payload.account is None:
        return ()
    refresh = payload.account.get("refreshToken") or payload.account.get("refresh_token")
    if isinstance(refresh, str) and refresh.strip():
        return ()
    return (
        "该账号的凭据没有 refresh token：access token 过期后无法自动续期，"
        "届时会要求重新登录。建议用「扫码登录」重新添加一次以补齐凭据。",
    )


@dataclass(frozen=True, slots=True)
class StepOutcome:
    step: str
    ok: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class SwitchReport:
    state: str
    dry_run: bool
    scope: str
    account_id: str
    steps: tuple[StepOutcome, ...] = ()
    snapshot_id: str | None = None
    error: str | None = None
    rollback_failed: bool = False
    plan: SwitchPlan | None = None
    migration: dict[str, Any] | None = None
    #: 切成功了但这套凭据有先天缺陷（缺 refresh token 之类）。
    #: 不拦着切，但要如实说出来 —— 否则用户过几小时被踢回登录页，完全不知道为什么。
    warnings: tuple[str, ...] = ()

    @property
    def succeeded(self) -> bool:
        return self.state == "succeeded"


@dataclass
class _Run:
    steps: list[StepOutcome] = field(default_factory=list)

    def add(self, step: str, ok: bool, detail: str = "") -> None:
        self.steps.append(StepOutcome(step=step, ok=ok, detail=detail))


class SwitchEngine:
    def __init__(
        self,
        repo: AccountRepository,
        backups: BackupService,
        adapters: dict[str, PlatformAdapter],
        conn: sqlite3.Connection,
        keep_snapshots: int = 5,
    ) -> None:
        self._repo = repo
        self._backups = backups
        self._adapters = adapters
        self._conn = conn
        self._keep = keep_snapshots
        self._lock = threading.RLock()

    def _adapter(self, scope: str, slot: str | None = None) -> PlatformAdapter:
        require_implemented(scope)
        adapter = self._adapters.get(scope)
        if adapter is None:
            raise ScopeNotImplemented(f"作用域 {scope} 尚未提供切换适配器")
        if slot is None:
            return adapter
        # 位解析交给适配器：只读 / 未适配的位必须在这里被挡下，
        # 绝不落到一个「能写但写错地方」的路径上。
        return adapter.slot_adapter(slot)

    def scope_of(self, account_id: str) -> str:
        """从账号 ID 反查作用域。

        合并号池里前端不传平台，切换/预演靠这里自动识别该账号属于哪个平台。
        account_id 全局唯一，反查是确定性的。
        """
        return self._repo.get(account_id).scope

    def preview_migration(
        self,
        scope: str,
        account_id: str,
        *,
        selection: Any = None,
        source_uid: str | None = None,
    ) -> dict[str, Any]:
        """切换前预检：只读推演将迁移的数据，**不写任何文件 / 库 / 台账**。

        源账号 = 当前客户端登录的账号（``source_uid`` 显式给出时优先），目标 =
        待切换账号；``selection`` 决定预检哪几块（与切换时的开关一致）。
        """
        if scope != "workbuddy-cn":
            return {"valid": False, "reason": "Trae 客户端无本地数据可迁移"}
        source = (source_uid or "").strip() or self.current_uid(scope) or ""
        if not source:
            return {"valid": False, "reason": "读不到当前登录账号，无法预检"}
        target = self._load_target(scope, account_id).external_id
        from chaierww.domain.migration import preview_account_data

        return preview_account_data(source, target, selection=selection)

    def current_uid(self, scope: str) -> str | None:
        """当前客户端登录账号的 uid（预检源账号用）。读不到返回 None。"""
        try:
            adapter = self._adapter(scope)
        except (ScopeNotImplemented, AdapterError):
            return None
        return self._current_external_id(adapter)

    def _plan(self, adapter: PlatformAdapter, payload: DecodedPayload) -> SwitchPlan:
        """按凭据形态选择预演路径：文件凭据 → plan_switch，token 凭据 → plan_token_switch。"""
        if payload.format == "oauth-token":
            assert payload.account is not None
            return adapter.plan_token_switch(payload.account)
        assert payload.file_bytes is not None
        return adapter.plan_switch(payload.file_bytes)

    def _apply(self, adapter: PlatformAdapter, payload: DecodedPayload) -> None:
        """按凭据形态选择写回路径：文件凭据 → apply_switch，token 凭据 → apply_token_switch。"""
        if payload.format == "oauth-token":
            assert payload.account is not None
            adapter.apply_token_switch(payload.account)
            return
        assert payload.file_bytes is not None
        adapter.apply_switch(payload.file_bytes)

    # ---------- 干跑 ----------

    def dry_run(self, scope: str, account_id: str, slot: str | None = None) -> SwitchReport:
        adapter = self._adapter(scope, slot)
        account = self._load_target(scope, account_id)
        payload = decode_payload(self._repo.current_credential(account_id))
        plan = self._plan(adapter, payload)
        run = _Run()
        run.add("读取凭据", True, f"目标账号 {account.display_name}")
        for item in plan.files:
            run.add(f"{item.action}:{item.path.name}", True, item.note)
        run.add(
            "客户端状态",
            True,
            "客户端运行中仍会尝试切换，登录态文件被锁时才会提示"
            if plan.requires_app_exit
            else "客户端未运行，可直接切换",
        )
        return SwitchReport(
            state="planned",
            dry_run=True,
            scope=scope,
            account_id=account_id,
            steps=tuple(run.steps),
            plan=plan,
        )

    # ---------- 真实切换 ----------

    def execute(
        self,
        scope: str,
        account_id: str,
        *,
        relaunch: bool = False,
        slot: str | None = None,
        migrate_memory: bool = True,
        migrate_connectors: bool = True,
        migrate_sessions: bool = True,
        migrate_profile: bool = True,
        migrate_workspaces: bool = True,
        migrate_webview: bool = True,
    ) -> SwitchReport:
        with self._lock:
            adapter = self._adapter(scope, slot)
            account = self._load_target(scope, account_id)
            payload = decode_payload(self._repo.current_credential(account_id))
            warnings = list(_credential_warnings(payload, scope))
            run = _Run()
            job_id = uuid.uuid4().hex
            started = now_iso()

            self._record_job(job_id, scope, None, account_id, None, "running", False, started)

            # 1. 预检凭据形态：token 账号在部分平台（如 Trae 加密信封）不可切换，
            #     坏账号（设备 ID 误当账号）也要在此拦下。预检**先于**退出客户端，
            #     确保「不可切换 / 坏账号」不会白白关掉正在运行的客户端、不留副作用。
            try:
                self._plan(adapter, payload)
            except AdapterError as exc:
                run.add("预检", False, str(exc))
                self._finish_job(job_id, "unsupported", "预检", str(exc))
                return SwitchReport(
                    state="unsupported",
                    dry_run=False,
                    scope=scope,
                    account_id=account_id,
                    steps=tuple(run.steps),
                    error=str(exc),
                )

            # 1.5 凭据一致性预检：历史遗留的坏账号可能把「设备 ID」误当成账号 ID
            #     存进 externalId，而凭据里真正的账号 ID 在别处。写入后校验必然失败，
            #     与其写到一半再回滚，不如在这里就拦下，给出明确提示、零副作用。
            verifier = getattr(adapter, "verify_incoming", None)
            if verifier is not None and payload.format == "raw-file":
                assert payload.file_bytes is not None
                if not verifier(payload.file_bytes, account.external_id):
                    detail = (
                        "目标账号的凭据标识与账号记录不一致（可能是旧版本导入的坏账号，"
                        "误把设备 ID 当成了账号 ID）。请删除该账号后重新导入。"
                    )
                    run.add("预检", False, detail)
                    self._finish_job(job_id, "unsupported", "预检", detail)
                    return SwitchReport(
                        state="unsupported",
                        dry_run=False,
                        scope=scope,
                        account_id=account_id,
                        steps=tuple(run.steps),
                        error=detail,
                    )

            # 2. 检查客户端：切换登录态会写客户端正在独占的登录态文件，客户端运行中会
            #    锁文件导致写入失败。优雅做法是「先退出 → 写文件 → 再重启」，而不是
            #    等写入失败再回滚（回滚时客户端还在运行，同样失败，制造「请手动恢复」的恐慌）。
            #    因此这里主动退出运行中的客户端进程。
            was_running = adapter.is_running()
            if was_running:
                terminated = adapter.terminate()
                adapter.wait_exit(timeout=5.0)
                run.add(
                    "退出客户端",
                    terminated,
                    "已退出运行中的客户端，切换后会自动重启" if terminated
                    else "客户端仍在运行，登录态文件可能被锁",
                )
            else:
                run.add("检查客户端", True, "客户端未运行，可直接切换")

            source_external_id = self._current_external_id(adapter)

            # 2.5 保存「切走前那个账号」的网页版登录态。
            #     客户端里乐享/文档/频道应用跑在按账号隔离的 Chromium 分区
            #     （Partitions/wb-webview-<uid>-*）。不存这一份，下次切回来网页版就是空的。
            #     客户端此刻已退出，分区文件不会被占用；失败不阻断切号。
            if migrate_webview and scope == "workbuddy-cn" and source_external_id:
                try:
                    saved = webview_state.snapshot(source_external_id)
                    if saved:
                        run.add("保存网页版登录态", True, f"已保存 {saved} 个网页分区")
                except OSError as exc:
                    run.add("保存网页版登录态", False, f"保存失败（不影响切号）：{exc}")

            # 3. 备份（失败即中止，不写入）
            try:
                snapshot = self._backups.create(
                    scope=scope,
                    source_dir=adapter.state_dir(),
                    reason="pre-switch",
                    account_id=account_id,
                )
            except BackupError as exc:
                run.add("备份", False, str(exc))
                self._finish_job(job_id, "failed_backup", "备份", str(exc))
                return SwitchReport(
                    state="failed_backup",
                    dry_run=False,
                    scope=scope,
                    account_id=account_id,
                    steps=tuple(run.steps),
                    error=f"{exc}；已中止，未做任何修改",
                )
            run.add("备份", True, f"备份点 {snapshot.id}（{snapshot.file_count} 个文件）")

            # 3. 写入
            try:
                self._apply(adapter, payload)
            except AdapterError as exc:
                return self._rollback_and_report(
                    adapter, snapshot, source_external_id, run, job_id, scope, account_id,
                    failed_step="写入", reason=str(exc),
                )
            except OSError as exc:
                return self._rollback_and_report(
                    adapter, snapshot, source_external_id, run, job_id, scope, account_id,
                    failed_step="写入", reason=f"写入失败：{exc.strerror or exc}",
                )
            run.add("写入", True, "登录态已替换")

            # 4. 校验
            if not adapter.verify_switch(account.external_id):
                return self._rollback_and_report(
                    adapter, snapshot, source_external_id, run, job_id, scope, account_id,
                    failed_step="校验", reason="回读内容与目标账号不一致",
                )
            run.add("校验", True, "回读确认已切换为目标账号")

            # 4.2 铺开目标账号的网页版登录态。
            #     有快照 → 铺回去，网页版跟着切号走；
            #     没快照 → 清掉旧账号的分区，否则网页版继续显示上一个身份，比空着更糟。
            #     两种情况都只影响网页版，失败一律不阻断切号。
            if migrate_webview and scope == "workbuddy-cn":
                try:
                    restored = webview_state.restore(account.external_id)
                    if restored:
                        run.add("网页版登录态", True, f"已恢复 {restored} 个网页分区")
                    elif source_external_id and source_external_id != account.external_id:
                        webview_state.drop(source_external_id)
                        run.add(
                            "网页版登录态",
                            False,
                            "该账号在本机没有网页版登录态，已清掉旧账号残留；网页版需要登录一次",
                        )
                        warnings.append(
                            "该账号在本机还没有网页版登录态：客户端网页版（乐享/文档等）"
                            "需要登录一次，之后切走再切回会自动保持。"
                        )
                except OSError as exc:
                    run.add("网页版登录态", False, f"迁移失败（不影响切号）：{exc}")

            # 4.5 数据迁移：账号切换成功后，把源账号的记忆/连接器/会话迁移到目标账号。
            #     仅 WorkBuddy 有对应本地数据（Trae 无）。迁移失败不阻断切换（切换已成功）。
            #     迁移哪几块由「设置 → 常规」的开关决定；全关则整块跳过，不产生迁移报告。
            migration: dict[str, Any] | None = None
            if (
                scope == "workbuddy-cn"
                and source_external_id
                and source_external_id != account.external_id
                and (
                    migrate_memory
                    or migrate_connectors
                    or migrate_sessions
                    or migrate_profile
                    or migrate_workspaces
                )
            ):
                migration = self._migrate_data(
                    run,
                    source_external_id,
                    account.external_id,
                    memory=migrate_memory,
                    connectors=migrate_connectors,
                    sessions=migrate_sessions,
                    profile=migrate_profile,
                    workspaces=migrate_workspaces,
                )

            # 5. 重启客户端：切换前已退出运行中的进程，这里重新拉起，让客户端
            #    重新加载新账号的登录态。只要「切换前客户端在运行」或显式要求 relaunch，
            #    就重启，避免把用户的客户端留在「已退出」状态。
            if relaunch or was_running:
                launched = adapter.launch()
                run.add("重启客户端", launched, "已重启" if launched else "未能启动，请手动打开")

            self._repo.touch_last_used(account_id)
            self._repo.set_credential_state(account_id, CredentialState.USABLE)
            self._finish_job(job_id, "succeeded", None, None, snapshot_id=snapshot.id)
            # 切换成功：pre-switch 备份已完成回滚兜底的使命，立即删除，不堆积常驻备份。
            # 常驻备份只由「每日自动 + 手动」承担（7 份滚动），临时备份不占配额。
            self._discard_temporary_snapshot(snapshot)
            return SwitchReport(
                state="succeeded",
                dry_run=False,
                scope=scope,
                account_id=account_id,
                steps=tuple(run.steps),
                snapshot_id=snapshot.id,
                migration=migration,
                warnings=tuple(warnings),
            )

    def _rollback_and_report(
        self,
        adapter: PlatformAdapter,
        snapshot: Snapshot,
        source_external_id: str | None,
        run: _Run,
        job_id: str,
        scope: str,
        account_id: str,
        *,
        failed_step: str,
        reason: str,
    ) -> SwitchReport:
        run.add(failed_step, False, reason)
        # 回滚同样要写登录态文件，客户端在运行会锁文件导致回滚也失败。
        # 先退出客户端再还原，避免「回滚失败、请手动恢复」的二次恐慌。
        if adapter.is_running():
            adapter.terminate()
            adapter.wait_exit(timeout=5.0)
        try:
            adapter.restore_from(snapshot.directory)
        except Exception as exc:  # noqa: BLE001 - 回滚失败必须显式上报
            run.add("回滚", False, f"回滚失败：{exc}")
            self._finish_job(job_id, "rollback_failed", failed_step, f"{reason}；回滚失败：{exc}")
            return SwitchReport(
                state="rollback_failed",
                dry_run=False,
                scope=scope,
                account_id=account_id,
                steps=tuple(run.steps),
                snapshot_id=snapshot.id,
                error=(
                    f"{failed_step}失败（{reason}），且自动回滚未成功。"
                    f"请用备份点 {snapshot.id} 手动恢复：{snapshot.directory}"
                ),
                rollback_failed=True,
            )

        restored = source_external_id is None or adapter.verify_switch(source_external_id)
        detail = "已还原到切换前状态" if restored else "已还原，但未能确认账号身份"
        run.add("回滚", restored, detail)
        state = "rolled_back" if restored else "rollback_unverified"
        self._finish_job(job_id, state, failed_step, reason)
        return SwitchReport(
            state=state,
            dry_run=False,
            scope=scope,
            account_id=account_id,
            steps=tuple(run.steps),
            snapshot_id=snapshot.id,
            error=f"{failed_step}失败（{reason}），已自动回滚，原账号未受影响",
        )

    # ---------- 内部 ----------

    def _discard_temporary_snapshot(self, snapshot: Snapshot) -> None:
        """删除切换前/恢复前的临时快照（目录 + 台账记录）。

        只在流程成功后调用：回滚路径仍需该快照的 directory，故不能提前删。
        删除失败（文件被锁等）只记日志，不影响已成功的切换结果。
        """
        try:
            self._backups.delete(snapshot.id)
        except Exception as exc:  # noqa: BLE001 - 临时备份清理失败不阻断切换
            import logging

            logging.getLogger(__name__).warning(
                "临时备份清理失败（%s）：%s", snapshot.id, exc
            )

    def _load_target(self, scope: str, account_id: str):
        account = self._repo.get(account_id)
        if account.scope != scope:
            raise SwitchError("目标账号不属于该作用域")
        if account.is_deleted:
            raise SwitchError("目标账号在回收站中，请先恢复")
        if not self._repo.has_credential(account_id):
            raise SwitchError("目标账号没有可用凭据，请重新导入")
        return account

    def _current_external_id(self, adapter: PlatformAdapter) -> str | None:
        try:
            return adapter.import_current().external_id
        except AdapterError:
            return None

    def _migrate_data(
        self,
        run: _Run,
        source_uid: str,
        target_uid: str,
        *,
        memory: bool,
        connectors: bool,
        sessions: bool,
        profile: bool,
        workspaces: bool,
    ) -> dict[str, Any] | None:
        """切换成功后，把源账号的记忆/连接器/会话/档案/工作区迁移到目标账号。

        迁移是**尽力而为**的增强：失败不阻断切换（切换已成功），但失败要显式报告。
        返回迁移报告（供前端展示），无内容迁移或失败时返回 None / 含 error 的报告。
        """
        try:
            from chaierww.domain.migration import (
                MigrationError,
                MigrationSelection,
                migrate_account_data,
            )

            report = migrate_account_data(
                source_uid,
                target_uid,
                selection=MigrationSelection(
                    memory=memory,
                    connectors=connectors,
                    sessions=sessions,
                    profile=profile,
                    workspaces=workspaces,
                ),
            )
            payload = report.payload()
            if report.changed:
                run.add("迁移数据", True, "记忆 / 连接器 / 会话 / 档案 / 工作区已合并到目标账号")
            else:
                run.add("迁移数据", True, "源账号无可迁移的数据")
            return payload
        except MigrationError as exc:
            run.add("迁移数据", False, str(exc))
            return {"sourceUid": source_uid, "targetUid": target_uid, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - 迁移失败绝不阻断已成功的切换
            detail = f"{type(exc).__name__}: {exc}"
            run.add("迁移数据", False, f"迁移失败：{detail}")
            return {
                "sourceUid": source_uid,
                "targetUid": target_uid,
                "error": detail,
            }

    def _record_job(
        self,
        job_id: str,
        scope: str,
        source_id: str | None,
        target_id: str,
        snapshot_id: str | None,
        state: str,
        dry_run: bool,
        started: str,
    ) -> None:
        with transaction(self._conn):
            self._conn.execute(
                """
                INSERT INTO switch_jobs (
                    id, scope, source_account_id, target_account_id, snapshot_id,
                    state, dry_run, started_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (job_id, scope, source_id, target_id, snapshot_id, state, int(dry_run), started),
            )

    def _finish_job(
        self,
        job_id: str,
        state: str,
        failed_step: str | None,
        error: str | None,
        snapshot_id: str | None = None,
    ) -> None:
        with transaction(self._conn):
            self._conn.execute(
                """
                UPDATE switch_jobs
                   SET state = ?, failed_step = ?, error = ?,
                       snapshot_id = COALESCE(?, snapshot_id), finished_at = ?
                 WHERE id = ?
                """,
                (state, failed_step, error, snapshot_id, now_iso(), job_id),
            )

    def history(self, scope: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM switch_jobs WHERE scope = ? ORDER BY started_at DESC LIMIT ?",
            (scope, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def prune_snapshots(self, scope: str) -> int:
        return self._backups.prune(scope, self._keep)


def build_adapters() -> dict[str, PlatformAdapter]:
    from chaierww.adapters.trae import TraeAdapter
    from chaierww.adapters.workbuddy import WorkBuddyAdapter

    workbuddy = WorkBuddyAdapter()
    trae = TraeAdapter()
    return {workbuddy.scope: workbuddy, trae.scope: trae}
