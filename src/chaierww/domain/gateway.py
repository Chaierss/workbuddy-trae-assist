"""额度网关（P12）：本地 OpenAI 兼容入口的「配置 / API Key / 日志 / 生命周期」。

## 它解决什么

WorkBuddy 客户端一次只能登一个账号。网关把号池里的多个账号收敛成一个本地
OpenAI 兼容端点（`/v1/chat/completions` + `/v1/models`），任何支持自定义
`base_url` 的客户端都能「拿一个 Key 打一整个号池」：请求进来 → 校验 Key →
从号池挑一个健康账号 → 用它的令牌转发到上游 → 流式回传 → 记一笔网关日志。

## 硬边界（对齐参考实现的安全红线）

- **默认关闭**，且**只监听回环**（`127.0.0.1` / `::1` / `localhost`）。
  要绑非回环地址必须显式打开 `allowNonLoopback`，否则拒绝启动 ——
  绝不出现「不小心把账号池开成公网代理」。
- **无 Key 不放行**：`/v1/*` 一律要求 `Authorization: Bearer sk-...`。
- **Key 只存摘要**：`gateway-keys.json` 里只有 sha256 摘要 + 前后缀，
  明文只在签发那一刻返回一次，之后不可再取。
- **与主服务同进程、不同端口**：网关是额外跑在后台线程里的 uvicorn，
  绑自己的端口，主服务（界面 57910）不受影响。

本模块只做状态与持久化，**不发任何网络请求**；选号与协议在 `gateway_relay.py`。
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from math import ceil
from pathlib import Path
from secrets import token_hex
from typing import Any

from chaierww.infra.fsutil import read_json_or_default, write_json_atomic

logger = logging.getLogger("chaierww")

CONFIG_FILENAME = "gateway.json"
KEYS_FILENAME = "gateway-keys.json"
LOGS_FILENAME = "gateway-logs.jsonl"
STATS_FILENAME = "gateway-stats.json"
POOL_FILENAME = "gateway-pool.json"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8788
DEFAULT_MAX_HOPS = 3
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
KEY_PREFIX = "sk-"
# 老版本签发的 Key 是 `sk-wb-…`：它同样以 `sk-` 开头，摘要又按整串算，
# 所以换了前缀也不会让已经配到客户端里的老 Key 失效。
MIN_PORT = 1024
MAX_PORT = 65535
LOG_KEEP = 200
#: 日志保留条数的可配范围；默认沿用 `LOG_KEEP`，旧存档照旧 200。
LOG_KEEP_MIN = 50
LOG_KEEP_MAX = 5000
#: Key 限额（日 token / 日请求 / 每分钟请求）的合法上限，防止误填天文数字。
LIMIT_MAX = 10_000_000
RPM_LIMIT_MAX = 600_000
RPM_WINDOW_SECONDS = 60.0
START_TIMEOUT_SECONDS = 6.0
#: 模型范围三段式：`all`（全部，含目录新增）/ `capability`（按能力，随目录自动重算）
#: / `list`（静态白名单）。
SCOPE_ALL = "all"
SCOPE_CAPABILITY = "capability"
SCOPE_LIST = "list"
KEY_SCOPES = (SCOPE_ALL, SCOPE_CAPABILITY, SCOPE_LIST)
#: 能力键：免费 / 支持图片 / 大上下文（≥128k）。落盘用英文键，界面另做翻译。
CAPABILITY_FREE = "free"
CAPABILITY_IMAGE = "image"
CAPABILITY_LONG_CONTEXT = "long_context"
CAPABILITY_KEYS = (CAPABILITY_FREE, CAPABILITY_IMAGE, CAPABILITY_LONG_CONTEXT)
LONG_CONTEXT_TOKENS = 128_000
#: 目录自动刷新间隔的合法范围（分钟）；0 表示关闭。
CATALOG_REFRESH_MIN_MINUTES = 5
CATALOG_REFRESH_MAX_MINUTES = 1440
#: 新建配置的默认刷新间隔（分钟）。目录过期不会影响转发，但会让 Key 的模型选择器空着。
DEFAULT_CATALOG_REFRESH_MINUTES = 30
#: 后台刷新线程的检查周期（秒）：到点才真抓一次，其余轮次只是看一眼时钟。
CATALOG_REFRESH_TICK_SECONDS = 30.0


class GatewayError(RuntimeError):
    """网关相关错误。消息面向用户，可直接展示。"""


class GatewayKeyNotFound(GatewayError):
    """Key 不存在。与「操作不被允许」区分：路由据此回 404 而不是 400。"""


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def capability_matches(model: Any, key: str) -> bool:
    """某个模型是否满足一个能力键。

    `model` 传入的是 `CatalogModel`（鸭子类型取值，避免与 `gateway_relay` 循环导入）。
    """
    if key == CAPABILITY_FREE:
        return bool(getattr(model, "free", False))
    if key == CAPABILITY_IMAGE:
        return bool(getattr(model, "supports_images", False))
    if key == CAPABILITY_LONG_CONTEXT:
        return int(getattr(model, "context_window", 0) or 0) >= LONG_CONTEXT_TOKENS
    return False


def _clean_capabilities(values: Any) -> tuple[str, ...]:
    """把外部传进来的能力键去重、校验；出现未知键直接报错（不静默丢弃）。"""
    if values is None:
        return ()
    raw = [str(item) for item in values]
    if any(item not in CAPABILITY_KEYS for item in raw):
        raise GatewayError("能力键取值不合法")
    return tuple(dict.fromkeys(raw))


def _clean_limit(value: Any, label: str, maximum: int) -> int:
    """额度取值：0 = 不限；负数、非整数、超上限一律报错，绝不静默改数。"""
    if value is None:
        return 0
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise GatewayError(f"{label}需为整数") from None
    if number < 0 or number > maximum:
        raise GatewayError(f"{label}需在 0-{maximum} 之间（0 = 不限）")
    return number


def _write_json(path: Path, data: Any) -> None:
    write_json_atomic(path, data)


def _read_json(path: Path) -> Any:
    # 保留「缺失 = None」语义；损坏文件会被隔离而不是静默重置。
    return read_json_or_default(path, None, logger=logger)


# ---------- 配置 ----------


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    """网关监听配置。`enabled=False` 表示完全不监听。"""

    enabled: bool = False
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    allow_non_loopback: bool = False
    max_hops: int = DEFAULT_MAX_HOPS
    #: 模型目录自动刷新间隔（分钟）。0 = 关闭；打开时限定 5..1440。
    #: 默认 30：目录属于「配好就不用管」的数据，默认让它自己保鲜，
    #: 否则新建 Key 时选模型会看到空目录。
    catalog_refresh_minutes: int = 30
    #: 请求日志保留条数（50..5000）。调小后立即裁剪内存与落盘。
    log_keep: int = LOG_KEEP

    def validate(self) -> None:
        if not self.host or self.host.strip() != self.host:
            raise GatewayError("监听地址不合法")
        if not (MIN_PORT <= self.port <= MAX_PORT):
            raise GatewayError(f"端口需在 {MIN_PORT}-{MAX_PORT} 之间")
        if not (1 <= self.max_hops <= 8):
            raise GatewayError("换号次数需在 1-8 之间")
        if not (LOG_KEEP_MIN <= self.log_keep <= LOG_KEEP_MAX):
            raise GatewayError(f"日志保留条数需在 {LOG_KEEP_MIN}-{LOG_KEEP_MAX} 之间")
        if self.catalog_refresh_minutes != 0 and not (
            CATALOG_REFRESH_MIN_MINUTES
            <= self.catalog_refresh_minutes
            <= CATALOG_REFRESH_MAX_MINUTES
        ):
            raise GatewayError(
                f"目录自动刷新间隔需为 0（关闭）或 "
                f"{CATALOG_REFRESH_MIN_MINUTES}-{CATALOG_REFRESH_MAX_MINUTES} 分钟"
            )
        if self.host not in LOOPBACK_HOSTS and not self.allow_non_loopback:
            raise GatewayError(
                f"地址 {self.host} 不是回环地址；要对外监听请先显式打开「允许非回环地址」"
            )

    @property
    def base_url(self) -> str:
        host = self.host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    def payload(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "host": self.host,
            "port": self.port,
            "allowNonLoopback": self.allow_non_loopback,
            "maxHops": self.max_hops,
            "catalogRefreshMinutes": self.catalog_refresh_minutes,
            "logKeep": self.log_keep,
            "baseUrl": self.base_url,
            "chatUrl": f"{self.base_url}/v1/chat/completions",
            "modelsUrl": f"{self.base_url}/v1/models",
            "loopbackOnly": self.host in LOOPBACK_HOSTS,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GatewayConfig:
        return cls(
            enabled=bool(data.get("enabled", False)),
            host=str(data.get("host") or DEFAULT_HOST),
            port=int(data.get("port") or DEFAULT_PORT),
            allow_non_loopback=bool(data.get("allowNonLoopback", False)),
            max_hops=int(data.get("maxHops") or DEFAULT_MAX_HOPS),
            catalog_refresh_minutes=int(
                data.get("catalogRefreshMinutes", DEFAULT_CATALOG_REFRESH_MINUTES)
            ),
            log_keep=int(data.get("logKeep", LOG_KEEP)),
        )


# ---------- API Key ----------


@dataclass(slots=True)
class ApiKeyRecord:
    """一条网关 Key。明文不落盘：只有 sha256 摘要与前后缀。

    **模型范围**是三段式（`scope`）：

    - `all`：全部模型，**目录里新上的模型自动跟着可用**（默认，也是老 Key 的语义）；
    - `capability`：当前目录里满足所选 `capabilities` 的模型，**随目录自动重算** ——
      这就是「号池上了新模型不必重签 Key」的实现；
    - `list`：静态白名单 `models`，只有名单里的模型可用。

    兼容老存档：没有 `scope` 字段时，`models` 为空按 `all` 加载、非空按 `list` 加载，
    与旧行为完全一致。
    """

    id: str
    label: str
    prefix: str
    suffix: str
    digest: str
    created_at: str
    last_used_at: str | None = None
    revoked_at: str | None = None
    requests: int = 0
    tokens: int = 0
    models: tuple[str, ...] = ()
    scope: str = SCOPE_ALL
    capabilities: tuple[str, ...] = ()
    #: 限额：0 = 不限。日额度按**本地时区自然日**滚动，RPM 按 60 秒滑动窗口。
    daily_token_limit: int = 0
    daily_request_limit: int = 0
    rpm_limit: int = 0
    #: 当日计数器：`usage_day` 是本地日期 `YYYY-MM-DD`，跨日读到时清零。
    usage_day: str = ""
    day_tokens: int = 0
    day_requests: int = 0

    @property
    def active(self) -> bool:
        return self.revoked_at is None

    @property
    def limited_today(self) -> bool:
        """今日额度是否已经用满（只看日额度；RPM 是瞬时值，不进这个药丸）。"""
        return (
            (self.daily_token_limit > 0 and self.day_tokens >= self.daily_token_limit)
            or (self.daily_request_limit > 0 and self.day_requests >= self.daily_request_limit)
        )

    def allows(self, model: str, catalog: list[Any] | None = None) -> bool:
        """该 Key 是否允许调用某模型。

        `capability` 模式要拿**当前目录**判定（不缓存，保证「动态」名副其实）：
        选中的多个能力是「与」关系（都要满足）；目录里查不到该模型直接拒绝；
        没选任何能力 = 什么都不放行（保守但可解释）。
        """
        if self.scope == SCOPE_LIST:
            return model in self.models
        if self.scope == SCOPE_CAPABILITY:
            if not self.capabilities:
                return False
            entry = next(
                (item for item in (catalog or []) if getattr(item, "id", None) == model),
                None,
            )
            if entry is None:
                return False
            return all(capability_matches(entry, key) for key in self.capabilities)
        return True

    def payload(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "mask": f"{self.prefix}…{self.suffix}",
            "createdAt": self.created_at,
            "lastUsedAt": self.last_used_at,
            "revokedAt": self.revoked_at,
            "requests": self.requests,
            "tokens": self.tokens,
            "active": self.active,
            "models": list(self.models),
            "scope": self.scope,
            "capabilities": list(self.capabilities),
            "dailyTokenLimit": self.daily_token_limit,
            "dailyRequestLimit": self.daily_request_limit,
            "rpmLimit": self.rpm_limit,
            "usageDay": self.usage_day,
            "dayTokens": self.day_tokens,
            "dayRequests": self.day_requests,
            "limitedToday": self.limited_today,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "prefix": self.prefix,
            "suffix": self.suffix,
            "digest": self.digest,
            "createdAt": self.created_at,
            "lastUsedAt": self.last_used_at,
            "revokedAt": self.revoked_at,
            "requests": self.requests,
            "tokens": self.tokens,
            "models": list(self.models),
            "modelScope": self.scope,
            "capabilities": list(self.capabilities),
            "dailyTokenLimit": self.daily_token_limit,
            "dailyRequestLimit": self.daily_request_limit,
            "rpmLimit": self.rpm_limit,
            "usageDay": self.usage_day,
            "dayTokens": self.day_tokens,
            "dayRequests": self.day_requests,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ApiKeyRecord:
        raw_models = data.get("models")
        models = (
            tuple(str(item) for item in raw_models if isinstance(item, str))
            if isinstance(raw_models, list)
            else ()
        )
        raw_scope = data.get("modelScope")
        # 老存档没有 `modelScope`：空白名单 = 全部，非空 = 指定名单。
        scope = raw_scope if raw_scope in KEY_SCOPES else (SCOPE_LIST if models else SCOPE_ALL)
        raw_caps = data.get("capabilities")
        capabilities = (
            tuple(
                dict.fromkeys(
                    str(item)
                    for item in raw_caps
                    if isinstance(item, str) and item in CAPABILITY_KEYS
                )
            )
            if isinstance(raw_caps, list)
            else ()
        )
        return cls(
            id=str(data.get("id") or uuid.uuid4().hex),
            label=str(data.get("label") or "未命名"),
            prefix=str(data.get("prefix") or ""),
            suffix=str(data.get("suffix") or ""),
            digest=str(data.get("digest") or ""),
            created_at=str(data.get("createdAt") or _now_iso()),
            last_used_at=data.get("lastUsedAt"),
            revoked_at=data.get("revokedAt"),
            requests=int(data.get("requests") or 0),
            tokens=int(data.get("tokens") or 0),
            models=models,
            scope=scope,
            capabilities=capabilities,
            daily_token_limit=max(0, int(data.get("dailyTokenLimit") or 0)),
            daily_request_limit=max(0, int(data.get("dailyRequestLimit") or 0)),
            rpm_limit=max(0, int(data.get("rpmLimit") or 0)),
            usage_day=str(data.get("usageDay") or ""),
            day_tokens=max(0, int(data.get("dayTokens") or 0)),
            day_requests=max(0, int(data.get("dayRequests") or 0)),
        )


# ---------- 状态存储 ----------


@dataclass(frozen=True, slots=True)
class AdmitDecision:
    """一次准入判定的结果。`allowed=False` 时带拒绝原因与建议重试秒数。

    `reason`：`quota`（当日 token 额度）/ `requests`（当日请求数）/ `rpm`（每分钟请求数）。
    `limit` / `remaining` 只对「被拒绝的那一项」有意义，直接映射成 `X-RateLimit-*` 响应头。
    """

    allowed: bool
    reason: str = ""
    retry_after: int = 0
    limit: int = 0
    remaining: int = 0


class GatewayStore:
    """网关的磁盘状态：配置 / Key / 请求日志 / 累计计数。

    主服务与网关线程都会读它，所以所有操作都在一把可重入锁里。
    """

    def __init__(
        self,
        home: Path,
        *,
        now: Callable[[], datetime] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config_file = home / CONFIG_FILENAME
        self._keys_file = home / KEYS_FILENAME
        self._logs_file = home / LOGS_FILENAME
        self._stats_file = home / STATS_FILENAME
        self._pool_file = home / POOL_FILENAME
        self._lock = threading.RLock()
        #: 可注入的时钟：跨日滚动与 RPM 窗口都靠它，单测不必真等一天 / 一分钟。
        self._now = now or (lambda: datetime.now().astimezone())
        self._clock = clock or time.monotonic
        #: RPM 滑动窗口：key_id → 最近 60 秒内的准入时刻（monotonic 秒）。
        self._rpm: dict[str, deque[float]] = {}
        self._config = self._load_config()
        self._keys = self._load_keys()
        self._logs = self._load_logs()
        self._pool = self._load_pool()
        self._stats = _read_json(self._stats_file)
        if not isinstance(self._stats, dict):
            self._stats = {}

    # ----- 配置 -----

    def _load_config(self) -> GatewayConfig:
        data = _read_json(self._config_file)
        if not isinstance(data, dict):
            return GatewayConfig()
        try:
            return GatewayConfig.from_dict(data)
        except (TypeError, ValueError):
            return GatewayConfig()

    def config(self) -> GatewayConfig:
        with self._lock:
            return self._config

    def log_keep(self) -> int:
        """当前生效的日志保留条数（配置可改，所以不要再用模块常量）。"""
        with self._lock:
            return int(self._config.log_keep)

    def save_config(self, config: GatewayConfig) -> GatewayConfig:
        config.validate()
        with self._lock:
            self._config = config
            _write_json(self._config_file, config.payload())
            # 调小保留条数要立即生效：内存先裁，落盘再重写一次。
            if len(self._logs) > config.log_keep:
                self._logs = self._logs[-config.log_keep :]
                self._rewrite_logs_locked()
            return config

    # ----- Key -----

    def _load_keys(self) -> list[ApiKeyRecord]:
        data = _read_json(self._keys_file)
        if not isinstance(data, list):
            return []
        return [
            ApiKeyRecord.from_dict(item) for item in data if isinstance(item, dict)
        ]

    def _save_keys_locked(self) -> None:
        _write_json(self._keys_file, [record.to_dict() for record in self._keys])

    def _today(self) -> str:
        """本地时区自然日（`YYYY-MM-DD`）—— 与调度器「用户自己的一天」口径一致。"""
        return self._now().astimezone().date().isoformat()

    @staticmethod
    def _roll_day(record: ApiKeyRecord, today: str) -> bool:
        """把当日计数器滚动到 `today`；跨日清零。返回是否真的改过。"""
        if record.usage_day == today:
            return False
        record.usage_day = today
        record.day_tokens = 0
        record.day_requests = 0
        return True

    def keys(self) -> list[ApiKeyRecord]:
        with self._lock:
            today = self._today()
            changed = False
            for record in self._keys:
                # 不能写 any(...)：生成器短路会让后面的 Key 漏滚动。
                changed = self._roll_day(record, today) or changed
            if changed:
                self._save_keys_locked()
            return list(self._keys)

    def active_key_count(self) -> int:
        with self._lock:
            return sum(1 for record in self._keys if record.active)

    def issue(
        self,
        label: str,
        models: list[str] | None = None,
        *,
        scope: str | None = None,
        capabilities: list[str] | None = None,
        daily_token_limit: int = 0,
        daily_request_limit: int = 0,
        rpm_limit: int = 0,
    ) -> tuple[str, ApiKeyRecord]:
        """签发一把新 Key。返回 `(明文, 记录)` —— 明文只有这一次机会拿到。

        `scope` 决定模型范围：`all`（全部）/ `capability`（按能力）/ `list`（指定
        `models` 白名单）。不传时按老语义推断：给了 `models` 当 `list`，否则当 `all`。
        `models` 无论哪种模式都存着，方便之后切换模式。
        三个额度参数 0 = 不限。
        """
        name = (label or "").strip() or "未命名"
        if len(name) > 60:
            raise GatewayError("备注最长 60 个字符")
        whitelist = tuple(dict.fromkeys(m.strip() for m in (models or []) if m.strip()))
        resolved = scope if scope is not None else (SCOPE_LIST if whitelist else SCOPE_ALL)
        if resolved not in KEY_SCOPES:
            raise GatewayError("模型范围取值不合法")
        caps = _clean_capabilities(capabilities)
        token_limit = _clean_limit(daily_token_limit, "每日 token 上限", LIMIT_MAX)
        request_limit = _clean_limit(daily_request_limit, "每日请求上限", LIMIT_MAX)
        rpm = _clean_limit(rpm_limit, "每分钟请求上限", RPM_LIMIT_MAX)
        body = token_hex(24)
        token = f"{KEY_PREFIX}{body}"
        record = ApiKeyRecord(
            id=uuid.uuid4().hex,
            label=name,
            prefix=token[: len(KEY_PREFIX) + 6],
            suffix=token[-4:],
            digest=sha256(token.encode("utf-8")).hexdigest(),
            created_at=_now_iso(),
            models=whitelist,
            scope=resolved,
            capabilities=caps,
            daily_token_limit=token_limit,
            daily_request_limit=request_limit,
            rpm_limit=rpm,
            usage_day=self._today(),
        )
        with self._lock:
            self._keys.insert(0, record)
            self._save_keys_locked()
        return token, record

    def update_key(
        self,
        key_id: str,
        *,
        label: str | None = None,
        scope: str | None = None,
        capabilities: list[str] | None = None,
        models: list[str] | None = None,
        daily_token_limit: int | None = None,
        daily_request_limit: int | None = None,
        rpm_limit: int | None = None,
    ) -> ApiKeyRecord:
        """改一把 Key 的备注 / 模型范围 / 额度。**C 位与明文都不变**，所以不必重签。

        只改传进来的字段（`None` = 保持原样）。改完立即生效。
        """
        # 先把新值全部验完再落笔：非法额度不能留下「改了一半」的 Key。
        token_limit = (
            None
            if daily_token_limit is None
            else _clean_limit(daily_token_limit, "每日 token 上限", LIMIT_MAX)
        )
        request_limit = (
            None
            if daily_request_limit is None
            else _clean_limit(daily_request_limit, "每日请求上限", LIMIT_MAX)
        )
        rpm = (
            None
            if rpm_limit is None
            else _clean_limit(rpm_limit, "每分钟请求上限", RPM_LIMIT_MAX)
        )
        with self._lock:
            for record in self._keys:
                if record.id != key_id:
                    continue
                if label is not None:
                    name = (label or "").strip()
                    if len(name) > 60:
                        raise GatewayError("备注最长 60 个字符")
                    if name:
                        record.label = name
                if scope is not None:
                    if scope not in KEY_SCOPES:
                        raise GatewayError("模型范围取值不合法")
                    record.scope = scope
                if capabilities is not None:
                    record.capabilities = _clean_capabilities(capabilities)
                if models is not None:
                    record.models = tuple(
                        dict.fromkeys(m.strip() for m in models if m.strip())
                    )
                if token_limit is not None:
                    record.daily_token_limit = token_limit
                if request_limit is not None:
                    record.daily_request_limit = request_limit
                if rpm is not None:
                    record.rpm_limit = rpm
                self._save_keys_locked()
                return record
        raise GatewayKeyNotFound("Key 不存在")

    def revoke(self, key_id: str) -> ApiKeyRecord:
        """停用一把 Key：记录与用量都留着，之后可「恢复」或「删除」。"""
        with self._lock:
            for record in self._keys:
                if record.id == key_id:
                    if record.active:
                        record.revoked_at = _now_iso()
                        self._save_keys_locked()
                    return record
        raise GatewayKeyNotFound("Key 不存在")

    def restore(self, key_id: str) -> ApiKeyRecord:
        """把一把已吊销的 Key 恢复成有效。未被吊销的 Key 无需恢复。"""
        with self._lock:
            for record in self._keys:
                if record.id == key_id:
                    if record.active:
                        raise GatewayError("这把 Key 未被吊销，无需恢复")
                    record.revoked_at = None
                    self._save_keys_locked()
                    return record
        raise GatewayKeyNotFound("Key 不存在")

    def delete(self, key_id: str) -> ApiKeyRecord:
        """把一条**已吊销**的 Key 记录彻底删掉，列表里不再出现。

        三态的关系：有效 →（吊销）→ 已吊销 →（删除）→ 不存在。
        删除必须先吊销：有效 Key 直接删掉等于「没留痕的紧急停用」，
        容易掩盖误操作，所以这里拒绝并让调用方先吊销。
        """
        with self._lock:
            for index, record in enumerate(self._keys):
                if record.id == key_id:
                    if record.active:
                        raise GatewayError("这把 Key 还在有效状态；请先吊销，再删除")
                    self._keys.pop(index)
                    self._save_keys_locked()
                    return record
        raise GatewayKeyNotFound("Key 不存在")

    def authenticate(self, token: str | None) -> ApiKeyRecord | None:
        """校验请求里的 Bearer / x-api-key。未签发或已吊销都返回 None。"""
        raw = (token or "").strip()
        if not raw or not raw.startswith(KEY_PREFIX):
            return None
        digest = sha256(raw.encode("utf-8")).hexdigest()
        with self._lock:
            for record in self._keys:
                if record.active and compare_digest(digest, record.digest):
                    return record
        return None

    def admit(self, key_id: str) -> AdmitDecision:
        """请求准入判定：日 token / 日请求 / 每分钟请求三道闸。

        - 先按**本地时区自然日**滚动计数器（跨日清零）；
        - 日额度看「已经用掉多少」：达到上限即拒绝，回复次日 0 点重试；
        - RPM 用 60 秒滑动窗口（per-key，内存），超限回复窗口内最早那次滑出的秒数；
        - 放行时**立刻记一次**窗口打点：失败请求也占 RPM（与 `record_use` 口径一致）。

        注意：token 额度只能拦住「下一次」请求 —— 一次请求要花多少 token
        在发出去之前无法预知（见方案 D7）。
        """
        with self._lock:
            record = next((item for item in self._keys if item.id == key_id), None)
            if record is None:
                raise GatewayKeyNotFound(f"找不到网关 Key：{key_id}")
            if self._roll_day(record, self._today()):
                self._save_keys_locked()

            if record.daily_token_limit > 0 and record.day_tokens >= record.daily_token_limit:
                return AdmitDecision(
                    False, "quota", self._seconds_to_tomorrow(), record.daily_token_limit, 0
                )
            if (
                record.daily_request_limit > 0
                and record.day_requests >= record.daily_request_limit
            ):
                return AdmitDecision(
                    False, "requests", self._seconds_to_tomorrow(), record.daily_request_limit, 0
                )
            if record.rpm_limit > 0:
                moment = self._clock()
                window = self._rpm.setdefault(record.id, deque())
                while window and moment - window[0] >= RPM_WINDOW_SECONDS:
                    window.popleft()
                if len(window) >= record.rpm_limit:
                    wait = RPM_WINDOW_SECONDS - (moment - window[0])
                    return AdmitDecision(
                        False, "rpm", max(1, ceil(wait)), record.rpm_limit, 0
                    )
                window.append(moment)
            return AdmitDecision(True)

    def _seconds_to_tomorrow(self) -> int:
        """距本地时区次日 0 点的秒数（日额度重置点）；至少 1 秒。"""
        now = self._now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        return max(1, ceil((start - now).total_seconds()))

    def record_use(self, key_id: str, *, tokens: int = 0) -> None:
        with self._lock:
            today = self._today()
            for record in self._keys:
                if record.id == key_id:
                    self._roll_day(record, today)
                    record.last_used_at = _now_iso()
                    record.requests += 1
                    record.tokens += max(0, tokens)
                    record.day_requests += 1
                    record.day_tokens += max(0, tokens)
                    self._save_keys_locked()
                    return

    # ----- 账号池：排除 / 指定偏好 -----

    def _load_pool(self) -> dict[str, Any]:
        """账号池的人工处置：排除集 + 指定偏好账号。落盘，跨重启保留。"""
        data = _read_json(self._pool_file)
        excluded = data.get("excluded") if isinstance(data, dict) else None
        preferred = data.get("preferred") if isinstance(data, dict) else None
        return {
            "excluded": (
                [str(item) for item in excluded if isinstance(item, str)]
                if isinstance(excluded, list)
                else []
            ),
            "preferred": preferred if isinstance(preferred, str) else None,
        }

    def _save_pool_locked(self) -> None:
        _write_json(self._pool_file, self._pool)

    def excluded_accounts(self) -> set[str]:
        """被手工排除的账号 id。"""
        with self._lock:
            return set(self._pool["excluded"])

    def is_excluded(self, account_id: str) -> bool:
        with self._lock:
            return account_id in self._pool["excluded"]

    def exclude(self, account_id: str) -> set[str]:
        """把账号排除出转发候选（人工操作，落盘保留）。"""
        with self._lock:
            if account_id not in self._pool["excluded"]:
                self._pool["excluded"].append(account_id)
                self._save_pool_locked()
            return set(self._pool["excluded"])

    def include(self, account_id: str) -> set[str]:
        """一键复活：把被排除的账号放回候选。"""
        with self._lock:
            if account_id in self._pool["excluded"]:
                self._pool["excluded"].remove(account_id)
                self._save_pool_locked()
            return set(self._pool["excluded"])

    def preferred_account(self) -> str | None:
        """指定账号偏好：它可用时优先被挑中；不可用时回退常规择优。"""
        with self._lock:
            return self._pool["preferred"]

    def set_preferred(self, account_id: str | None) -> str | None:
        """设置 / 取消指定账号偏好（`None` = 取消）。"""
        name = account_id or None
        with self._lock:
            if self._pool["preferred"] != name:
                self._pool["preferred"] = name
                self._save_pool_locked()
            return self._pool["preferred"]

    def pool_payload(self) -> dict[str, Any]:
        with self._lock:
            return {
                "excluded": sorted(self._pool["excluded"]),
                "preferred": self._pool["preferred"],
            }

    def forget_account(self, account_id: str) -> bool:
        """把已永久删除的账号从账号池设置里摘掉。

        账号彻底删除后，``excluded`` 里留一个永不命中的 id 无害、``preferred``
        指向一个不存在的账号则是「永远找不到指定号」的幽灵偏好。有变化才落盘，
        返回是否真的改过。
        """
        with self._lock:
            changed = False
            if account_id in self._pool["excluded"]:
                self._pool["excluded"].remove(account_id)
                changed = True
            if self._pool["preferred"] == account_id:
                self._pool["preferred"] = None
                changed = True
            if changed:
                self._save_pool_locked()
            return changed

    # ----- 请求日志 -----

    def _load_logs(self) -> list[dict[str, Any]]:
        if not self._logs_file.is_file():
            return []
        entries: list[dict[str, Any]] = []
        try:
            with self._logs_file.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        item = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(item, dict):
                        entries.append(item)
        except OSError:
            return []
        return entries[-self.log_keep() :]

    def log(self, entry: dict[str, Any]) -> dict[str, Any]:
        """记一笔网关请求。返回写入后的条目（含 id 与时间）。"""
        record = {
            "id": uuid.uuid4().hex,
            "at": _now_iso(),
            **entry,
        }
        with self._lock:
            try:
                self._logs_file.parent.mkdir(parents=True, exist_ok=True)
                with self._logs_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                logger.warning("网关日志写入失败", exc_info=True)
            self._logs.append(record)
            keep = self._config.log_keep
            if len(self._logs) > keep:
                self._logs = self._logs[-keep:]
                self._rewrite_logs_locked()
            self._bump_stats_locked(record)
        return record

    def _rewrite_logs_locked(self) -> None:
        try:
            with self._logs_file.open("w", encoding="utf-8") as handle:
                for item in self._logs:
                    handle.write(json.dumps(item, ensure_ascii=False) + "\n")
        except OSError:
            logger.warning("网关日志裁剪失败", exc_info=True)

    def logs(
        self,
        limit: int = 50,
        *,
        key_id: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """最近的请求日志（新的在前）。

        `key_id` / `session_id` 是**内存里的等值过滤**（不读盘、不改落盘格式）：
        日志保留条数可配（默认 200），扫一遍内存比先读文件再筛更省事。
        先过滤再截取，所以拿到的是「最近的 N 条**命中项**」，不是「最近 N 条里命中的」。
        """
        with self._lock:
            items = self._logs
            if key_id is not None:
                items = [item for item in items if item.get("keyId") == key_id]
            if session_id is not None:
                items = [item for item in items if item.get("sessionId") == session_id]
            recent = items[-max(1, limit) :]
        return list(reversed(recent))

    def key_usage(self, key_id: str, *, limit: int = 20) -> dict[str, Any]:
        """单把 Key 的用量视图：累计值 + 采样窗口内的分解 + 最近请求。

        两个口径显式分开（界面也要分开标注）：
        - `total`：Key 记录上的**累计**计数（跨日志保留期，来自 `record_use`）；
        - `sample`：只用**当前保留的日志**算出来，日志容量有限，不能当全量。
        """
        record = next((item for item in self.keys() if item.id == key_id), None)
        if record is None:
            raise GatewayKeyNotFound(f"找不到网关 Key：{key_id}")

        with self._lock:
            entries = [
                item
                for item in self._logs
                if item.get("keyId") == key_id and item.get("kind") != "probe"
            ]

        by_model: dict[str, dict[str, Any]] = {}
        buckets: dict[str, dict[str, Any]] = {}
        ok_count = 0
        duration_sum = 0
        duration_count = 0
        for entry in entries:
            ok = bool(entry.get("ok"))
            tokens = int(entry.get("totalTokens") or 0)
            model = str(entry.get("model") or "—")
            if ok:
                ok_count += 1
            slot = by_model.setdefault(
                model, {"model": model, "requests": 0, "tokens": 0, "errors": 0}
            )
            slot["requests"] += 1
            slot["tokens"] += tokens
            if not ok:
                slot["errors"] += 1
            duration = entry.get("durationMs")
            if isinstance(duration, int) and duration >= 0:
                duration_sum += duration
                duration_count += 1
            bucket = _hour_bucket(entry.get("at"))
            if bucket:
                item = buckets.setdefault(
                    bucket, {"bucket": bucket, "requests": 0, "tokens": 0, "errors": 0}
                )
                item["requests"] += 1
                item["tokens"] += tokens
                if not ok:
                    item["errors"] += 1

        total = len(entries)
        recent = list(reversed(entries[-max(1, limit) :]))
        return {
            "key": record.payload(),
            "sample": {
                "scope": self.log_keep(),
                "requests": total,
                "ok": ok_count,
                "failed": total - ok_count,
                "tokens": sum(item["tokens"] for item in by_model.values()),
                "avgDurationMs": int(duration_sum / duration_count) if duration_count else None,
            },
            "byModel": sorted(by_model.values(), key=lambda x: -x["requests"]),
            "series": [buckets[key] for key in sorted(buckets)],
            "recent": recent,
        }

    def clear_logs(self) -> int:
        with self._lock:
            removed = len(self._logs)
            self._logs = []
            try:
                self._logs_file.unlink(missing_ok=True)
            except OSError:
                logger.warning("网关日志清空失败", exc_info=True)
            return removed

    # ----- 计数 -----

    def _bump_stats_locked(self, entry: dict[str, Any]) -> None:
        if entry.get("kind") == "probe":
            # 账号探测是运维动作、不是客户端的业务请求：进日志，但不进累计计数。
            return
        stats = self._stats
        stats["requests"] = int(stats.get("requests") or 0) + 1
        if not entry.get("ok"):
            stats["errors"] = int(stats.get("errors") or 0) + 1
        stats["promptTokens"] = int(stats.get("promptTokens") or 0) + int(
            entry.get("promptTokens") or 0
        )
        stats["completionTokens"] = int(stats.get("completionTokens") or 0) + int(
            entry.get("completionTokens") or 0
        )
        stats["lastRequestAt"] = entry.get("at")
        try:
            _write_json(self._stats_file, stats)
        except OSError:
            logger.warning("网关计数写入失败", exc_info=True)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            stats = self._stats
            prompt = int(stats.get("promptTokens") or 0)
            completion = int(stats.get("completionTokens") or 0)
            return {
                "requests": int(stats.get("requests") or 0),
                "errors": int(stats.get("errors") or 0),
                "promptTokens": prompt,
                "completionTokens": completion,
                "totalTokens": prompt + completion,
                "lastRequestAt": stats.get("lastRequestAt"),
            }

    def breakdown(self) -> dict[str, Any]:
        """从**最近保留的日志**派生看板数据。

        口径必须与日志保留上限一致：这里给的是「最近 N 条请求」（N = `log_keep`）
        的分解，不是全量统计 —— 界面要如实标注，别谎报成累计值。
        账号探测（`kind=probe`）是运维动作，进日志但不进看板，免得污染成功率。

        `byKey` 是「每把下发的 Key 在这批采样里用了多少」，与 Key 记录上的
        `requests` / `tokens`（**累计**，界面照原样展示）是两个不同口径，别混。
        """
        with self._lock:
            entries = [item for item in self._logs if item.get("kind") != "probe"]

        by_model: dict[str, dict[str, Any]] = {}
        by_account: dict[str, dict[str, Any]] = {}
        by_key: dict[str, dict[str, Any]] = {}
        buckets: dict[str, dict[str, Any]] = {}
        total = len(entries)
        ok_count = 0
        duration_sum = 0
        duration_count = 0
        total_hops = 0

        for entry in entries:
            ok = bool(entry.get("ok"))
            model = str(entry.get("model") or "—")
            tokens = int(entry.get("totalTokens") or 0)
            hops = int(entry.get("hops") or 0)
            account_id = str(entry.get("accountId") or "")
            account_name = str(entry.get("accountName") or "无账号")

            slot = by_model.setdefault(
                model, {"model": model, "requests": 0, "tokens": 0, "errors": 0}
            )
            slot["requests"] += 1
            slot["tokens"] += tokens
            if not ok:
                slot["errors"] += 1

            acc = by_account.setdefault(
                account_id,
                {
                    "accountId": account_id,
                    "accountName": account_name,
                    "requests": 0,
                    "tokens": 0,
                    "ok": 0,
                    "failed": 0,
                    "hops": 0,
                },
            )
            acc["requests"] += 1
            acc["tokens"] += tokens
            acc["hops"] += hops
            acc["ok" if ok else "failed"] += 1

            key_slot = by_key.setdefault(
                str(entry.get("keyId") or ""),
                {
                    "keyId": str(entry.get("keyId") or ""),
                    "keyLabel": str(entry.get("keyLabel") or "—"),
                    "requests": 0,
                    "tokens": 0,
                    "ok": 0,
                    "failed": 0,
                },
            )
            key_slot["requests"] += 1
            key_slot["tokens"] += tokens
            key_slot["ok" if ok else "failed"] += 1

            if ok:
                ok_count += 1
            total_hops += hops
            duration = entry.get("durationMs")
            if isinstance(duration, int) and duration >= 0:
                duration_sum += duration
                duration_count += 1

            bucket = _hour_bucket(entry.get("at"))
            if bucket:
                item = buckets.setdefault(
                    bucket, {"bucket": bucket, "requests": 0, "tokens": 0, "errors": 0}
                )
                item["requests"] += 1
                item["tokens"] += tokens
                if not ok:
                    item["errors"] += 1

        return {
            "scope": self.log_keep(),
            "sampledAt": _now_iso(),
            "total": total,
            "byModel": sorted(by_model.values(), key=lambda x: -x["requests"]),
            "byAccount": sorted(by_account.values(), key=lambda x: -x["requests"]),
            "byKey": sorted(by_key.values(), key=lambda x: -x["requests"]),
            "series": [buckets[key] for key in sorted(buckets)],
            "successRate": round(ok_count / total, 4) if total else None,
            "avgDurationMs": round(duration_sum / duration_count) if duration_count else None,
            "totalHops": total_hops,
        }


def _hour_bucket(value: Any) -> str:
    """把 ISO 时间截成 `YYYY-MM-DDTHH:00Z` 小时桶；解析不了返回空串。"""
    if not isinstance(value, str) or len(value) < 13:
        return ""
    text = value.replace("+00:00", "Z")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return ""
    return moment.replace(minute=0, second=0, microsecond=0).isoformat().replace("+00:00", "Z")


# ---------- 生命周期 ----------


class GatewayRuntime:
    """把网关端口的 uvicorn 跑在一个后台线程里，与主服务互不干扰。

    只有 `config.enabled` 为真且地址校验通过时才监听；配置一改就热重启。
    """

    def __init__(self, store: GatewayStore) -> None:
        self._store = store
        self._lock = threading.Lock()
        self._server: Any | None = None
        self._thread: threading.Thread | None = None
        self._error: str | None = None

    @property
    def running(self) -> bool:
        server = self._server
        return bool(server is not None and getattr(server, "started", False))

    @property
    def error(self) -> str | None:
        return self._error

    def start(self, app_factory: Callable[[], Any]) -> None:
        config = self._store.config()
        if not config.enabled:
            return
        config.validate()

        import uvicorn

        server = uvicorn.Server(
            uvicorn.Config(
                app_factory(),
                host=config.host,
                port=config.port,
                # log_config=None：别让网关线程抢走主服务的日志配置。
                log_config=None,
                log_level="warning",
                access_log=False,
            )
        )
        thread = threading.Thread(target=server.run, name="chaierww-gateway", daemon=True)
        thread.start()

        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline and not server.started and thread.is_alive():
            time.sleep(0.05)
        if not server.started:
            server.should_exit = True
            thread.join(timeout=2)
            self._error = f"端口 {config.port} 无法监听（可能已被占用）"
            raise GatewayError(self._error)

        with self._lock:
            self._server = server
            self._thread = thread
        self._error = None

    def stop(self) -> None:
        with self._lock:
            server, thread = self._server, self._thread
            self._server = None
            self._thread = None
        if server is None:
            return
        server.should_exit = True
        if thread is not None:
            thread.join(timeout=5)

    def apply(self, app_factory: Callable[[], Any]) -> None:
        """配置变更后按需重启。失败时记下原因但不抛出（配置已落库，界面要能看到原因）。"""
        self.stop()
        try:
            self.start(app_factory)
        except GatewayError as exc:
            self._error = str(exc)
            logger.warning("网关启动失败：%s", exc)

    def status(self) -> dict[str, Any]:
        config = self._store.config()
        return {
            **config.payload(),
            "running": self.running,
            "error": self._error,
        }


class CatalogRefresher:
    """按配置间隔在后台刷新模型目录的单线程。

    `interval_minutes` 与 `fetcher` 都是可调用对象：间隔从配置**现读**（改设置即生效），
    抓取本身由 API 层注入（内部自己 `asyncio.run`）—— 本模块不发任何网络请求。
    间隔为 0 时只空转（不抓）；一旦打开或改动间隔就**立刻刷一次**，之后按新间隔排下一轮。
    单次失败只记日志，绝不退出线程（否则一次网络抖动就永久失去自动刷新）。
    """

    def __init__(
        self,
        interval_minutes: Callable[[], int],
        fetcher: Callable[[], Any],
        *,
        tick_seconds: float = CATALOG_REFRESH_TICK_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._interval_minutes = interval_minutes
        self._fetcher = fetcher
        self._tick_seconds = tick_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._next_at: float | None = None
        self._schedule_minutes = 0

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="chaierww-catalog", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self._tick_seconds + 2)

    def _loop(self) -> None:
        while not self._stop.wait(self._tick_seconds):
            self.tick()

    def tick(self) -> None:
        """到点就抓一次。公开出来是为了让单测能直接驱动，不必真等一个 tick。"""
        minutes = int(self._interval_minutes() or 0)
        now = self._clock()
        if minutes <= 0:
            self._schedule_minutes = 0
            self._next_at = None
            return
        if minutes != self._schedule_minutes or self._next_at is None:
            # 刚打开或间隔被改动：立刻刷一次，再按新间隔排下一轮。
            self._schedule_minutes = minutes
            self._next_at = now
        if now < self._next_at:
            return
        try:
            self._fetcher()
        except Exception:  # noqa: BLE001 - 后台线程不能被单次失败带崩
            logger.warning("模型目录自动刷新失败", exc_info=True)
        finally:
            self._next_at = now + minutes * 60
