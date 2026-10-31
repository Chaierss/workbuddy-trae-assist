from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from chaierww import __version__
from chaierww.api.diagnostics import router as diagnostics_router
from chaierww.api.docs import build_docs_router
from chaierww.api.gateway import api_router as gateway_api_router
from chaierww.api.gateway import build_gateway_app, refresh_catalog
from chaierww.api.gateway import v1_router as gateway_v1_router
from chaierww.api.guard import (
    build_auth_router,
    install_api_guard,
    install_security_headers,
)
from chaierww.api.logs import build_logs_router
from chaierww.api.routes import router
from chaierww.api.swagger import mount_swagger
from chaierww.config import Settings, default_home, load_settings
from chaierww.domain.account_purge import AccountPurger
from chaierww.domain.accounts.models import CredentialState
from chaierww.domain.accounts.repository import AccountRepository
from chaierww.domain.accounts.service import AccountService
from chaierww.domain.activities import ActivitiesService, validate_official_catalog
from chaierww.domain.auto_backup import AutoBackupRunner
from chaierww.domain.backup import BackupService
from chaierww.domain.client_settings import ClientSettingsService, default_settings_file
from chaierww.domain.credential_lifecycle import MODE_DUE, CredentialLifecycleService
from chaierww.domain.credits import CreditsService
from chaierww.domain.gateway import CatalogRefresher, GatewayRuntime, GatewayStore
from chaierww.domain.gateway_relay import GatewayRelay
from chaierww.domain.metrics import MetricsStore
from chaierww.domain.oauth import build_oauth
from chaierww.domain.preferences import PreferencesStore
from chaierww.domain.restore import apply_pending_restore
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
from chaierww.domain.sync import SyncEngine
from chaierww.domain.sync.repository import SyncRepository
from chaierww.domain.trae_oauth import build_trae_oauth
from chaierww.domain.usage import UsageService
from chaierww.domain.wb_tasks import GrowthTaskRunner
from chaierww.infra.crash import (
    install_excepthooks,
    install_exception_handler,
    install_request_id,
)
from chaierww.infra.db import checkpoint, open_database
from chaierww.infra.parent_watch import watch_parent
from chaierww.infra.secrets import default_box
from chaierww.infra.startup import clear_startup_error, write_startup_error
from chaierww.logging_setup import setup_logging


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or load_settings()
    resolved.ensure_dirs()
    logger = setup_logging(resolved.logs_dir)

    # 目录校验只告警、不阻断启动：单个活动文案写错不该让用户打不开应用。
    # CI 与 scripts/check_activity_catalog.py 用的是同一份校验，会在合并前拦住。
    try:
        validate_official_catalog()
    except ValueError as exc:
        logger.warning("官方活动目录校验未通过：%s", exc)

    # 原地恢复必须在打开数据库之前执行：覆盖 .db 时不能有活动连接。
    restore_result = apply_pending_restore(resolved.home)
    if restore_result is not None:
        if restore_result.get("ok"):
            logger.warning("已完成全局备份恢复：%s", restore_result.get("snapshotId"))
        else:
            logger.error("全局备份恢复失败：%s", restore_result.get("error"))

    conn = open_database(
        resolved.db_path, backup_dir=resolved.pre_migrate_dir
    )
    box = default_box()
    repository = AccountRepository(conn, box)
    accounts = AccountService(repository)
    backups = BackupService(conn, resolved.backups_dir)
    adapters = build_adapters()
    sync_repository = SyncRepository(conn, repository.lock)
    sync_engine = SyncEngine(repository, sync_repository, resolved.home, adapters)
    preferences = PreferencesStore(resolved.home)
    metrics = MetricsStore(resolved.home)
    engine = SwitchEngine(
        repository,
        backups,
        adapters,
        conn,
        keep_snapshots=resolved.snapshot_keep,
        sync=sync_engine,
        # 记「第一次成功换号」的时间：本地漏斗只用它算激活与 TTFV。
        on_success=lambda _scope, _account_id: preferences.mark_first_switch(),
    )
    # 额度网关：配置/Key/日志落盘，真正的转发由 gateway_relay + api/gateway 完成。
    # 选号器同时接两个只读数据源：排除集（持久化）与积分缓存（判断「积分耗尽」）。
    gateway = GatewayStore(resolved.home)
    credits = CreditsService(repository)
    gateway_relay = GatewayRelay(
        repository, excluded=gateway.excluded_accounts, credits=credits.cached
    )
    gateway_runtime = GatewayRuntime(gateway)

    activities = ActivitiesService(repository, growth_runner=GrowthTaskRunner())
    # 凭据生命周期：过期判断 / 续期 / 验活。路由与计划任务共用同一实例，
    # 这样手动点的「验活」结果与定时巡检的结论不会各说各话（缓存也才生效）。
    lifecycle = CredentialLifecycleService(repository)

    def scheduled_accounts(account_ids: tuple[str, ...]) -> list[Any]:
        implemented = [scope.key for scope in all_scopes() if scope.implemented]
        found = accounts.list_merged(implemented)
        if account_ids:
            wanted = set(account_ids)
            found = [item for item in found if item.id in wanted]
        return found

    def run_scheduled(
        key: str, account_ids: tuple[str, ...], activity_keys: tuple[str, ...]
    ) -> str:
        """计划任务的执行体：调度器只问「什么时候跑」，这里回答「跑什么」。

        ``activity_keys`` 是任务里配置的活动白名单；空 = 全部活动。
        """
        targets = scheduled_accounts(account_ids)
        if not targets:
            return "号池里没有可用账号，本轮跳过"
        keys = set(activity_keys) or None
        if key == "session_sync":
            # 只读采集：客户端运行中也安全，因此无需等待退出。只采「当前登录的那个号」，
            # 其余账号没有客户端数据可读，采了也是空跑。
            if not preferences.load().session_sync_enabled:
                return "会话同步已在设置中关闭，本轮跳过"
            lines: list[str] = []
            for scope_key in ("workbuddy-cn", "trae-cn"):
                if scope_key not in adapters:
                    continue
                uid = engine.current_uid(scope_key)
                account = repository.get_by_external(scope_key, uid) if uid else None
                if account is None:
                    lines.append(f"{scope_key}：当前未登录或未导入，跳过")
                    continue
                report = sync_engine.capture(scope_key, account.id)
                if report.state == "unavailable":
                    lines.append(f"{scope_key}：不支持会话同步")
                    continue
                lines.append(
                    f"{scope_key}：{report.state}，镜像 {report.counts.get('captured', 0)} 条"
                )
            return "；".join(lines) or "没有可同步的账号"
        if key == "credential_refresh":
            # 只续「有效期已知且到期前 30 分钟」的号；其余轮次是空跑，
            # 所以默认开启也不会给上游增加日常压力。
            results = lifecycle.sweep(targets, mode=MODE_DUE)
            return lifecycle.describe(results, MODE_DUE)
        if key == "keepalive":
            # 保活 = 逐账号发一次已鉴权探测（顺带刷新活动缓存），确认登录态仍有效。
            detected = activities.detect(targets, force=True)
            healthy = sum(1 for item in detected if item.detectable)
            return f"保活探测 {len(targets)} 个账号，{healthy} 个正常"
        if key == "checkin":
            # 签到是纯读 + 一次幂等写；失败只重试「网络异常 / 5xx / 9074」的号，
            # 401/403 与业务拒绝不会重试。「全部活动」有更多写动作，保持不重试。
            report = activities.run(
                targets, include_growth=False, retry_failed=True, keys=keys
            )
        elif key in ("travel_depart", "travel_claim"):
            # 派猫猫旅行拆成两个独立动作：早上派、下午领。两者互不挤占，
            # 同一天能各成功一次 —— 以前挤在一个函数里，一次执行只能做其中一件。
            wanted = "workbuddy.travel"
            if keys is not None and wanted not in keys:
                return "本任务未勾选「派猫猫旅行」，本轮跳过"
            report = activities.run_travel_stage(
                targets, stage="depart" if key == "travel_depart" else "claim"
            )
        else:
            report = activities.run(targets, keys=keys)
        summary = report.get("summary", {})
        line = (
            f"账号 {len(targets)} 个：成功 {summary.get('ok', 0)}，"
            f"跳过 {summary.get('skipped', 0)}，失败 {summary.get('failed', 0)}"
        )
        retried = summary.get("retried", 0)
        if retried:
            line += f"，重试 {retried} 个账号（其中 {summary.get('retryOk', 0)} 项转为成功）"
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

    # 客户端「刚退出」这个边沿没有定时点：调度器每 30 秒心跳一次，这里对比
    # 上轮/本轮运行状态，捕捉「运行中 → 未运行」的瞬间补一次只读采集。
    last_running: dict[str, bool] = {}

    def sync_tick() -> None:
        if not preferences.load().session_sync_enabled:
            return
        for scope_key, adapter in adapters.items():
            try:
                running = bool(adapter.is_running())
            except Exception:  # noqa: BLE001 - 探测失败当作没发生，下轮再试
                continue
            previous = last_running.get(scope_key)
            last_running[scope_key] = running
            if previous is not True or running:
                continue
            uid = engine.current_uid(scope_key)
            account = repository.get_by_external(scope_key, uid) if uid else None
            if account is None:
                continue
            try:
                sync_engine.capture(scope_key, account.id, record_job=False)
            except Exception:  # noqa: BLE001 - 后台心跳绝不影响主流程
                logger.exception("客户端退出后采集会话失败：%s", scope_key)

    scheduler = Scheduler(resolved.home, executor=run_scheduled, tick_hook=sync_tick)

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

    # 轮换台账的存储实例要给两处用：轮换服务本身，以及永久删除账号时的关联清理。
    rotation_store = RotationStore(resolved.home)
    rotation = RotationService(
        rotation_store,
        snapshot=rotation_snapshot,
        switch=rotation_switch,
    )
    rotation_runner = RotationRunner(rotation)

    # 回收站「彻底删除」：删账号行之后，顺手清掉散在别处的引用
    # （网页版快照 / 网关账号池 / 轮换台账）。
    purger = AccountPurger(
        accounts, gateway=gateway, rotation=rotation_store, sync=sync_engine
    )

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
            try:
                checkpoint(conn)
            except Exception:  # noqa: BLE001 - 关闭前 checkpoint 失败只记日志
                logger.warning("WAL checkpoint 失败，保留 -wal 文件待下次启动回收")
            conn.close()
            logger.info("chaierww 已停止")

    # 关掉 FastAPI 自带的 /docs 与 /redoc：前端 /docs 是应用内的文档页，
    # 两者路径冲突会让浏览器直接落到 Swagger UI 上。规格本身仍挂在 /api/openapi.json。
    app = FastAPI(
        title="chaierww",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.state.settings = resolved
    app.state.conn = conn
    app.state.started_at = time.monotonic()
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
    app.state.metrics = metrics
    app.state.oauth = build_oauth()
    app.state.trae_oauth = build_trae_oauth()
    app.state.credits = credits
    app.state.activities = activities
    app.state.credential_lifecycle = lifecycle
    app.state.scheduler = scheduler
    app.state.sync = sync_engine
    app.state.sync_repository = sync_repository
    app.state.rotation = rotation
    app.state.purger = purger
    # 用量页走 UsageService（读云端明细），对话正文从本机 workbuddy.db / projects/*.jsonl 关联。
    usage = UsageService(repository)
    app.state.usage = usage
    # 客户端设置托管：只读写登记的那一个 settings.json，备份落在我们自己的目录。
    app.state.client_settings = ClientSettingsService(
        default_settings_file(), resolved.home / "client-settings" / "backups"
    )
    # 中间件顺序：守卫 → 安全头 → request-id（最后添加的最外层）。
    # 这样 401/403 也带安全头，所有响应（含守卫拒绝）都带 X-Request-Id。
    token = install_api_guard(app, resolved)
    install_security_headers(app)
    install_request_id(app)
    install_exception_handler(app)
    # 自建的 /docs（同源 iframe 内嵌 Swagger UI）必须排在 SPA 兜底之前，
    # 否则会被 `/{full_path:path}` 抢走。
    app.include_router(build_auth_router(resolved, token))
    app.include_router(diagnostics_router)
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
    # 兜底路由只允许读 dist 内的文件。`%2F` 编码的 `..` 会被解码成真实路径
    # （`GET /..%2F..%2FCHANGELOG.md`），不校验就能把安装目录外的文件读出来。
    webui_root = webui.resolve()

    # 首页必须每次回源。桌面端升级后 resources/webui 里的文件换了，但 Chromium
    # 会把没有缓存头的 `index.html` 留在磁盘缓存里，于是它继续引用**上一版**的
    # 资源 —— 表现是「版本号已经是 0.4.2，界面还是 0.4.0」。带内容哈希的
    # `/assets/*` 可以放心缓存，唯独入口页不能。
    no_store = {"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}
    # SPA 入口用严格 CSP：脚本只允许同源文件（主题脚本已外置）。
    # `/docs` 是自建 iframe + Swagger UI，不套这份 CSP，避免把文档页打坏。
    csp = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self' http://127.0.0.1:* http://localhost:* "
        "ws://127.0.0.1:* ws://localhost:*; "
        "frame-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"
    )
    index_headers = {**no_store, "Content-Security-Policy": csp}

    def index_response() -> FileResponse:
        return FileResponse(webui / "index.html", headers=index_headers)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return index_response()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        # 单页应用路由交给前端，但 API 的未知路径必须保持 404，
        # 否则前端会拿到一份 HTML 却按 JSON 解析。
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="接口不存在")
        if full_path:
            target = (webui / full_path).resolve()
            if target.is_file() and target.is_relative_to(webui_root):
                # 入口页同样不许缓存；图标等小文件按默认策略即可。
                if target.name == "index.html":
                    return index_response()
                return FileResponse(target)
        return index_response()


def run() -> None:
    import uvicorn

    # 桌面端会传父进程 PID 过来（见 desktop/main.js）。父进程被强杀/崩溃时，
    # 这里能自己退出，避免变成孤儿进程占着端口和安装目录里的 exe。
    watch_parent()

    try:
        settings = load_settings()
    except ValueError as exc:
        # 端口配置写错也必须留下可读原因：此时 home 还没解析出来，按默认规则取一次。
        try:
            home = default_home()
            home.mkdir(parents=True, exist_ok=True)
            write_startup_error(home, exc)
        except OSError:
            logging.getLogger("chaierww").exception("写入启动错误文件失败")
        raise
    install_excepthooks(settings.home)
    try:
        app = create_app(settings)
    except BaseException as exc:
        # 启动失败必须留下可读原因：桌面端会读取这个文件并展示。
        try:
            write_startup_error(settings.home, exc)
        except OSError:
            logging.getLogger("chaierww").exception("写入启动错误文件失败")
        raise
    clear_startup_error(settings.home)
    logging.getLogger("chaierww").info("监听 http://%s:%s", settings.host, settings.port)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    run()
