"""用量明细 ↔ 本机对话的关联层。

云端用量接口（`get-user-request-usage`）只回计费行（`requestId` + credit + model +
client + requestTime），**没有**对话正文。但本机 WorkBuddy 会话库
（`~/.workbuddy/projects/{workspace}/{cid}.jsonl`）里每条 `message` 的
`providerData.conversationRequestId` 就是云端那行的 `requestId` —— 一次「提问」
在两边是同一个 id（2026-10-20 真机实测）。

于是可以：拿云端明细行的 `requestId` 去本机 jsonl 里定位那一轮对话，回答
「这一笔积分花在了哪次对话上、聊了什么」。

## 实现取向

- **一次字节级扫描建索引**（`bytes.find`，不做 JSON 解析）：本机 79 个 jsonl /
  392 MB 实测 0.5 秒。索引是 `requestId -> (jsonl 路径, 起止字节偏移)`。
- 索引缓存在内存里，按「文件集合 + 各自 mtime/size」判新鲜；刚重建过 5 秒内
  不重复建（客户端正在写文件时 mtime 一直在变）。
- 定位到的那一轮只取 user / assistant 正文（复用 `sessions._extract_text`），
  内部轨迹（reasoning / function_call* / file-history-snapshot）不进正文。

## 诚实边界

- 本机找不到对应对话时返回 `available=False` + 原因（可能是别的机器上跑的、
  或本机正文已被清理），**不是**「没有对话」。
- Trae 既没有逐请求明细也没有本机会话库，这条链路对它不适用。
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from chaierww.domain.sessions import _extract_text, workbuddy_data_dir

_ANCHOR = b'"conversationRequestId":"'
_QUOTE = b'"'

#: 同一轮最多返回多少条正文（工具调用之间的碎碎念可能很多）。
#: 截断时**保留第一条用户提问**，再取末尾若干条 —— 否则最关键的「我问了啥」会被截掉。
MAX_TURN_MESSAGES = 20

#: 索引重建的最小间隔：客户端正在写 jsonl 时 mtime 一直变，不必每次点击都重建。
REBUILD_MIN_SECONDS = 5.0


@dataclass(frozen=True, slots=True)
class TurnRange:
    """一次「提问」在本机 jsonl 里的字节区间。"""

    conversation_id: str
    path: Path
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class UsageTurn:
    """一次「提问」的对话内容（云端明细行点开就是它）。"""

    request_id: str
    conversation_id: str | None
    messages: tuple[dict[str, Any], ...]
    available: bool
    error: str | None = None
    truncated: bool = False

    def payload(self) -> dict[str, Any]:
        return {
            "requestId": self.request_id,
            "conversationId": self.conversation_id,
            "messages": list(self.messages),
            "available": self.available,
            "error": self.error,
            "truncated": self.truncated,
        }


def projects_dir() -> Path:
    return workbuddy_data_dir() / "projects"


# ---------- 索引 ----------


def _scan_file(path: Path) -> dict[str, tuple[int, int]]:
    """扫一个 jsonl，返回 ``{requestId: (起, 止) 字节偏移}``。

    同一个 `conversationRequestId` 在一轮对话里会重复出现（每个 step 都有），
    所以按「连续相同 id 的锚点段」切：起点取该 id 首次锚点所在行的行首，
    终点取下一个**不同** id 首次锚点所在行的行首（没有则到文件末尾）。
    """
    try:
        data = path.read_bytes()
    except OSError:
        return {}

    anchors: list[tuple[str, int]] = []
    pos = 0
    while True:
        at = data.find(_ANCHOR, pos)
        if at < 0:
            break
        begin = at + len(_ANCHOR)
        end = data.find(_QUOTE, begin)
        if end < 0:
            break
        anchors.append((data[begin:end].decode("ascii", "ignore"), at))
        pos = end + 1
    if not anchors:
        return {}

    segments: list[tuple[str, int]] = []
    previous: str | None = None
    for request_id, at in anchors:
        if request_id != previous:
            segments.append((request_id, at))
            previous = request_id

    ranges: dict[str, tuple[int, int]] = {}
    for index, (request_id, at) in enumerate(segments):
        start = data.rfind(b"\n", 0, at) + 1
        if index + 1 < len(segments):
            next_at = segments[index + 1][1]
            end = data.rfind(b"\n", 0, next_at) + 1
            if end <= start:
                end = len(data)
        else:
            end = len(data)
        ranges.setdefault(request_id, (start, end))
    return ranges


def _list_files(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    try:
        return sorted(
            path
            for workspace in root.iterdir()
            if workspace.is_dir()
            for path in workspace.glob("*.jsonl")
        )
    except OSError:
        return []


def _fingerprint(files: list[Path]) -> tuple[tuple[str, int, int], ...]:
    entries: list[tuple[str, int, int]] = []
    for path in files:
        try:
            stat = path.stat()
        except OSError:
            continue
        entries.append((str(path), int(stat.st_mtime), stat.st_size))
    return tuple(entries)


class UsageTurnIndex:
    """`requestId -> TurnRange` 的内存索引（不落盘）。"""

    def __init__(self, ttl: float = 600.0) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._ranges: dict[str, list[TurnRange]] = {}
        self._fingerprint: tuple[tuple[str, int, int], ...] = ()
        self._built_at = 0.0

    def reset(self) -> None:
        with self._lock:
            self._ranges.clear()
            self._fingerprint = ()
            self._built_at = 0.0

    def _ensure(self, root: Path) -> dict[str, list[TurnRange]]:
        files = _list_files(root)
        fingerprint = _fingerprint(files)
        now = time.monotonic()
        with self._lock:
            fresh = (
                fingerprint == self._fingerprint
                and self._ranges
                and now - self._built_at < self._ttl
            )
            cooling = self._built_at and now - self._built_at < REBUILD_MIN_SECONDS
            if fresh or (cooling and self._ranges):
                return self._ranges
            ranges: dict[str, list[TurnRange]] = {}
            for path in files:
                for request_id, (start, end) in _scan_file(path).items():
                    ranges.setdefault(request_id, []).append(
                        TurnRange(
                            conversation_id=path.stem,
                            path=path,
                            start=start,
                            end=end,
                        )
                    )
            self._ranges = ranges
            self._fingerprint = fingerprint
            self._built_at = now
            return self._ranges

    def locate(self, request_id: str, *, allowed: set[str] | None = None) -> TurnRange | None:
        """定位一次提问；``allowed`` 用于把结果限定在这个账号自己的会话里。

        同一个 requestId 可能因账号切换迁徙被复制到另一个账号（cid 不同），
        索引里会保留每份副本，优先选 ``allowed`` 里的那份。
        """
        found = self._ensure(projects_dir()).get(request_id) or []
        if allowed is not None:
            for item in found:
                if item.conversation_id in allowed:
                    return item
            return None
        return found[0] if found else None

    def select_many(
        self, request_ids: Iterable[str], *, allowed: set[str] | None = None
    ) -> dict[str, TurnRange]:
        """批量定位（只建/校验一次索引，避免逐行重复 stat 全部 jsonl）。"""
        index = self._ensure(projects_dir())
        out: dict[str, TurnRange] = {}
        for request_id in request_ids:
            found = index.get(request_id) or []
            if allowed is not None:
                match = next((item for item in found if item.conversation_id in allowed), None)
            else:
                match = found[0] if found else None
            if match is not None:
                out[request_id] = match
        return out


_INDEX = UsageTurnIndex()


def lookup(request_id: str, *, allowed: set[str] | None = None) -> TurnRange | None:
    return _INDEX.locate(request_id, allowed=allowed)


def lookup_many(
    request_ids: Iterable[str], *, allowed: set[str] | None = None
) -> dict[str, TurnRange]:
    return _INDEX.select_many(request_ids, allowed=allowed)


def reset_cache() -> None:
    """测试用：清掉内存索引。"""
    _INDEX.reset()


# ---------- 读一轮对话 ----------


def _messages_in_range(location: TurnRange) -> tuple[list[dict[str, Any]], bool]:
    try:
        with location.path.open("rb") as handle:
            handle.seek(location.start)
            raw = handle.read(max(0, location.end - location.start))
    except OSError:
        return [], False

    messages: list[dict[str, Any]] = []
    truncated = False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict) or data.get("type") != "message":
            continue
        role = data.get("role")
        if role not in ("user", "assistant"):
            continue
        text = _extract_text(data.get("content"))
        if not text:
            continue
        provider = data.get("providerData")
        model = None
        if isinstance(provider, dict):
            value = provider.get("model")
            if isinstance(value, str) and value.strip():
                model = value.strip()
        timestamp = data.get("timestamp")
        messages.append(
            {
                "role": role,
                "text": text,
                "at": timestamp if isinstance(timestamp, int) else None,
                "model": model,
            }
        )
    if len(messages) > MAX_TURN_MESSAGES:
        first_user = next(
            (index for index, message in enumerate(messages) if message["role"] == "user"),
            None,
        )
        if first_user is not None:
            keep = MAX_TURN_MESSAGES - 1
            head = [messages[first_user]]
            tail = messages[-keep:] if keep else []
            messages = head + [message for message in tail if message is not messages[first_user]]
        else:
            messages = messages[-MAX_TURN_MESSAGES:]
        truncated = True
    return messages, truncated


#: 明细摘要里每条文本的截断长度（够看清「问了什么、答了什么」）。
BRIEF_TEXT_LIMIT = 220
#: 一次批量摘要最多处理多少条（详情页一页 50 条，留一倍余量）。
MAX_BRIEF_ITEMS = 120


def _clip(text: str, limit: int = BRIEF_TEXT_LIMIT) -> str:
    """压平空白并截断，避免表格行被超长正文撑爆。"""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[:limit].rstrip() + "…"


def brief_of(turn: UsageTurn) -> dict[str, Any]:
    """一轮对话 → 列表用的摘要（首条提问 + 末条回答 + 条数）。"""
    user = next((item for item in turn.messages if item.get("role") == "user"), None)
    assistant = next(
        (item for item in reversed(turn.messages) if item.get("role") == "assistant"), None
    )
    model = None
    for item in turn.messages:
        value = item.get("model")
        if isinstance(value, str) and value.strip():
            model = value.strip()
            break
    return {
        "requestId": turn.request_id,
        "conversationId": turn.conversation_id,
        "available": turn.available,
        "messageCount": len(turn.messages),
        "userText": _clip(str(user.get("text") or "")) if user else "",
        "assistantText": _clip(str(assistant.get("text") or "")) if assistant else "",
        "model": model,
        "error": turn.error,
    }


def read_turns_brief(
    request_ids: Iterable[str],
    *,
    allowed: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """批量读摘要：只建一次索引，逐条读各自的字节区间。

    明细表一页最多几十行，这里限流到 ``MAX_BRIEF_ITEMS``，避免误传全量 id
    造成一次扫描几百个区间。找不到的 id 不返回（前端按「本机无记录」处理）。
    """
    ids = list(dict.fromkeys(item for item in request_ids if item))[:MAX_BRIEF_ITEMS]
    if not ids:
        return {}
    locations = lookup_many(ids, allowed=allowed)
    out: dict[str, dict[str, Any]] = {}
    for request_id in ids:
        location = locations.get(request_id)
        if location is None:
            continue
        messages, truncated = _messages_in_range(location)
        if not messages:
            continue
        out[request_id] = brief_of(
            UsageTurn(
                request_id=request_id,
                conversation_id=location.conversation_id,
                messages=tuple(messages),
                available=True,
                truncated=truncated,
            )
        )
    return out


def read_turn(
    request_id: str,
    *,
    conversation_id: str | None = None,
    allowed: set[str] | None = None,
) -> UsageTurn:
    """读一次提问的对话内容；找不到时如实返回原因。

    ``allowed`` 限定只在这些会话里找（账号归属校验用），避免把同一 requestId
    在别的账号副本里的内容返回出去。
    """
    scope = {conversation_id} if conversation_id else allowed
    location = lookup(request_id, allowed=scope)
    if location is None:
        return UsageTurn(
            request_id=request_id,
            conversation_id=None,
            messages=(),
            available=False,
            error="本机没有这次请求的对话记录（可能在其他设备产生，或本机正文已被清理）",
        )
    messages, truncated = _messages_in_range(location)
    if not messages:
        return UsageTurn(
            request_id=request_id,
            conversation_id=location.conversation_id,
            messages=(),
            available=False,
            error="定位到了会话文件，但这一轮没有可展示的对话正文",
        )
    return UsageTurn(
        request_id=request_id,
        conversation_id=location.conversation_id,
        messages=tuple(messages),
        available=True,
        truncated=truncated,
    )
