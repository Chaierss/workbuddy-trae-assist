"""上游兼容雷达：客户端版本 + 登录态**结构指纹**。

只做三件事：

1. 从适配器拿一次只读探测结果（版本 / 关键路径 / 结构指纹）；
2. 把指纹落进 ``compat_snapshots``，同一份结构只留首末时间，不重复膨胀；
3. 对比「当前指纹」与「历史指纹」，告诉界面这是 ``compatible`` 还是 ``changed``。

**指纹不包含任何值**：只递归收集 JSON 的键路径与类型再排序取哈希。令牌、昵称、
手机号都只可能出现在值里，因此既不会进库，也不会进日志。

行为边界（与设计文档一致）：只提示、不改写、不自动适配；结构变化时续期照常，
由既有的退避 / 熔断兜底。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chaierww.infra.db import now_iso, transaction

#: 指纹取 sha256 前 16 位十六进制：足够区分结构，又比全长好读。
FINGERPRINT_LENGTH = 16
#: 单份文档最多收集多少条键路径：防御异常大的文件把探测拖慢。
MAX_PATHS = 4000
#: 手动「立即探测」的去重窗口（秒），防止连点把上游/磁盘刷爆。
MANUAL_PROBE_INTERVAL = 300.0

COMPAT_COMPATIBLE = "compatible"
COMPAT_CHANGED = "changed"
COMPAT_MISSING = "missing"
COMPAT_UNKNOWN = "unknown"
COMPAT_NOT_PROBED = "not_probed"


@dataclass(frozen=True, slots=True)
class ClientProbe:
    """一次只读探测的结论（不含任何凭据内容）。"""

    platform: str
    scope: str
    client_version: str = ""
    fingerprint: str = ""
    state_paths_ok: bool = False
    missing_paths: tuple[str, ...] = ()
    detail: str = ""

    def payload(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "scope": self.scope,
            "clientVersion": self.client_version,
            "fingerprint": self.fingerprint,
            "statePathsOk": self.state_paths_ok,
            "missingPaths": list(self.missing_paths),
            "detail": self.detail,
        }


def structure_paths(document: Any, *, max_paths: int = MAX_PATHS) -> list[str]:
    """递归收集「键路径 + 类型」，排序去重。值一律不进入结果。

    列表只贡献一层 ``[]`` 标记并继续深入元素：既不会因为账号条数变化而误报
    结构变化，也能感知「列表元素的键变了」这种真实变更。
    """
    found: set[str] = set()

    def walk(node: Any, prefix: str) -> None:
        if len(found) >= max_paths:
            return
        if isinstance(node, dict):
            for key in node:
                path = f"{prefix}.{key}" if prefix else str(key)
                found.add(f"{path}:dict" if isinstance(node[key], dict) else path)
                walk(node[key], path)
        elif isinstance(node, list):
            found.add(f"{prefix}[]")
            for item in node:
                walk(item, prefix)

    walk(document, "")
    return sorted(found)


def fingerprint_of(document: Any) -> str:
    """结构指纹：键路径排序拼串后取 sha256 前 16 位。"""
    digest = hashlib.sha256("\n".join(structure_paths(document)).encode("utf-8"))
    return digest.hexdigest()[:FINGERPRINT_LENGTH]


def fingerprint_of_files(paths: list[Path]) -> str:
    """给单测用的小工具：把若干 JSON 文件的结构合起来算指纹。"""
    documents: list[Any] = []
    for path in paths:
        try:
            documents.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            documents.append(None)
    return fingerprint_of(documents)


def _row_payload(row: sqlite3.Row) -> dict[str, Any]:
    missing: list[str] = []
    try:
        parsed = json.loads(row["missing_paths"])
        if isinstance(parsed, list):
            missing = [str(item) for item in parsed]
    except (TypeError, ValueError):
        missing = []
    return {
        "platform": row["platform"],
        "scope": row["scope"],
        "clientVersion": row["client_version"],
        "fingerprint": row["fingerprint"],
        "statePathsOk": bool(row["state_paths_ok"]),
        "missingPaths": missing,
        "firstSeenAt": row["first_seen_at"],
        "lastSeenAt": row["last_seen_at"],
    }


class CompatService:
    """上游兼容雷达的服务层：探测 → 落库 → 对比。

    与适配器解耦：只要求适配器提供 ``probe_client()``；没有该方法的作用域直接
    跳过（老适配器照常工作，不会因为本功能变成不可用）。
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        adapters: dict[str, Any],
        *,
        lock: threading.RLock | None = None,
        clock: Any = time.monotonic,
    ) -> None:
        self._conn = conn
        self._adapters = adapters
        self._lock = lock or threading.RLock()
        self._clock = clock
        self._last_manual: float | None = None

    # ---------- 探测 ----------

    def probe(self, *, manual: bool = False, force: bool = False) -> dict[str, Any]:
        """探测所有支持的作用域并落库。

        ``manual=True`` 时 5 分钟内只真正执行一次（连点去重），返回上次结果。
        """
        if (
            manual
            and not force
            and self._last_manual is not None
            and self._clock() - self._last_manual < MANUAL_PROBE_INTERVAL
        ):
            return self.overview(probed=False, deduped=True)
        if manual:
            self._last_manual = self._clock()

        probes = self._run_probes()
        new_scopes: set[str] = set()
        for probe in probes:
            if probe.fingerprint and probe.state_paths_ok and self._record(probe):
                new_scopes.add(probe.scope)
        return self.overview(probes=probes, probed=True, new_scopes=new_scopes)

    def _run_probes(self) -> list[ClientProbe]:
        probes: list[ClientProbe] = []
        for scope, adapter in sorted(self._adapters.items()):
            runner = getattr(adapter, "probe_client", None)
            if runner is None:
                continue
            try:
                probes.append(runner())
            except Exception as exc:  # noqa: BLE001 - 单个平台探测失败不影响另一个
                probes.append(
                    ClientProbe(
                        platform=_platform_name(scope),
                        scope=scope,
                        detail=f"探测异常：{type(exc).__name__}",
                    )
                )
        return probes

    # ---------- 落库 ----------

    def _record(self, probe: ClientProbe) -> bool:
        """同一 ``(platform, scope, fingerprint)`` 只更新末次时间；返回是否新结构。"""
        stamp = now_iso()
        missing = json.dumps(list(probe.missing_paths), ensure_ascii=False)
        with self._lock, transaction(self._conn):
            row = self._conn.execute(
                """
                SELECT id FROM compat_snapshots
                 WHERE platform = ? AND scope = ? AND fingerprint = ?
                """,
                (probe.platform, probe.scope, probe.fingerprint),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """
                    INSERT INTO compat_snapshots (
                        platform, scope, client_version, fingerprint,
                        state_paths_ok, missing_paths, first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        probe.platform,
                        probe.scope,
                        probe.client_version,
                        probe.fingerprint,
                        int(bool(probe.state_paths_ok)),
                        missing,
                        stamp,
                        stamp,
                    ),
                )
                return True
            self._conn.execute(
                """
                UPDATE compat_snapshots
                   SET last_seen_at = ?,
                       client_version = ?,
                       state_paths_ok = ?,
                       missing_paths = ?
                 WHERE id = ?
                """,
                (stamp, probe.client_version, int(bool(probe.state_paths_ok)), missing, row["id"]),
            )
        return False

    def latest(self, platform: str, scope: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM compat_snapshots
                 WHERE platform = ? AND scope = ?
                 ORDER BY last_seen_at DESC, id DESC LIMIT 1
                """,
                (platform, scope),
            ).fetchone()
        return _row_payload(row) if row is not None else None

    def history(self, platform: str, scope: str, *, limit: int = 20) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM compat_snapshots
                 WHERE platform = ? AND scope = ?
                 ORDER BY last_seen_at DESC, id DESC LIMIT ?
                """,
                (platform, scope, max(1, limit)),
            ).fetchall()
        return [_row_payload(row) for row in rows]

    # ---------- 汇总 ----------

    def overview(
        self,
        *,
        probes: list[ClientProbe] | None = None,
        probed: bool = False,
        deduped: bool = False,
        new_scopes: set[str] | None = None,
    ) -> dict[str, Any]:
        current = {probe.scope: probe for probe in (probes or [])}
        scopes = sorted(set(self._adapters) | {p.scope for p in (probes or [])})
        items: list[dict[str, Any]] = []
        for scope in scopes:
            platform = _platform_name(scope)
            probe = current.get(scope)
            latest = self.latest(platform, scope)
            history = self.history(platform, scope, limit=10)
            state = COMPAT_NOT_PROBED
            detail = ""
            if probe is not None:
                detail = probe.detail
                if probe.missing_paths:
                    state = COMPAT_MISSING
                elif not probe.fingerprint:
                    state = COMPAT_UNKNOWN
                elif latest is None:
                    state = COMPAT_CHANGED
                elif scope in (new_scopes or set()):
                    # 这次探测刚发现的结构：第一次见 = 待确认兼容。
                    state = COMPAT_CHANGED
                elif latest["fingerprint"] == probe.fingerprint:
                    state = COMPAT_COMPATIBLE
                else:
                    # 当前结构在历史里出现过（曾经兼容过）也算兼容；只有没见过才算变化。
                    known = any(item["fingerprint"] == probe.fingerprint for item in history)
                    state = COMPAT_COMPATIBLE if known else COMPAT_CHANGED
            elif latest is not None:
                state = COMPAT_COMPATIBLE if latest["statePathsOk"] else COMPAT_MISSING
            items.append(
                {
                    "platform": platform,
                    "scope": scope,
                    "state": state,
                    "detail": detail,
                    "current": probe.payload() if probe is not None else None,
                    "latest": latest,
                    "history": history,
                }
            )
        return {
            "items": items,
            "probed": probed,
            "deduped": deduped,
            "checkedAt": now_iso(),
            "changed": [item["scope"] for item in items if item["state"] == COMPAT_CHANGED],
        }


def _platform_name(scope: str) -> str:
    """scope → 平台名；不依赖 domain.scopes，避免循环导入。"""
    if scope.startswith("workbuddy"):
        return "workbuddy"
    if scope.startswith("trae"):
        return "trae"
    return scope
