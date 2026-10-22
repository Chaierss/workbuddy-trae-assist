from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from chaierww.api.docs import build_docs_router
from chaierww.api.gateway import api_router as gateway_api_router
from chaierww.api.gateway import build_gateway_app, refresh_catalog
from chaierww.api.gateway import v1_router as gateway_v1_router
from chaierww.api.logs import build_logs_router
from chaierww.api.routes import router
from chaierww.api.swagger import mount_swagger
from chaierww.config import Settings, load_settings
from chaierww.domain.accounts.models import CredentialState
from chaierww.domain.accounts.repository import AccountRepository
from chaierww.domain.accounts.service import AccountService
from chaierww.domain.activities import ActivitiesService
from chaierww.domain.auto_backup import AutoBackupRunner
from chaierww.domain.backup import BackupService
from chaierww.domain.client_settings import ClientSettingsService, default_settings_file
from chaierww.domain.credits import CreditsService
from chaierww.domain.gateway import CatalogRefresher, GatewayRuntime, GatewayStore
from chaierww.domain.gateway_relay import GatewayRelay
from chaierww.domain.oauth import build_oauth
from chaierww.domain.preferences import PreferencesStore
from chaierww.domain.rotation import Candidate as RotationCandidate
from chaierww.domain.rotation import (
    RotationRunner,
    RotationService,
    RotationSnapshot,
    RotationStore,
)
from chaierww.domain.scheduler import Scheduler
from chaierww.domain.scopes import all_scopes
from chaierww.domain.switching import SwitchEngine, build_adapters
from chaierww.domain.trae_oauth import build_trae_oauth
from chaierww.domain.usage import UsageService
from chaierww.domain.wb_tasks import GrowthTaskRunner
from chaierww.infra.db import open_database
from chaierww.infra.parent_watch import watch_parent
from chaierww.infra.secrets import default_box
from chaierww.logging_setup import setup_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()
    resolved.ensure_dirs()
    logger = setup_logging(resolved.logs_dir)

    conn = open_database(resolved.db_path)
    box = default_box()
    repository = AccountRepository(conn, box)
    accounts = AccountService(repository)
    backups = BackupService(conn, resolved.backups_dir)
    adapters = build_adapters()
    engine = SwitchEngine(
        repository, backups, adapters, conn, keep_snapshots=resolved.snapshot_keep
    )
    # 额度网关：配置/Key/日志落盘，真正的转发由 gateway_relay + api/gateway 完成。
    # 选号器同时接两个只读数据源：排除集（持久化）与积分缓存（判断「积分耗尽」）。
    gateway = GatewayStore(resolved.home)
    credits = CreditsService(repository)
    gateway_relay = GatewayRelay(
        repository, excluded=gateway.excluded_accounts, credits=credits.cached
    )
    gateway_runtime = GatewayRuntime(gateway)

    preferences = PreferencesStore(resolved.home)
    activities = ActivitiesService(repository, growth_runner=GrowthTaskRunner())

    def scheduled_accounts(account_ids: tuple[str, ...]) -> list[Any]:
        implemented = [scope.key for scope in all_scopes() if scope.implemented]
        found = accounts.list_merged(implemented)
        if account_ids:
            wanted = set(account_ids)
            found = [item for item in found if item.id in wanted]
        return found

    def run_scheduled(key: str, account_ids: tuple[str, ...]) -> str:
        """计划任务的执行体：调度器只问「什么时候跑」，这里回答「跑什么」。"""
        targets = scheduled_accounts(account_ids)
        if not targets:
            return "号池里没有可用账号，本轮跳过"
        if key == "keepalive":
            # 保活 = 逐账号发一次已鉴权探测（顺带刷新活动缓存），确认登录态仍有效。
            detected = activities.detect(targets, force=True)
            healthy = sum(1 for item in detected if item.detectable)
            return f"保活探测 {len(targets)} 个账号，{healthy} 个正常"
        if key == "checkin":
            report = activities.run(targets, include_growth=False)
        elif key in ("travel_depart", "travel_claim"):
            # 派猫猫旅行拆成两个独立动作：早上派、下午领。两者互不挤占，
            # 同一天能各成功一次 —— 以前挤在一个函数里，一次执行只能做其中一件。
            report = activities.run_travel_stage(
                targets, stage="depart" if key == "travel_depart" else "claim"
            )
        else:
            report = activities.run(targets)
        summary = report.get("summary", {})
        line = (
            f"账号 {len(targets)} 个：成功 {summary.get('ok', 0)}，"
            f"跳过 {summary.get('skipped', 0)}，失败 {summary.get('failed', 0)}"
        )
        # 只报数字等于没报 —— 把失败原因按出现次数归并进来，日志才能自己解释自己。
        reasons: dict[str, int] = {}
        for item in report.get("results", []):
            if item.get("status") == "failed":
                message = str(item.get("message") or "未知原因")
                reasons[message] = reasons.get(message, 0) + 1
        if reasons:
            brief = "；".join(
                f"{message}×{count}" if count > 1 else message
                for message, count in list(reasons.items())[:2]
            )
            line += f"（{brief}）"
        return line

    scheduler = Scheduler(resolved.home, executor=run_scheduled)

    # 积分到期自动轮换（默认关闭，设置里手动开）：判定走 `domain/rotation.py`，
    # 这里只提供两个回调 —— 取快照（账号库 + 积分 + 客户端状态）与执行切换。
    def rotation_snapshot(scope: str) -> RotationSnapshot:
        implemented = [item.key for item in all_scopes() if item.implemented]
        if scope not in implemented:
            return RotationSnapshot()
        pool = accounts.list_merged([scope])
        credits_info = credits.query_many(pool) if pool else {}
        candidates: list[RotationCandidate] = []
        current_account_id: str | None = None
        for account in pool:
            info = credits_info.get(account.id)
            reachable = bool(info is not None and info.available)
            candidates.append(
                RotationCandidate(
                    account_id=account.id,
                    display_name=account.display_name,
                    scope=account.scope,
                    # 查不到余额一律给 None（≠ 0），判定第 1 条据此排除该账号。
                    remaining=info.credits if reachable else None,
                    expires_at=info.expires_at if reachable else None,
                    available=reachable,
                    switchable=(
                        account.credential_state is not CredentialState.RELOGIN_REQUIRED
                    ),
                    last_used_at=account.last_used_at,
                )
            )
        adapter = adapters.get(scope)
        running = True  # 探测失败按「运行中」处理：宁可这一轮不切，也不误切。
        if adapter is not None:
            try:
                running = bool(adapter.is_running())
            except Exception:  # noqa: BLE001 - 探测失败不拖垮评估
                logger.warning("自动轮换：客户端运行状态探测失败，按「运行中」处理")
        current_uid = engine.current_uid(scope)
        if current_uid:
            found = next(
                (item for item in pool if item.external_id == current_uid), None
            )
            current_account_id = found.id if found is not None else None
        return RotationSnapshot(
            candidates=tuple(candidates),
            current_account_id=current_account_id,
            client_running=running,
        )

    def rotation_switch(scope: str, account_id: str) -> tuple[bool, str]:
        """执行一次轮换切换。数据迁移**沿用常规设置里的开关**（与手动切换完全一致），
        以免自动切换与手动切换在「迁不迁数据」上给出两套行为。"""
        prefs = preferences.load()
        report = engine.execute(
            scope,
            account_id,
            migrate_memory=prefs.migrate_memory,
            migrate_connectors=prefs.migrate_connectors,
            migrate_sessions=prefs.migrate_sessions,
            migrate_profile=prefs.migrate_profile,
            migrate_workspaces=prefs.migrate_workspaces,
        )
        if report.succeeded:
            return True, f"已切换（备份点 {report.snapshot_id or '无'}）"
        return False, f"切换未成功：{report.error or report.state}"

    rotation = RotationService(
        RotationStore(resolved.home),
        snapshot=rotation_snapshot,
        switch=rotation_switch,
    )
    rotation_runner = RotationRunner(rotation)

    # 模型目录自动刷新：间隔从配置现读（设置里一改就生效，0 = 不刷）。
    # 抓取复用 api/gateway 的 refresh_catalog（内部 asyncio.run，跑在自己的线程里）。
    catalog_refresher = CatalogRefresher(
        lambda: gateway.config().catalog_refresh_minutes,
        lambda: asyncio.run(refresh_catalog(gateway_relay)),
    )

    # 每日自动备份：开关从偏好现读；目标 = 每个已实现作用域的客户端数据目录。
    def auto_backup_targets() -> list[tuple[str, Path]]:
        items: list[tuple[str, Path]] = []
        for item in all_scopes():
            adapter = adapters.get(item.key)
            if item.implemented and adapter is not None:
                items.append((item.key, adapter.state_dir()))
        return items

    auto_backup = AutoBackupRunner(
        lambda: preferences.load().auto_backup,
        auto_backup_targets,
        backups,
        # 全局备份：分区随偏好现读（默认 config 不含凭据；用户勾选含凭据分区确认后
        # 自动备份也跟着纳入），开关在设置 → 备份页。
        global_enabled=lambda: preferences.load().auto_global_backup,
        global_home=lambda: resolved.home,
        global_parts=lambda: preferences.load().auto_global_parts,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        purged = accounts.purge_expired(resolved.recycle_days)
        if purged:
            logger.info("回收站清理：删除 %s 个过期账号", purged)
        if gateway.config().enabled:
            def factory() -> FastAPI:
                return build_gateway_app(gateway, gateway_relay, gateway_runtime)

            gateway_runtime.apply(factory)
            if gateway_runtime.running:
                logger.info("网关已监听 %s", gateway.config().base_url)
            else:
                logger.warning("网关未启动：%s", gateway_runtime.error)
        # 保活 / 签到不再从常规设置读偏好：它们就是本调度器里的两个任务，
        # 权威状态在 schedules.json 里（`_default_tasks` 默认开着保活与签到）。
        scheduler.start()
        # 轮换线程始终运行：未启用时每轮只读一次策略就返回，这样设置里一开就生效。
        rotation_runner.start()
        # 目录自动刷新线程同样始终运行：间隔为 0 时它只空转，不抓任何东西。
        catalog_refresher.start()
        # 自动备份线程始终运行：开关关着时每轮只读一次偏好就返回。
        auto_backup.start()
        logger.info("chaierww 已启动，数据目录 %s", resolved.home)
        try:
            yield
        finally:
            auto_backup.stop()
            catalog_refresher.stop()
            rotation_runner.stop()
            scheduler.stop()
            gateway_runtime.stop()
            conn.close()
            logger.info("chaierww 已停止")

    # 关掉 FastAPI 自带的 /docs 与 /redoc：前端 /docs 是应用内的文档页，
    # 两者路径冲突会让浏览器直接落到 Swagger UI 上。规格本身仍挂在 /api/openapi.json。
    app = FastAPI(
        title="chaierww",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = resolved
    app.state.box = box
    app.state.accounts = accounts
    app.state.repository = repository
    app.state.backups = backups
    app.state.adapters = adapters
    app.state.engine = engine
    app.state.gateway = gateway
    app.state.gateway_relay = gateway_relay
    app.state.gateway_runtime = gateway_runtime
    app.state.preferences = preferences
    app.state.oauth = build_oauth()
    app.state.trae_oauth = build_trae_oauth()
    app.state.credits = credits
    app.state.activities = activities
    app.state.scheduler = scheduler
    app.state.rotation = rotation
    # 用量页走 UsageService（读云端明细），对话正文从本机 workbuddy.db / projects/*.jsonl 关联。
    usage = UsageService(repository)
    app.state.usage = usage
    # 客户端设置托管：只读写登记的那一个 settings.json，备份落在我们自己的目录。
    app.state.client_settings = ClientSettingsService(
        default_settings_file(), resolved.home / "client-settings" / "backups"
    )
    # 自建的 /docs（同源 iframe 内嵌 Swagger UI）必须排在 SPA 兜底之前，
    # 否则会被 `/{full_path:path}` 抢走。
    app.include_router(router)
    # 网关接口：/api/gateway/*（界面）与 /v1/*（OpenAI 兼容客户端）。
    # /v1/* 挂在主服务上是为了「未启用时给客户端一个可读的 503」，而不是 SPA 兜底页；
    # 启用后另有独立端口的网关应用（build_gateway_app）承接同样的路由。
    app.include_router(gateway_api_router)
    app.include_router(gateway_v1_router)
    app.include_router(build_docs_router())
    app.include_router(build_logs_router())
    mount_swagger(app)
    _mount_webui(app)
    return app


def resolve_webui() -> Path | None:
    """定位前端构建产物。

    优先用环境变量覆盖（打包/桌面端会用到），否则按仓库布局找 web/dist。
    找不到就只提供 API —— 这是开发早期和后端独立运行时的正常状态。
    """
    override = os.environ.get("CHAIERWW_WEBUI")
    if override:
        candidate = Path(override).expanduser()
        return candidate if (candidate / "index.html").is_file() else None

    repo_web = Path(__file__).resolve().parents[2] / "web" / "dist"
    return repo_web if (repo_web / "index.html").is_file() else None


def _mount_webui(app: FastAPI) -> None:
    webui = resolve_webui()
    if webui is None:
        logging.getLogger("chaierww").warning(
            "未找到前端产物（web/dist），当前只提供 API。请先执行 npm run build。"
        )
        return

    assets = webui / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(webui / "index.html")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        # 单页应用路由交给前端，但 API 的未知路径必须保持 404，
        # 否则前端会拿到一份 HTML 却按 JSON 解析。
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="接口不存在")
        target = webui / full_path
        if full_path and target.is_file():
            return FileResponse(target)
        return FileResponse(webui / "index.html")


def run() -> None:
    import uvicorn

    # 桌面端会传父进程 PID 过来（见 desktop/main.js）。父进程被强杀/崩溃时，
    # 这里能自己退出，避免变成孤儿进程占着端口和安装目录里的 exe。
    watch_parent()

    settings = load_settings()
    app = create_app(settings)
    logging.getLogger("chaierww").info("监听 http://%s:%s", settings.host, settings.port)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    run()
