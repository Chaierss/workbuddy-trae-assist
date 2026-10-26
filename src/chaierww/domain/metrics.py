"""本地体验数据（只在本机，不上传）。

产品的第一版度量不能靠「偷偷上报」：本模块把事件写成 **本机 NDJSON**，
`~/.chaierww/metrics/events.ndjson`，不上传、不引第三方 SDK、不建服务端。
用户可在「设置 → 环境 → 体验数据」里关闭、导出、清空。

## 纪律

1. **双白名单**：事件名与属性名都必须是登记过的，其余静默丢弃。
2. **不落 PII**：属性值只接受短字符串（≤ 32 字符）与布尔值，不接受账号 id、
   邮箱、路径、prompt 等自由文本。写进文件的只有「事件名 + 白名单属性 + 时间」。
3. **写失败不影响任何交互**：调用方不检查返回值，异常一律吞掉并记日志。
4. **有界**：单文件 5 MB 轮转，最多保留 2 份历史，避免长期使用后占满磁盘。

## 漏斗口径

- `app_open(first=true)` 是「安装后首次打开」的锚点；
- `switch_completed` 首次出现即「首次换号」，两者之差是 TTFV；
- 激活 = 首次换号发生在首次打开后 24 小时内。
"""

from __future__ import annotations

import json
import logging
import statistics
import threading
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger("chaierww")

DIRNAME = "metrics"
FILENAME = "events.ndjson"
#: 轮转上限：events.ndjson 满 5 MB → events.1.ndjson → events.2.ndjson（旧的丢弃）。
MAX_BYTES = 5 * 1024 * 1024
KEEP_BACKUPS = 2

#: 允许记录的事件名（与方案 §2.2 一一对应）。
EVENT_NAMES: frozenset[str] = frozenset(
    {
        "app_open",
        "client_detected",
        "onboarding_step",
        "account_imported",
        "switch_previewed",
        "switch_completed",
        "gateway_key_created",
    }
)

#: 允许的属性名。**不要**往里加任何能定位到人或内容的东西。
PROPERTY_KEYS: frozenset[str] = frozenset(
    {"locale", "platform", "step", "status", "method", "first"}
)

#: 字符串属性的最大长度：超过一律丢弃（正常取值都是 2–20 字符）。
MAX_VALUE_LENGTH = 32

#: 默认漏斗窗口（天）。
DEFAULT_WINDOW_DAYS = 14

#: 首次换号窗口：24 小时。
ACTIVATION_WINDOW_SECONDS = 24 * 3600


def _now() -> datetime:
    return datetime.now(UTC).astimezone()


def clean_properties(raw: Any) -> dict[str, Any]:
    """按白名单收敛属性；未知键、超长字符串、非标量一律丢弃。"""
    if not isinstance(raw, dict):
        return {}
    cleaned: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in PROPERTY_KEYS:
            continue
        if isinstance(value, bool):
            cleaned[key] = value
        elif isinstance(value, str) and len(value) <= MAX_VALUE_LENGTH and value.strip():
            cleaned[key] = value.strip()
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            cleaned[key] = value
    return cleaned


class MetricsStore:
    def __init__(self, home: Path) -> None:
        self._dir = home / DIRNAME
        self._file = self._dir / FILENAME
        self._lock = threading.RLock()

    # ---------- 写入 ----------

    def record(self, name: str, props: Any = None) -> bool:
        """记录一条事件；非法事件名 / 属性直接丢弃。返回是否写入。"""
        if name not in EVENT_NAMES:
            return False
        payload = {
            "ts": _now().isoformat(timespec="seconds"),
            "name": name,
            "props": clean_properties(props),
        }
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                self._dir.mkdir(parents=True, exist_ok=True)
                if self._file.exists() and self._file.stat().st_size >= MAX_BYTES:
                    self._rotate()
                with open(self._file, "a", encoding="utf-8", newline="") as handle:
                    handle.write(line)
            except OSError as exc:
                # 度量永远不能阻断交互。
                logger.warning("写入本地体验数据失败：%s", exc)
                return False
        return True

    def _rotate(self) -> None:
        """events.ndjson → events.1.ndjson → …，只保留最近 KEEP_BACKUPS 份。"""
        oldest = self._dir / f"events.{KEEP_BACKUPS}.ndjson"
        if oldest.exists():
            oldest.unlink(missing_ok=True)
        for index in range(KEEP_BACKUPS - 1, 0, -1):
            source = self._dir / f"events.{index}.ndjson"
            if source.exists():
                source.replace(self._dir / f"events.{index + 1}.ndjson")
        if self._file.exists():
            self._file.replace(self._dir / "events.1.ndjson")

    # ---------- 读取 ----------

    def _read(self, since: datetime | None = None) -> list[dict[str, Any]]:
        """读全部轮转文件；解析失败的行跳过，不整份丢弃。"""
        with self._lock:
            paths = [self._file, *(self._dir / f"events.{i}.ndjson" for i in (1, 2))]
            events: list[dict[str, Any]] = []
            for path in paths:
                if not path.exists():
                    continue
                try:
                    raw = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                for line in raw.splitlines():
                    item = self._parse(line)
                    if item is None:
                        continue
                    if since is not None and item["at"] < since:
                        continue
                    events.append(item)
        events.sort(key=lambda item: item["at"])
        return events

    @staticmethod
    def _parse(line: str) -> dict[str, Any] | None:
        text = line.strip()
        if not text:
            return None
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        if not isinstance(data, dict) or data.get("name") not in EVENT_NAMES:
            return None
        stamp = data.get("ts")
        if not isinstance(stamp, str):
            return None
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            return None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return {
            "at": moment,
            "name": data["name"],
            "props": clean_properties(data.get("props")),
        }

    def funnel(self, days: int = DEFAULT_WINDOW_DAYS) -> dict[str, Any]:
        """近 N 天的计数 + 激活率 + TTFV（本机口径，样本小，只作方向判断）。"""
        span = max(1, min(int(days), 90))
        since = _now() - timedelta(days=span)
        events = self._read(since)
        counts = Counter(item["name"] for item in events)

        first_open = next(
            (
                item["at"]
                for item in events
                if item["name"] == "app_open" and item["props"].get("first") is True
            ),
            None,
        )
        first_switch = next(
            (item["at"] for item in events if item["name"] == "switch_completed"), None
        )
        first_switch_seconds: float | None = None
        activated: bool | None = None
        if first_open is not None and first_switch is not None:
            first_switch_seconds = max(
                0.0, (first_switch - first_open).total_seconds()
            )
            activated = first_switch_seconds <= ACTIVATION_WINDOW_SECONDS

        # 「打开 → 切换」的逐次差值：每次切换都配对它之前最近的一次打开。
        gaps: list[float] = []
        last_open: datetime | None = None
        for item in events:
            if item["name"] == "app_open":
                last_open = item["at"]
            elif item["name"] == "switch_completed" and last_open is not None:
                gaps.append(max(0.0, (item["at"] - last_open).total_seconds()))
        ttfv_median = statistics.median(gaps) if gaps else None

        onboarding: Counter[tuple[str, str]] = Counter()
        for item in events:
            if item["name"] != "onboarding_step":
                continue
            step = item["props"].get("step")
            status = item["props"].get("status")
            if isinstance(step, str) and isinstance(status, str):
                onboarding[(step, status)] += 1

        return {
            "windowDays": span,
            "since": since.isoformat(timespec="seconds"),
            "counts": {
                name: counts.get(name, 0) for name in sorted(EVENT_NAMES)
            },
            "perDay": self._per_day(events, span),
            "onboarding": [
                {"step": step, "status": status, "count": count}
                for (step, status), count in sorted(onboarding.items())
            ],
            "firstOpenAt": first_open.isoformat(timespec="seconds") if first_open else None,
            "firstSwitchAt": (
                first_switch.isoformat(timespec="seconds") if first_switch else None
            ),
            "firstSwitchSeconds": first_switch_seconds,
            "ttfvMedianSeconds": ttfv_median,
            "activated": activated,
        }

    @staticmethod
    def _per_day(events: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:
        """按天计数（含今天，共 days 天），缺失的天补零。"""
        today = _now().date()
        buckets: dict[str, Counter[str]] = {}
        for item in events:
            day = item["at"].date().isoformat()
            buckets.setdefault(day, Counter())[item["name"]] += 1
        rows: list[dict[str, Any]] = []
        for offset in range(days - 1, -1, -1):
            day = (today - timedelta(days=offset)).isoformat()
            counter = buckets.get(day, Counter())
            rows.append(
                {
                    "day": day,
                    "opens": counter.get("app_open", 0),
                    "switches": counter.get("switch_completed", 0),
                    "imports": counter.get("account_imported", 0),
                }
            )
        return rows

    def export_payload(self) -> dict[str, Any]:
        """导出给用户自己看的 JSON（不含任何凭据/账号内容）。"""
        events = self._read()
        return {
            "format": "chaierww.metrics",
            "version": 1,
            "generatedAt": _now().isoformat(timespec="seconds"),
            "note": "本地体验数据，只包含事件名、白名单属性与时间；不含账号或对话内容。",
            "events": [
                {
                    "ts": item["at"].isoformat(timespec="seconds"),
                    "name": item["name"],
                    "props": item["props"],
                }
                for item in events
            ],
        }

    def clear(self) -> int:
        """清空全部事件文件，返回删除的文件数。"""
        removed = 0
        with self._lock:
            for path in (
                self._file,
                *(self._dir / f"events.{i}.ndjson" for i in (1, 2)),
            ):
                try:
                    if path.exists():
                        path.unlink()
                        removed += 1
                except OSError as exc:
                    logger.warning("删除本地体验数据失败（%s）：%s", path, exc)
        return removed
