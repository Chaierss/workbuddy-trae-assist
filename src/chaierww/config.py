from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "chaierww"
HOME_ENV = "CHAIERWW_HOME"
#: 监听端口覆盖。只覆盖端口不覆盖 host —— 服务始终只监听回环地址，远程访问走 SSH 隧道。
PORT_ENV = "CHAIERWW_PORT"
DEFAULT_PORT = 57910

DB_FILENAME = "chaierww.db"

# 引导安装：各客户端官方下载链接（已内置邀请码）。
# 未安装时号池页展示「提示 + 引导下载链接」，链接从这里取。
DOWNLOAD_LINKS: dict[str, str] = {
    "workbuddy": "https://www.workbuddy.cn/events/invite/?inviteCode=gg5iv39v9w",
    "trae": (
        "https://www.trae.cn/events/code-fission/FAXHZCBRK8Y5"
        "?utm_source=copy_link&utm_medium=code_fission"
    ),
}


@dataclass(frozen=True)
class Settings:
    """运行期配置。

    数据目录优先级：显式传入 > 环境变量 > 用户家目录下的默认位置。
    所有派生路径都从 home 推导，避免出现"配置了一半"的状态。
    """

    home: Path
    host: str = "127.0.0.1"
    port: int = DEFAULT_PORT
    recycle_days: int = 30
    # 本机 `/api/*` 守卫。显式构造默认关闭（测试/嵌入式），生产入口
    # `load_settings()` 默认开启，可用 `CHAIERWW_API_GUARD=0` 关闭。
    api_guard: bool = False
    # 每个客户端作用域保留的常驻备份份数（按次数滚动，与 domain/backup.py 的
    # KEEP_SNAPSHOTS 一致）。常驻备份 = 每日自动 + 手动；切换前/恢复前的临时
    # 备份在流程成功后即删除，不占用这里的配额。
    snapshot_keep: int = 7

    @property
    def db_path(self) -> Path:
        return self.home / DB_FILENAME

    @property
    def backups_dir(self) -> Path:
        return self.home / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def avatars_dir(self) -> Path:
        return self.home / "avatars"

    @property
    def api_token_path(self) -> Path:
        """本机 API 令牌文件；外部脚本用它走 Bearer。"""
        return self.home / "api-token"

    @property
    def pre_migrate_dir(self) -> Path:
        """数据库迁移前的自动备份目录。"""
        return self.backups_dir / "pre-migrate"

    def ensure_dirs(self) -> None:
        for path in (self.home, self.backups_dir, self.logs_dir, self.avatars_dir):
            path.mkdir(parents=True, exist_ok=True)


def default_home() -> Path:
    override = os.environ.get(HOME_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / f".{APP_NAME}"


def port_from_env() -> int:
    """监听端口，可用 ``CHAIERWW_PORT`` 覆盖（同机多实例 / 端口冲突时用）。

    非法值直接抛 ``ValueError``，不静默回落到默认端口：静默回落会把「我明明设了 58000」
    变成最难排查的一类问题，而启动失败会写进 startup-error 文件让用户看得见。
    """
    raw = os.environ.get(PORT_ENV)
    if raw is None or not raw.strip():
        return DEFAULT_PORT
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{PORT_ENV}={raw!r} 不是整数") from exc
    if not 1 <= value <= 65535:
        raise ValueError(f"{PORT_ENV}={value} 超出端口范围 1-65535")
    return value


def load_settings(home: Path | str | None = None) -> Settings:
    resolved = Path(home).expanduser() if home is not None else default_home()
    guard = os.environ.get("CHAIERWW_API_GUARD", "1").strip() != "0"
    return Settings(home=resolved, port=port_from_env(), api_guard=guard)
