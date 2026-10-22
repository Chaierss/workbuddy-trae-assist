from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from chaierww.adapters.base import AdapterError, NotInstalled, NotLoggedIn, StateUnreadable
from chaierww.domain.accounts.models import Account, CredentialState
from chaierww.domain.accounts.repository import AccountNotFound
from chaierww.domain.accounts.service import AccountBusyError, AccountInUseError, AccountService
from chaierww.domain.activities import ACTIVITY_CATALOG, ActivityBusyError, summarize
from chaierww.domain.backup import (
    CREDENTIAL_PARTS,
    DEFAULT_GLOBAL_PARTS,
    GLOBAL_PART_LABELS,
    GLOBAL_PARTS,
    GLOBAL_SCOPE,
    BackupError,
    Snapshot,
    SnapshotNotFound,
)
from chaierww.domain.client_settings import ClientSettingsError
from chaierww.domain.rotation import RotationError
from chaierww.domain.scheduler import SchedulerError
from chaierww.domain.scopes import (
    ScopeNotImplemented,
    UnknownScope,
    all_scopes,
    get_scope,
    require_implemented,
)
from chaierww.domain.session_migration import SessionMigrationError
from chaierww.domain.sessions import (
    conversation_owner,
    conversations_for_user,
    read_conversation_messages,
)
from chaierww.domain.switching import (
    SwitchEngine,
    SwitchError,
    encode_payload,
    encode_token_payload,
)
from chaierww.domain.usage import summarize as summarize_usage
from chaierww.domain.usage_turns import read_turn, read_turns_brief
from chaierww.infra.db import now_iso

logger = logging.getLogger("chaierww")
router = APIRouter(prefix="/api")


# ---------- 通用 ----------


def _service(request: Request) -> AccountService:
    return request.app.state.accounts


def _engine(request: Request) -> SwitchEngine:
    return request.app.state.engine


def _account_payload(account: Account) -> dict[str, Any]:
    """对外只暴露界面需要的字段，凭据永远不出现在响应里。"""
    return {
        "id": account.id,
        "scope": account.scope,
        "platform": _platform_of(account.scope),
        "externalId": account.external_id,
        "displayName": account.display_name,
        "note": account.note,
        "avatarPath": account.avatar_path,
        "source": account.source,
        "credentialState": account.credential_state.value,
        "lastUsedAt": account.last_used_at,
        "createdAt": account.created_at,
        "updatedAt": account.updated_at,
        "deletedAt": account.deleted_at,
    }


def _platform_of(scope: str) -> str:
    """scope → 平台名，用于合并号池的平台徽标。未知 scope 退回原文。"""
    try:
        return get_scope(scope).platform
    except UnknownScope:
        return scope


def _snapshot_payload(snapshot: Snapshot) -> dict[str, Any]:
    return {
        "id": snapshot.id,
        "scope": snapshot.scope,
        "createdAt": snapshot.created_at,
        "reason": snapshot.reason,
        "accountId": snapshot.account_id,
        "totalBytes": snapshot.total_bytes,
        "fileCount": snapshot.file_count,
        "verified": snapshot.verified,
        "note": snapshot.note,
    }


def _handle(exc: Exception) -> HTTPException:
    if isinstance(exc, AccountNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (AccountBusyError, AccountInUseError, ActivityBusyError)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, (NotInstalled, NotLoggedIn, StateUnreadable)):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, SnapshotNotFound):
        return HTTPException(status_code=404, detail="备份点不存在")
    if isinstance(exc, BackupError):
        return HTTPException(status_code=500, detail=str(exc))
    if isinstance(exc, SessionMigrationError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, ScopeNotImplemented):
        return HTTPException(status_code=501, detail=str(exc))
    if isinstance(exc, (UnknownScope, ValueError, AdapterError, SwitchError)):
        return HTTPException(status_code=400, detail=str(exc))
    logger.exception("未预期的接口错误：%s", type(exc).__name__)
    return HTTPException(status_code=500, detail="内部错误")


# ---------- 请求体 ----------


class ImportRequest(BaseModel):
    scope: str | None = None


class JsonImportRequest(BaseModel):
    content: str = Field(min_length=2, max_length=1_000_000)


#: 账号导出包的格式标识与版本。导入时严格比对，避免把别家 JSON 当成本工具导出。
ACCOUNT_BUNDLE_FORMAT = "chaierww.accounts"
ACCOUNT_BUNDLE_VERSION = 1


class AccountExportRequest(BaseModel):
    """导出选中账号。空列表直接 400，绝不默认导出整个号池。"""

    accountIds: list[str] = Field(default_factory=list)


class AccountBundleRequest(BaseModel):
    """导入导出包。上限 10 MB：凭据带整份状态文件时单账号就可能几十 KB。"""

    content: str = Field(min_length=2, max_length=10_000_000)


class SwitchRequest(BaseModel):
    scope: str | None = None
    accountId: str
    relaunch: bool = False
    #: 程序位（多宿主客户端才有）。不传 = 该平台的默认位；只读 / 未适配的位会被拒。
    slot: str | None = None


class SnapshotRequest(BaseModel):
    scope: str
    note: str = ""


class GlobalSnapshotRequest(BaseModel):
    """全局备份的分区勾选。省略 parts = 默认分区（不含凭据）。"""

    parts: list[str] = Field(default_factory=list)
    note: str = ""
    #: 勾选含凭据的分区时必须置 true —— 界面上的「二次确认」就映射到这个字段。
    confirmCredentials: bool = False


class NoteRequest(BaseModel):
    note: str = Field(default="", max_length=200)


class PreferencesRequest(BaseModel):
    migrateMemory: bool | None = None
    migrateConnectors: bool | None = None
    migrateSessions: bool | None = None
    migrateProfile: bool | None = None
    migrateWorkspaces: bool | None = None
    autoBackup: bool | None = None
    autoGlobalBackup: bool | None = None


class ActivityRunRequest(BaseModel):
    """一键执行的账号范围；省略 = 号池里全部可检测账号。"""

    accountIds: list[str] | None = None


class ScheduleUpdateRequest(BaseModel):
    """计划任务的部分更新；省略的字段保持原值。"""

    enabled: bool | None = None
    times: list[str] | None = None
    jitterMinutes: int | None = None
    accountIds: list[str] | None = None


class ClientSettingsUpdateRequest(BaseModel):
    """客户端设置写入：`updates` 只允许登记的键，`confirmFingerprint` 是读取时的指纹。"""

    updates: dict[str, Any] = Field(default_factory=dict)
    confirmFingerprint: str = ""


class ClientSettingsRestoreRequest(BaseModel):
    backupId: str = Field(min_length=1)


class RotationUpdateRequest(BaseModel):
    """自动轮换策略的部分更新；省略的字段保持原值。"""

    enabled: bool | None = None
    minUrgencyHours: float | None = Field(default=None, ge=0)
    cooldownMinutes: int | None = Field(default=None, ge=0)
    activeGuardMinutes: int | None = Field(default=None, ge=0)
    minRemainingCredits: float | None = Field(default=None, ge=0)
    minGapHours: float | None = Field(default=None, ge=0)


# ---------- 基础 ----------


@router.get("/health", summary="服务健康检查", tags=["基础"])
def health(request: Request) -> dict[str, Any]:
    return {
        "status": "ok",
        "secrets": request.app.state.box.describe(),
        "dataDir": str(request.app.state.settings.home),
    }


@router.get("/scopes", summary="列出所有作用域及其实现状态", tags=["基础"])
def list_scopes() -> dict[str, Any]:
    return {
        "scopes": [
            {
                "key": scope.key,
                "platform": scope.platform,
                "region": scope.region,
                "label": scope.label,
                "implemented": scope.implemented,
            }
            for scope in all_scopes()
        ]
    }


@router.get("/detect", summary="检测客户端安装、运行与登录状态", tags=["基础"])
def detect(request: Request, scope: str) -> dict[str, Any]:
    try:
        adapter = request.app.state.adapters.get(scope)
        if adapter is None:
            raise ScopeNotImplemented(f"作用域 {scope} 尚未实现")
        result = adapter.detect()
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {
        "scope": scope,
        "installed": result.installed,
        "running": result.running,
        "loggedIn": result.logged_in,
        "stateDir": str(result.state_dir) if result.state_dir else None,
        "appPath": str(result.app_path) if result.app_path else None,
        "message": result.message,
        "currentAccountId": result.current_external_id,
        "slots": _slot_items(adapter),
    }


def _slot_items(adapter: Any) -> list[dict[str, Any]]:
    """程序位清单：只读 / 未适配的位置也在这里如实报出（含原因），由界面灰掉。"""
    try:
        return [item.payload() for item in adapter.slots()]
    except Exception as exc:  # noqa: BLE001 - 单平台位探测失败不该拖垮整页
        logger.warning("程序位探测失败：%s", exc)
        return []


@router.get("/detection", summary="合并检测所有已实现客户端，含引导下载链接", tags=["基础"])
def detect_all(request: Request) -> dict[str, Any]:
    """一次返回所有已实现平台的安装/运行/登录状态 + 引导下载链接。

    号池页顶部的安装检测状态条依赖这里：不区分平台，逐项列出，
    未安装的附下载链接（含邀请码），已安装未登录的提示登录。
    """
    return {"clients": _detection_items(request)}


def _detection_items(request: Request) -> list[dict[str, Any]]:
    """合并检测的实现体；``GET /detection`` 与概览待处理清单共用，避免两处逻辑漂移。"""
    from chaierww.config import DOWNLOAD_LINKS

    adapters = request.app.state.adapters
    items: list[dict[str, Any]] = []
    for key, adapter in adapters.items():
        scope = get_scope(key)
        try:
            result = adapter.detect()
            installed = result.installed
            running = result.running
            logged_in = result.logged_in
            message = result.message
            app_path = str(result.app_path) if result.app_path else None
            state_dir = str(result.state_dir) if result.state_dir else None
            current = result.current_external_id
        except Exception as exc:  # noqa: BLE001 - 单个平台失败不拖垮整页
            installed = False
            running = False
            logged_in = False
            message = str(exc)
            app_path = None
            state_dir = None
            current = None

        items.append(
            {
                "scope": key,
                "platform": scope.platform,
                "label": scope.label,
                "installed": installed,
                "running": running,
                "loggedIn": logged_in,
                "stateDir": state_dir,
                "appPath": app_path,
                "message": message,
                "currentAccountId": current,
                "downloadUrl": DOWNLOAD_LINKS.get(scope.platform),
                "slots": _slot_items(adapter),
            }
        )

    return items


# ---------- 扫码登录（OAuth 采集） ----------


@router.post("/oauth/start", summary="发起 WorkBuddy 扫码登录", tags=["号池"])
def oauth_start(request: Request) -> dict[str, Any]:
    """申请 state + 授权页地址，前端打开后用户扫码，再轮询 /oauth/status 拿结果。"""
    try:
        return request.app.state.oauth.start()
    except Exception as exc:  # noqa: BLE001
        logger.exception("扫码登录发起失败")
        raise HTTPException(status_code=502, detail=f"发起扫码登录失败：{exc}") from exc


@router.get("/oauth/status", summary="轮询扫码登录结果并入库", tags=["号池"])
def oauth_status(request: Request, loginId: str) -> dict[str, Any]:
    """轮询一次。成功时把 token 账号入库（scope=workbuddy-cn，凭据为 oauth-token 形态）。"""
    try:
        result = request.app.state.oauth.poll(loginId)
    except Exception as exc:  # noqa: BLE001
        logger.exception("轮询扫码登录结果失败")
        raise HTTPException(status_code=502, detail=f"轮询扫码登录失败：{exc}") from exc

    if result.get("done") and result.get("result"):
        account_data = result["result"]
        uid = str(account_data.get("uid") or "")
        if not uid:
            return {"done": True, "error": "扫码结果缺少账号标识"}
        display_name = str(
            account_data.get("nickname")
            or account_data.get("email")
            or f"用户 {uid[-8:]}"
        )
        upserted = request.app.state.repository.upsert(
            scope="workbuddy-cn",
            external_id=uid,
            display_name=display_name,
            credential=encode_token_payload(account_data),
            credential_kind="oauth_token",
            source="oauth_login",
        )
        return {
            "done": True,
            "result": {
                "created": upserted.created,
                "restored": upserted.restored,
                "account": _account_payload(upserted.account),
            },
        }
    return result


# ---------- Trae 扫码登录（OAuth 采集） ----------


@router.post("/trae/oauth/start", summary="发起 Trae 扫码登录", tags=["号池"])
def trae_oauth_start(request: Request) -> dict[str, Any]:
    """读取 Trae storage.json 的设备身份，占端口起监听，返回授权页地址。"""
    try:
        adapter = request.app.state.adapters.get("trae-cn")
        if adapter is None:
            raise ScopeNotImplemented("作用域 trae-cn 尚未实现")
        storage = adapter.read_storage_json()
        return request.app.state.trae_oauth.start(storage)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


@router.get("/trae/oauth/status", summary="轮询 Trae 扫码登录结果并入库", tags=["号池"])
def trae_oauth_status(request: Request, loginId: str) -> dict[str, Any]:
    """轮询一次。成功时把 JWT 账号入库（scope=trae-cn，凭据为 oauth-token 形态）。"""
    try:
        result = request.app.state.trae_oauth.poll(loginId)
    except Exception as exc:  # noqa: BLE001
        logger.exception("轮询 Trae 扫码登录结果失败")
        raise HTTPException(status_code=502, detail=f"轮询 Trae 扫码登录失败：{exc}") from exc

    if result.get("done") and result.get("result"):
        account_data = result["result"]
        uid = str(account_data.get("uid") or "")
        if not uid:
            return {"done": True, "error": "扫码结果缺少账号标识"}
        display_name = str(account_data.get("displayName") or f"账号{uid[-6:]}")
        upserted = request.app.state.repository.upsert(
            scope="trae-cn",
            external_id=uid,
            display_name=display_name,
            credential=encode_token_payload(account_data),
            credential_kind="oauth_token",
            source="oauth_login",
        )
        return {
            "done": True,
            "result": {
                "created": upserted.created,
                "restored": upserted.restored,
                "account": _account_payload(upserted.account),
            },
        }
    return result


@router.post("/trae/oauth/cancel", summary="取消 Trae 扫码登录", tags=["号池"])
def trae_oauth_cancel(request: Request, loginId: str) -> dict[str, Any]:
    try:
        cancelled = request.app.state.trae_oauth.cancel(loginId)
    except Exception as exc:  # noqa: BLE001
        logger.exception("取消 Trae 扫码登录失败")
        raise HTTPException(status_code=502, detail=f"取消 Trae 扫码登录失败：{exc}") from exc
    return {"ok": cancelled}


# ---------- 账号 ----------


@router.get("/accounts", summary="列出账号（不含回收站）", tags=["账号"])
def list_accounts(request: Request, scope: str, q: str | None = None) -> dict[str, Any]:
    try:
        accounts = _service(request).list_active(scope, query=q)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {"accounts": [_account_payload(a) for a in accounts]}


@router.get("/accounts/merged", summary="合并号池：多个作用域的账号合成一张表", tags=["账号"])
def list_merged(request: Request, q: str | None = None) -> dict[str, Any]:
    try:
        implemented = [scope.key for scope in all_scopes() if scope.implemented]
        accounts = _service(request).list_merged(implemented, query=q)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {"accounts": [_account_payload(a) for a in accounts]}


@router.post("/accounts/import", summary="导入客户端当前登录的账号", tags=["账号"])
def import_account(request: Request, body: ImportRequest) -> dict[str, Any]:
    try:
        scope = _import_scope(request, body.scope)
        adapter = request.app.state.adapters.get(scope)
        if adapter is None:
            raise ScopeNotImplemented(f"作用域 {scope} 尚未实现")

        imported = adapter.import_current()
        result = request.app.state.repository.upsert(
            scope=scope,
            external_id=imported.external_id,
            display_name=imported.display_name,
            credential=encode_payload(imported.payload),
            source="local_import",
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc

    return {
        "created": result.created,
        "restored": result.restored,
        "account": _account_payload(result.account),
    }


def _import_scope(request: Request, scope: str | None) -> str:
    """导入账号的作用域：显式传入优先，否则自动探测「哪个已实现客户端已登录」。

    统一号池下前端不关心平台，点「导入」就是导入当前登录的那个号。
    有多个客户端都登录时，取已实现的第一个；都不登录则报错，不静默猜。
    """
    if scope:
        return scope
    for key, adapter in request.app.state.adapters.items():
        if adapter.detect().logged_in:
            return key
    raise NotLoggedIn("没有检测到已登录的客户端，请先在客户端登录后再导入")


@router.post("/accounts/import-json", summary="粘贴 JSON 导入账号（自动识别平台）", tags=["账号"])
def import_json(request: Request, body: JsonImportRequest) -> dict[str, Any]:
    """把登录态 JSON 文本粘贴进号池。

    内容依次交给各平台适配器试解析，第一个认领成功的决定作用域 ——
    前端不需要选平台。解析失败（谁都不认领）返回 400。
    """
    raw = body.content.strip().encode("utf-8")
    try:
        for key, adapter in request.app.state.adapters.items():
            import_json_fn = getattr(adapter, "import_json", None)
            if import_json_fn is None:
                continue
            try:
                imported = import_json_fn(raw)
            except AdapterError:
                continue
            result = request.app.state.repository.upsert(
                scope=key,
                external_id=imported.external_id,
                display_name=imported.display_name,
                credential=encode_payload(imported.payload),
                source="json_import",
            )
            break
        else:
            raise ValueError(
                "无法识别内容：不是任何已支持客户端的登录态 JSON"
            )
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc

    return {
        "created": result.created,
        "restored": result.restored,
        "account": _account_payload(result.account),
    }


@router.post(
    "/accounts/export",
    summary="导出选中账号（含登录凭据，可再用 JSON 导入恢复）",
    tags=["账号"],
)
def export_accounts(request: Request, body: AccountExportRequest) -> dict[str, Any]:
    """把选中账号连同**解密后的**登录凭据打包成 JSON 导出包。

    导出物等价于这些账号的登录态，拿到就能直接顶替登录；界面上必须提示妥善保管。
    只导出显式选中的账号，空选择直接 400 —— 不做「默认全量」这种危险兜底。
    """
    account_ids = list(dict.fromkeys(body.accountIds))
    if not account_ids:
        raise HTTPException(status_code=400, detail="请先选择要导出的账号")

    repository = request.app.state.repository
    exported: list[dict[str, Any]] = []
    skipped: list[str] = []
    for account_id in account_ids:
        try:
            account = repository.get(account_id)
            if account.is_deleted:
                skipped.append(account_id)
                continue
            kind, credential = repository.credential_record(account_id)
        except (AccountNotFound, ValueError):
            # 没有凭据的账号无法导入后直接使用，跳过并如实告知数量。
            skipped.append(account_id)
            continue
        exported.append(
            {
                "scope": account.scope,
                "externalId": account.external_id,
                "displayName": account.display_name,
                "note": account.note,
                "credentialKind": kind,
                "credential": credential,
            }
        )

    if not exported:
        raise HTTPException(status_code=400, detail="选中的账号都没有可导出的登录凭据")

    return {
        "format": ACCOUNT_BUNDLE_FORMAT,
        "version": ACCOUNT_BUNDLE_VERSION,
        "exportedAt": now_iso(),
        "accounts": exported,
        "skipped": len(skipped),
    }


@router.post(
    "/accounts/import-bundle",
    summary="导入账号导出包（含登录凭据，恢复为未验证状态）",
    tags=["账号"],
)
def import_account_bundle(request: Request, body: AccountBundleRequest) -> dict[str, Any]:
    """把 `/accounts/export` 的产物导回号池。

    逐条独立处理：某条缺字段或作用域不认识只跳过那一条并记下原因，
    不让一个坏条目把整包都废掉。导入后凭据状态一律回到「未验证」，
    由用户自己切换一次确认真实可用。
    """
    try:
        data = json.loads(body.content)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"不是有效的 JSON：{exc.msg}") from exc
    if not isinstance(data, dict) or data.get("format") != ACCOUNT_BUNDLE_FORMAT:
        raise HTTPException(status_code=400, detail="不是本工具导出的账号文件")
    if data.get("version") != ACCOUNT_BUNDLE_VERSION:
        raise HTTPException(status_code=400, detail="账号文件版本不受支持，请用当前版本重新导出")
    items = data.get("accounts")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="账号文件里没有可导入的账号")

    repository = request.app.state.repository
    imported = created = restored = 0
    errors: list[str] = []
    for index, item in enumerate(items, start=1):
        try:
            if not isinstance(item, dict):
                raise ValueError("条目不是对象")
            scope = require_implemented(str(item.get("scope") or "")).key
            external_id = str(item.get("externalId") or "").strip()
            display_name = str(item.get("displayName") or "").strip()
            credential = item.get("credential")
            if not external_id or not display_name:
                raise ValueError("缺少账号标识或显示名")
            if not isinstance(credential, dict) or not credential:
                raise ValueError("缺少登录凭据")
            result = repository.upsert(
                scope=scope,
                external_id=external_id,
                display_name=display_name,
                credential=credential,
                credential_kind=str(item.get("credentialKind") or "session"),
                source="json_bundle",
            )
            note = str(item.get("note") or "").strip()
            if note and note != result.account.note:
                repository.update_note(result.account.id, note)
            imported += 1
            created += int(result.created)
            restored += int(result.restored)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"第 {index} 条：{exc}")

    if not imported:
        raise HTTPException(status_code=400, detail=errors[0] if errors else "没有账号被导入")
    return {
        "total": len(items),
        "imported": imported,
        "created": created,
        "restored": restored,
        "failed": len(errors),
        "errors": errors,
    }


@router.get("/accounts/credits", summary="查询各账号积分余额与到期时间", tags=["账号"])
def account_credits(
    request: Request, refresh: bool = False, accountIds: str | None = None
) -> dict[str, Any]:
    """一次返回合并号池里所有账号的积分摘要（可用 accountIds 限定范围）。

    ``available=False`` 表示该账号凭据形态查不了（不是 0 积分）——
    前端必须按「未知」渲染，不能显示 0。
    """
    try:
        implemented = [scope.key for scope in all_scopes() if scope.implemented]
        accounts = _service(request).list_merged(implemented)
        if accountIds:
            wanted = {item.strip() for item in accountIds.split(",") if item.strip()}
            accounts = [a for a in accounts if a.id in wanted]
        infos = request.app.state.credits.query_many(accounts, force=refresh)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {"credits": {account_id: info.payload() for account_id, info in infos.items()}}


@router.get("/activities/catalog", summary="活动目录（静态清单，零网络）", tags=["活动"])
def list_activity_catalog() -> dict[str, Any]:
    """返回本工具**认识**的全部活动项，不访问上游、不读账号。

    页面先用它渲染骨架卡片（状态「未检测」），随后被真实检测结果按 key 覆盖，
    这样冷启动时不会先看到一片空白。``key`` 与检测结果的 key 一一对应。
    """
    return {"entries": [dict(entry) for entry in ACTIVITY_CATALOG]}


@router.get("/activities", summary="检测各账号的活动状态", tags=["活动"])
def list_activities(
    request: Request, refresh: bool = False, accountIds: str | None = None
) -> dict[str, Any]:
    """一次返回号池里所有账号的活动状态（可用 accountIds 限定范围）。

    ``detectable=False`` 表示这个账号的凭据形态查不了活动（不是「没有活动」）——
    前端要按「不可查」渲染并给出原因，不能显示成「全部完成」。
    """
    try:
        accounts = _activities_accounts(request, accountIds)
        detected = request.app.state.activities.detect(accounts, force=refresh)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {
        "accounts": [item.payload() for item in detected],
        "summary": summarize(detected),
    }


@router.post("/activities/run", summary="一键执行可自动完成的活动", tags=["活动"])
def run_activities(
    request: Request, payload: ActivityRunRequest | None = None
) -> dict[str, Any]:
    """执行每个账号上「可自动 + 未完成」的活动；已完成的自动跳过并说明原因。

    每日签到、以及**有动作表覆盖**的成长任务会被代做（纯 API 上报 + 领奖）；
    expert/skill/小程序族等需要真实客户端操作的任务仍标「需手动」，不在范围。
    """
    try:
        accounts = _activities_accounts(request, None)
        if payload and payload.accountIds:
            wanted = set(payload.accountIds)
            accounts = [item for item in accounts if item.id in wanted]
        report = request.app.state.activities.run(accounts)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    summary = report.get("summary", {})
    logger.info(
        "一键执行活动：账号 %s 个，成功 %s，跳过 %s，失败 %s",
        len(accounts),
        summary.get("ok", 0),
        summary.get("skipped", 0),
        summary.get("failed", 0),
    )
    return report


def _activities_accounts(request: Request, accountIds: str | None) -> list[Account]:
    implemented = [scope.key for scope in all_scopes() if scope.implemented]
    accounts = _service(request).list_merged(implemented)
    if accountIds:
        wanted = {item.strip() for item in accountIds.split(",") if item.strip()}
        accounts = [item for item in accounts if item.id in wanted]
    return accounts


@router.get("/usage", summary="各账号请求与积分消耗的合并视图", tags=["用量"])
def list_usage(
    request: Request, refresh: bool = False, accountIds: str | None = None
) -> dict[str, Any]:
    """一次返回号池里所有账号的用量（可用 accountIds 限定范围）。

    ``detectable=False`` 表示这个账号查不了用量（不是「没用过」）。
    请求侧只有 WorkBuddy 有逐请求明细；Trae 只有积分消耗摘要。
    前端必须如实标注这两点。
    """
    try:
        accounts = _activities_accounts(request, accountIds)
        detected = request.app.state.usage.query_many(accounts, force=refresh)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {
        "accounts": [item.payload() for item in detected],
        "summary": summarize_usage(detected),
    }


@router.get(
    "/usage/requests/{request_id}",
    summary="某次计费请求在本机的对话正文",
    tags=["用量"],
)
def usage_request_turn(request: Request, request_id: str, accountId: str) -> dict[str, Any]:
    """把一条云端计费明细还原成「当时聊了什么」。

    依据：云端 ``requestId`` == 本机 jsonl 的
    ``providerData.conversationRequestId``。归属校验与会话正文一致 —— 只允许读
    该账号自己名下的会话；找不到对应对话时 ``available=False`` + 原因，不编造。
    """
    try:
        account = _service(request).by_id(accountId)
        _require_session_platform(account)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc

    meta = conversations_for_user(account.external_id or "")
    turn = read_turn(request_id, allowed=set(meta))
    if turn.conversation_id is None or turn.conversation_id not in meta:
        return {
            "accountId": accountId,
            "requestId": request_id,
            "found": False,
            "conversationId": None,
            "title": "",
            "cwd": "",
            "messages": [],
            "truncated": False,
            "error": turn.error or "这条请求不属于该账号，或本机没有对应会话",
        }
    title, cwd = meta[turn.conversation_id]
    return {
        "accountId": accountId,
        "requestId": request_id,
        "found": turn.available,
        "conversationId": turn.conversation_id,
        "title": title,
        "cwd": cwd,
        "messages": list(turn.messages),
        "truncated": turn.truncated,
        "error": turn.error,
    }


@router.get("/usage/turns", summary="批量取多条计费请求在本机的对话摘要", tags=["用量"])
def usage_turns_brief(request: Request, accountId: str, ids: str = "") -> dict[str, Any]:
    """明细表一次拉一页的对话摘要（首条提问 + 末条回答）。

    单独做一个批量口而不是逐行点开，是为了让明细表直接看到「聊了什么」；
    正文仍走 ``/usage/requests/{id}``，这里只给截断后的摘要。
    """
    try:
        account = _service(request).by_id(accountId)
        _require_session_platform(account)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc

    wanted = [item.strip() for item in ids.split(",") if item.strip()]
    meta = conversations_for_user(account.external_id or "")
    allowed = set(meta)
    briefs = read_turns_brief(wanted, allowed=allowed)
    return {
        "accountId": accountId,
        "turns": {
            key: {**value, "title": meta.get(value["conversationId"] or "", ("", ""))[0]}
            for key, value in briefs.items()
            if value["conversationId"] in allowed
        },
    }


@router.get(
    "/sessions/{account_id}/{conversation_id}/messages",
    summary="读一条会话的完整对话正文",
    tags=["会话"],
)
def conversation_messages(
    request: Request, account_id: str, conversation_id: str
) -> dict[str, Any]:
    """读某账号下某条会话的对话正文（user / assistant 气泡）。

    归属校验：会话必须属于该账号（`sessions.user_id` == 账号 `externalId`），
    否则 404 —— 避免凭会话 id 越权读到别的账号的聊天记录。正文文件缺失时返回空
    列表（「没有聊天记录」），**不是**错误。
    """
    try:
        account = _service(request).by_id(account_id)
        _require_session_platform(account)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    owner = conversation_owner(conversation_id)
    if owner is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    if owner != (account.external_id or ""):
        raise HTTPException(status_code=404, detail="该会话不属于此账号")
    return {
        "accountId": account_id,
        "conversationId": conversation_id,
        "messages": read_conversation_messages(conversation_id),
    }


def _require_session_platform(account: Account) -> None:
    """会话数据是 WorkBuddy 专有；Trae 账号明确拒绝，而不是给出空的会话结果。"""
    platform = _platform_of(account.scope)
    if platform != "workbuddy":
        raise SessionMigrationError(
            f"{account.display_name} 是 {platform} 账号，该平台不提供会话数据"
        )


@router.get("/accounts/stats", summary="按凭据状态统计账号数量", tags=["账号"])
def account_stats(request: Request, scope: str) -> dict[str, Any]:
    try:
        summary = _service(request).summary(scope)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {
        "scope": summary.scope,
        "total": summary.total,
        "usable": summary.usable,
        "reloginRequired": summary.relogin_required,
        "unknown": summary.unknown,
    }


@router.get("/accounts/recycle-bin", summary="列出回收站中的账号", tags=["账号"])
def recycle_bin(request: Request, scope: str) -> dict[str, Any]:
    try:
        accounts = _service(request).list_recycle_bin(scope)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {"accounts": [_account_payload(a) for a in accounts if a.is_deleted]}


@router.patch("/accounts/{account_id}/note", summary="修改账号备注", tags=["账号"])
def update_note(request: Request, account_id: str, body: NoteRequest) -> dict[str, Any]:
    try:
        account = _service(request).set_note(account_id, body.note)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return _account_payload(account)


@router.delete("/accounts/{account_id}", summary="把账号移入回收站（软删除）", tags=["账号"])
def delete_account(request: Request, account_id: str) -> dict[str, Any]:
    try:
        _service(request).delete(account_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {"ok": True}


@router.post("/accounts/{account_id}/restore", summary="从回收站恢复账号", tags=["账号"])
def restore_account(request: Request, account_id: str) -> dict[str, Any]:
    try:
        account = _service(request).restore(account_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return _account_payload(account)


def _resolve_scope(request: Request, body: SwitchRequest) -> str:
    """切换/预演的作用域：显式传入优先，否则从账号 ID 反查。

    合并号池下前端不传平台，靠这里自动识别该账号属于哪个平台。
    """
    if body.scope:
        return body.scope
    return _engine(request).scope_of(body.accountId)


# ---------- 切换 ----------


@router.post("/switch/dry-run", summary="预演切换：只返回计划，不写入任何文件", tags=["切换"])
def switch_dry_run(request: Request, body: SwitchRequest) -> dict[str, Any]:
    try:
        scope = _resolve_scope(request, body)
        report = _engine(request).dry_run(scope, body.accountId, slot=body.slot)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return _report_payload(report)


@router.post("/switch", summary="执行切换：确认退出、备份、原子写入、校验", tags=["切换"])
def switch(request: Request, body: SwitchRequest) -> dict[str, Any]:
    try:
        scope = _resolve_scope(request, body)
        # 迁移范围来自「设置 → 常规」的开关，切换时由后端读偏好决定复制哪几块数据。
        prefs = request.app.state.preferences.load()
        report = _engine(request).execute(
            scope,
            body.accountId,
            relaunch=body.relaunch,
            slot=body.slot,
            migrate_memory=prefs.migrate_memory,
            migrate_connectors=prefs.migrate_connectors,
            migrate_sessions=prefs.migrate_sessions,
            migrate_profile=prefs.migrate_profile,
            migrate_workspaces=prefs.migrate_workspaces,
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return _report_payload(report)


@router.get("/switch/history", summary="切换任务历史", tags=["切换"])
def switch_history(request: Request, scope: str, limit: int = 20) -> dict[str, Any]:
    try:
        items = _engine(request).history(scope, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {"jobs": items}


@router.get("/migration/preview", summary="切换前预检：只读推演将迁移的数据", tags=["切换"])
def migration_preview(
    request: Request,
    scope: str = "",
    accountId: str = "",
    sourceUid: str = "",
) -> dict[str, Any]:
    """切换前的迁移预检（只读，不写任何文件 / 库 / 台账）。

    源账号 = 当前客户端登录账号（也可用 ``sourceUid`` 显式指定），目标 = ``accountId``；
    预检哪几块由「设置 → 常规」的迁移开关决定，与真实切换保持一致。
    """
    try:
        engine = _engine(request)
        resolved_scope = scope or engine.scope_of(accountId)
        prefs = request.app.state.preferences.load()
        from chaierww.domain.migration import MigrationSelection

        selection = MigrationSelection(
            memory=prefs.migrate_memory,
            connectors=prefs.migrate_connectors,
            sessions=prefs.migrate_sessions,
            profile=prefs.migrate_profile,
            workspaces=prefs.migrate_workspaces,
        )
        return engine.preview_migration(
            resolved_scope, accountId, selection=selection, source_uid=sourceUid
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc


def _report_payload(report: Any) -> dict[str, Any]:
    return {
        "state": report.state,
        "dryRun": report.dry_run,
        "succeeded": report.succeeded,
        "scope": report.scope,
        "accountId": report.account_id,
        "snapshotId": report.snapshot_id,
        "error": report.error,
        "rollbackFailed": report.rollback_failed,
        "migration": report.migration,
        "steps": [{"step": s.step, "ok": s.ok, "detail": s.detail} for s in report.steps],
        "plan": (
            {
                "requiresAppExit": report.plan.requires_app_exit,
                "warnings": list(report.plan.warnings),
                "files": [
                    {
                        "path": str(item.path),
                        "action": item.action,
                        "size": item.size,
                        "note": item.note,
                    }
                    for item in report.plan.files
                ],
            }
            if report.plan
            else None
        ),
    }


# ---------- 备份 ----------


@router.get("/backups", summary="列出备份点", tags=["备份"])
def list_backups(request: Request, scope: str, limit: int = 50) -> dict[str, Any]:
    try:
        items = request.app.state.backups.list(scope, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {"snapshots": [_snapshot_payload(s) for s in items]}


@router.post("/backups", summary="创建手动备份", tags=["备份"])
def create_backup(request: Request, body: SnapshotRequest) -> dict[str, Any]:
    try:
        adapter = request.app.state.adapters.get(body.scope)
        if adapter is None:
            raise ScopeNotImplemented(f"作用域 {body.scope} 尚未实现")
        snapshot = request.app.state.backups.create(
            scope=body.scope,
            source_dir=adapter.state_dir(),
            reason="manual",
            note=body.note,
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return _snapshot_payload(snapshot)


@router.post("/backups/{snapshot_id}/verify", summary="用 sha256 清单校验备份完整性", tags=["备份"])
def verify_backup(request: Request, snapshot_id: str) -> dict[str, Any]:
    try:
        ok = request.app.state.backups.verify(snapshot_id)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {"id": snapshot_id, "verified": ok}


@router.post(
    "/backups/{snapshot_id}/restore",
    summary="用快照恢复客户端登录态（恢复前自动再备份一次）",
    tags=["备份"],
)
def restore_backup(request: Request, snapshot_id: str) -> dict[str, Any]:
    """把某个客户端数据快照覆盖回客户端目录。

    顺序刻意如此：先校验 → 退出客户端 → 给当前状态再做一份备份 → 才真正覆盖。
    任何一步不成立都中止，绝不在「备份不可信」或「还原点缺失」的情况下动用户目录。
    """
    try:
        snapshot = request.app.state.backups.get(snapshot_id)
        if snapshot.scope == GLOBAL_SCOPE:
            raise ValueError("全局备份用于换机/导出，不支持原地恢复")
        adapter = request.app.state.adapters.get(snapshot.scope)
        restore_from = getattr(adapter, "restore_from", None)
        if adapter is None or restore_from is None:
            raise ScopeNotImplemented(f"作用域 {snapshot.scope} 尚未实现恢复")
        # 校验不过的快照不允许覆盖：损坏的备份比没有备份更危险。
        if not request.app.state.backups.verify(snapshot_id):
            raise BackupError("快照校验不通过，已中止恢复")
        if adapter.is_running():
            adapter.terminate()
            adapter.wait_exit()
            if adapter.is_running():
                raise BackupError("客户端仍在运行，请先退出客户端再恢复")
        # 恢复本身也要可逆：覆盖前把当前状态落一份「恢复前」快照。
        safety: Snapshot | None = None
        if adapter.state_dir().is_dir():
            safety = request.app.state.backups.create(
                scope=snapshot.scope,
                source_dir=adapter.state_dir(),
                reason="pre-restore",
                note="恢复前自动备份",
            )
        restore_from(snapshot.directory)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    # 恢复成功：pre-restore 安全备份已完成「可撤销恢复」的使命，立即删除，不堆积常驻备份。
    # 删除失败只记日志，不影响已成功的恢复结果。
    if safety is not None:
        try:
            request.app.state.backups.delete(safety.id)
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning(
                "恢复前临时备份清理失败（%s）：%s", safety.id, exc
            )
    return {
        "snapshotId": snapshot_id,
        "safetySnapshotId": safety.id if safety is not None else None,
    }


@router.get(
    "/backups/global",
    summary="全局备份：可勾选的分区与已有备份点",
    tags=["备份"],
)
def list_global_backups(request: Request, limit: int = 50) -> dict[str, Any]:
    """分区清单由后端给出（含「是否含凭据」标注），前端不写死分区表。"""
    parts = [
        {
            "key": key,
            "label": GLOBAL_PART_LABELS[key],
            "credential": key in CREDENTIAL_PARTS,
            "files": list(files),
        }
        for key, files in GLOBAL_PARTS.items()
    ]
    try:
        items = request.app.state.backups.list(GLOBAL_SCOPE, limit=limit)
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return {
        "scope": GLOBAL_SCOPE,
        "parts": parts,
        "defaultParts": list(DEFAULT_GLOBAL_PARTS),
        "snapshots": [_snapshot_payload(s) for s in items],
    }


@router.post(
    "/backups/global",
    summary="按分区创建全局备份（含凭据的分区需二次确认）",
    tags=["备份"],
)
def create_global_backup(request: Request, body: GlobalSnapshotRequest) -> dict[str, Any]:
    try:
        snapshot = request.app.state.backups.create_global(
            request.app.state.settings.home,
            body.parts or list(DEFAULT_GLOBAL_PARTS),
            note=body.note,
            confirm_credentials=body.confirmCredentials,
        )
    except Exception as exc:  # noqa: BLE001
        raise _handle(exc) from exc
    return _snapshot_payload(snapshot)


# ---------- 偏好（设置 → 常规） ----------


@router.get("/preferences", summary="读取常规设置（切换迁移开关）", tags=["设置"])
def get_preferences(request: Request) -> dict[str, Any]:
    return request.app.state.preferences.load().payload()


@router.put("/preferences", summary="修改常规设置", tags=["设置"])
def put_preferences(request: Request, body: PreferencesRequest) -> dict[str, Any]:
    current = request.app.state.preferences.load()

    def pick(value: bool | None, fallback: bool) -> bool:
        return fallback if value is None else value

    updated = type(current)(
        migrate_memory=pick(body.migrateMemory, current.migrate_memory),
        migrate_connectors=pick(body.migrateConnectors, current.migrate_connectors),
        migrate_sessions=pick(body.migrateSessions, current.migrate_sessions),
        migrate_profile=pick(body.migrateProfile, current.migrate_profile),
        migrate_workspaces=pick(body.migrateWorkspaces, current.migrate_workspaces),
        auto_backup=pick(body.autoBackup, current.auto_backup),
        auto_global_backup=pick(body.autoGlobalBackup, current.auto_global_backup),
    )
    try:
        saved = request.app.state.preferences.save(updated)
    except OSError as exc:
        raise HTTPException(status_code=500, detail=f"偏好写入失败：{exc}") from exc
    return saved.payload()


# ---------- 计划任务（设置 → 计划任务） ----------


def _schedule_payload(request: Request, key: str) -> dict[str, Any] | None:
    return next(
        (item for item in request.app.state.scheduler.payloads() if item["key"] == key), None
    )


@router.get("/schedules", summary="列出计划任务（保活 / 签到 / 全部活动）", tags=["设置"])
def list_schedules(request: Request) -> dict[str, Any]:
    return {"tasks": request.app.state.scheduler.payloads()}


@router.put("/schedules/{key}", summary="修改计划任务：时点 / 抖动 / 启用 / 范围", tags=["设置"])
def update_schedule(request: Request, key: str, body: ScheduleUpdateRequest) -> dict[str, Any]:
    scheduler = request.app.state.scheduler
    try:
        scheduler.update(
            key,
            enabled=body.enabled,
            times=body.times,
            jitter_minutes=body.jitterMinutes,
            account_ids=body.accountIds,
        )
    except SchedulerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"task": _schedule_payload(request, key)}


@router.post("/schedules/{key}/run-now", summary="立即执行一次计划任务", tags=["设置"])
def run_schedule_now(request: Request, key: str) -> dict[str, Any]:
    scheduler = request.app.state.scheduler
    try:
        outcome = scheduler.run_now(key)
    except SchedulerError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    logger.info("计划任务手动执行：%s → %s", key, outcome.get("result"))
    return {**outcome, "task": _schedule_payload(request, key)}


# ---------- 自动轮换（积分到期，防过期） ----------


@router.get("/rotation", summary="读取自动轮换策略与台账", tags=["设置"])
def read_rotation(request: Request) -> dict[str, Any]:
    return request.app.state.rotation.state()


@router.put("/rotation", summary="修改自动轮换阈值（省略的字段保持原值）", tags=["设置"])
def update_rotation(request: Request, body: RotationUpdateRequest) -> dict[str, Any]:
    patch = body.model_dump(exclude_none=True)
    try:
        request.app.state.rotation.update(patch)
    except RotationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return request.app.state.rotation.state()


@router.post(
    "/rotation/run-now",
    summary="立即按策略评估一轮（可选只推演，不写客户端）",
    tags=["设置"],
)
def run_rotation_now(request: Request, dryRun: bool = False) -> dict[str, Any]:
    """未启用也能跑：这是用户显式要求的一次评估。

    `dryRun=true` 只给出「切 / 不切」与原因，绝不写客户端；默认按策略真的执行，
    但仍受「客户端运行中不切」这道闸门约束。
    """
    try:
        return request.app.state.rotation.evaluate(dry_run=dryRun, manual=True)
    except RotationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------- 客户端设置托管（设置 → 客户端设置） ----------


@router.get(
    "/client/settings",
    summary="读取客户端设置文件（只读登记键 + 格式指纹 + 备份点）",
    tags=["设置"],
)
def get_client_settings(request: Request) -> dict[str, Any]:
    """返回登记键的当前值与格式指纹；未登记的键既不读也不返回。

    前端拿指纹去写，写入时再比对一次 —— 中间被别的东西改过就拒绝写。
    """
    return request.app.state.client_settings.snapshot()


@router.put(
    "/client/settings",
    summary="按白名单写入客户端设置（格式指纹 + 字节级最小补丁 + 自动备份）",
    tags=["设置"],
)
def put_client_settings(
    request: Request, body: ClientSettingsUpdateRequest
) -> dict[str, Any]:
    try:
        return request.app.state.client_settings.write(
            body.updates, body.confirmFingerprint
        )
    except ClientSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(
    "/client/settings/restore",
    summary="用备份点还原客户端设置文件（恢复前会先备份当前内容）",
    tags=["设置"],
)
def restore_client_settings(
    request: Request, body: ClientSettingsRestoreRequest
) -> dict[str, Any]:
    try:
        return request.app.state.client_settings.restore(body.backupId)
    except ClientSettingsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------- 概览：待处理清单 ----------

#: 需要用户回客户端处理一手的凭据状态 → (severity, 标题, 说明)。
#: `unverified` 单列一档并降级为 info：它是「还没验过」，不等于「已失效」。
_ATTENTION_STATES: dict[str, tuple[str, str, str]] = {
    CredentialState.RELOGIN_REQUIRED.value: (
        "high",
        "需要重新登录",
        "登录态已失效，重新导入或扫码登录后才能继续使用",
    ),
    CredentialState.UNVERIFIED.value: (
        "info",
        "需要验证登录态",
        "还没校验过凭据，建议先验证一次，免得用到一半才发现失效",
    ),
}

_SEVERITY_ORDER = {"high": 0, "warn": 1, "info": 2}


def _scope_label(scope_key: str) -> str:
    try:
        return get_scope(scope_key).label
    except UnknownScope:
        return scope_key


@router.get("/overview/actionables", summary="概览待处理清单（只读聚合）", tags=["基础"])
def overview_actionables(request: Request) -> dict[str, Any]:
    """把「今天该处理的号」聚合成一张只读清单，四类数据源并发取后合成。

    - 凭据状态 → 需重新登录 / 需验证登录态；
    - 活动缓存 → 今日未签到（`state=todo` 且 `automatable`）；
    - 积分缓存 → 积分将过期；
    - 客户端检测 → 客户端未运行。

    活动与积分都走**既有缓存**（`force=False`），这一屏不会新增上游压力。
    本接口**只读**：不写库、不改客户端、不发执行类请求；要执行什么由返回的
    `action` 描述（`navigate` 跳页 / `run` 调接口），前端据此渲染按钮。
    """
    accounts = _activities_accounts(request, None)
    activities = request.app.state.activities
    credits = request.app.state.credits
    with ThreadPoolExecutor(max_workers=3) as pool:
        clients_future = pool.submit(_detection_items, request)
        activities_future = pool.submit(activities.detect, accounts, force=False)
        credits_future = pool.submit(credits.query_many, accounts)
        clients = clients_future.result()
        detected = activities_future.result()
        credit_map = credits_future.result()

    items: list[dict[str, Any]] = []

    # 1) 需要重新登录 / 需要验证登录态
    for account in accounts:
        hit = _ATTENTION_STATES.get(account.credential_state.value)
        if hit is None:
            continue
        severity, title, reason = hit
        items.append(
            {
                "kind": "credential",
                "severity": severity,
                "title": title,
                "detail": f"{account.display_name}（{_scope_label(account.scope)}）：{reason}",
                "accountId": account.id,
                "action": {"type": "navigate", "to": "/accounts"},
            }
        )

    # 2) 今日未签到（同类里也可能有其它可自动完成的活动）
    for account in detected:
        if not account.detectable:
            continue
        pending = [entry for entry in account.items if entry.actionable]
        if not pending:
            continue
        names = "、".join(entry.title for entry in pending)
        items.append(
            {
                "kind": "checkin",
                "severity": "warn",
                "title": "今日未签到",
                "detail": f"{account.display_name}：{names} 尚未完成，可一键代做",
                "accountId": account.account_id,
                "action": {
                    "type": "run",
                    "endpoint": "/activities/run",
                    "accountIds": [account.account_id],
                },
            }
        )

    # 3) 积分将过期
    for account in accounts:
        info = credit_map.get(account.id)
        if info is None or not info.expiring_soon:
            continue
        amount = "" if info.credits is None else f"{info.credits:g} 积分"
        items.append(
            {
                "kind": "credits_expiring",
                "severity": "warn",
                "title": "积分将过期",
                "detail": f"{account.display_name}：{amount}即将到期，建议优先使用",
                "accountId": account.id,
                "action": {"type": "navigate", "to": "/usage"},
            }
        )

    # 4) 客户端未运行（按客户端而非账号报，避免一台没开就刷出几十条）
    for client in clients:
        if not client["installed"] or client["running"]:
            continue
        items.append(
            {
                "kind": "client_offline",
                "severity": "info",
                "title": "客户端未运行",
                "detail": (
                    f"{client['label']} 未运行，该平台的签到与切换需要客户端处于运行状态"
                ),
                "accountId": None,
                "action": {"type": "navigate", "to": "/accounts"},
            }
        )

    items.sort(key=lambda entry: _SEVERITY_ORDER.get(entry["severity"], 3))

    # 概览顶部的「今日速览」。全部来自上面已经取到的缓存，不额外打上游。
    gateway_store = request.app.state.gateway
    gateway_stats = gateway_store.stats()
    pulse = {
        "accounts": len(accounts),
        "detectable": sum(1 for item in detected if item.detectable),
        "activityActionable": sum(
            1 for item in detected for entry in item.items if entry.actionable
        ),
        "activityDone": sum(
            1 for item in detected for entry in item.items if entry.state == "done"
        ),
        "activityTotal": sum(len(item.items) for item in detected if item.detectable),
        "credits": sum(
            info.credits or 0
            for info in credit_map.values()
            if info.available and not info.error and info.credits
        ),
        "creditsExpiring": sum(1 for info in credit_map.values() if info.expiring_soon),
        "clientsRunning": sum(1 for client in clients if client["running"]),
        "gateway": {
            "requests": gateway_stats["requests"],
            "errors": gateway_stats["errors"],
            "tokens": gateway_stats["totalTokens"],
            "activeKeys": gateway_store.active_key_count(),
        },
    }
    return {"items": items, "pulse": pulse}
