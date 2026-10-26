"""本机会话数据：会话元数据与对话正文。

## 数据源

- **元数据**：`~/.workbuddy/workbuddy.db` 的 `sessions` 表，按 `user_id`
  （= 账号 `external_id`）归属到号池账号；用量页用它给会话补标题/工作区、做归属校验。
- **正文**：`~/.workbuddy/projects/{workspace}/{cid}.jsonl` 里 `type=message` 行的
  user / assistant 文本（见 `read_conversation_messages`）。

本地库一律**只读**打开，本模块**不写任何文件**；账号切换迁徙另有
`session_migration.py` 一套写入逻辑，两者互不影响。

## 诚实边界

- **读不到 ≠ 没有**：库缺失 / 打不开 / 表缺列时返回空表或 None，绝不编造。
- 会话有索引行但没有 jsonl（正文缺失）时如实返回空正文，由调用方提示。
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

# ---------- 本地路径 / 解析 ----------


def workbuddy_data_dir() -> Path:
    """WorkBuddy 数据目录（home 下的 `.workbuddy`）。"""
    return Path.home() / ".workbuddy"


def workbuddy_db_path() -> Path:
    return workbuddy_data_dir() / "workbuddy.db"


def projects_dir() -> Path:
    return workbuddy_data_dir() / "projects"


def find_jsonl(cid: str) -> Path | None:
    """在某工作区目录下找 `{cid}.jsonl`；找不到返回 None。"""
    projects = projects_dir()
    if not projects.is_dir():
        return None
    try:
        for workspace in projects.iterdir():
            if not workspace.is_dir():
                continue
            path = workspace / f"{cid}.jsonl"
            if path.is_file():
                return path
    except OSError:
        return None
    return None


def build_history_index(projects: Path) -> set[str]:
    """扫 `projects/{workspace}/{cid}.jsonl`，返回有正文的 cid 集合。

    旧项目真机教训：对每行会话都重扫目录是 O(行数 × 工作区数) 的 syscall 爆炸，
    会话页加载慢的第二大来源。这里只扫一次，之后每行是一次 set 查询。
    """
    cids: set[str] = set()
    if not projects.is_dir():
        return cids
    try:
        for workspace in projects.iterdir():
            if not workspace.is_dir():
                continue
            for file in workspace.iterdir():
                if file.suffix == ".jsonl":
                    cids.add(file.stem)
    except OSError:
        return cids
    return cids


def is_claw_workspace(cwd: str) -> bool:
    """Claw 是账号绑定的 IM 渠道工作区，复制会话行过去目标账号也用不了。"""
    name = (cwd or "").strip().rstrip("/\\").rsplit("/", 1)[-1]
    return name.lower() == "claw"


def display_title(title: Any, custom_title: Any) -> str:
    """侧栏同款展示名：优先 custom_title（用户改名/定时任务名），否则 title。"""
    for value in (custom_title, title):
        if isinstance(value, str) and value.strip():
            return value.strip()
    return "(无标题)"


def _open_readonly() -> sqlite3.Connection | None:
    db = workbuddy_db_path()
    if not db.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def session_rows_for_user(uid: str) -> list[dict[str, Any]] | None:
    """某账号名下的**全部** ``sessions`` 行（含已软删除），供会话镜像采集。

    与 ``conversations_for_user`` 的区别：那个只取展示所需的四列、供用量页用；
    镜像需要完整行才能在目标账号里重建索引。读不到（库缺失 / 打不开）返回 ``None``，
    与「该账号没有会话」的空列表区分开。
    """
    if not uid:
        return []
    conn = _open_readonly()
    if conn is None:
        return None
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM sessions WHERE user_id = ?", (uid,)).fetchall()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return [dict(row) for row in rows]


def conversation_owner(cid: str) -> str | None:
    """这条会话属于哪个账号（``sessions.user_id``）；库里没有则 None。"""
    conn = _open_readonly()
    if conn is None:
        return None
    try:
        row = conn.execute("SELECT user_id FROM sessions WHERE id = ?", (cid,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def conversations_for_user(uid: str) -> dict[str, tuple[str, str]]:
    """某账号名下的会话：``cid -> (展示标题, cwd)``。读不到时返回空表。

    用量页要按会话聚合消耗、并给会话一个可读标题；只查这一个 ``user_id``，
    不扫整张表。
    """
    if not uid:
        return {}
    conn = _open_readonly()
    if conn is None:
        return {}
    try:
        cols = _columns(conn, "sessions")
        if not cols:
            return {}
        title_col = "custom_title" if "custom_title" in cols else "NULL"
        cwd_col = "cwd" if "cwd" in cols else "''"
        rows = conn.execute(
            f"SELECT id, title, {title_col}, {cwd_col} FROM sessions WHERE user_id = ?",
            (uid,),
        )
        return {
            str(cid): (display_title(title, custom), str(cwd or ""))
            for cid, title, custom, cwd in rows
        }
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


# ---------- 对话正文 ----------

#: 客户端把真实话术包在 `<user_query>` 里；前面还塞了一大段 `<system-reminder>`
#: 系统上下文。取 `<user_query>` 内容，取不到时丢掉整段注入块。
_USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)
_TEXT_TYPES = frozenset({"input_text", "output_text", "text"})


#: 客户端注入块（不是用户真实话术）——纯这类内容一律不进正文。
_INJECTED_TAG_RE = re.compile(
    r"^<(system-reminder|task-notification|tool-result|user-context|craft_mode)\b"
)


def _strip_injected(text: str) -> str:
    match = _USER_QUERY_RE.search(text)
    if match:
        return match.group(1).strip()
    stripped = text.strip()
    if _INJECTED_TAG_RE.match(stripped):
        return ""
    return stripped


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return _strip_injected(content)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") not in _TEXT_TYPES:
            continue
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        cleaned = _strip_injected(text)
        if cleaned:
            parts.append(cleaned)
    return "\n".join(parts).strip()


def read_conversation_messages(cid: str) -> list[dict[str, Any]]:
    """读一条会话的对话正文，返回 ``[{role, text, at, model}]``。

    只取 `type=message` 且角色为 user / assistant 的行；`reasoning` /
    `function_call*` / `file-history-snapshot` 等内部轨迹**不进正文**。
    正文文件不存在时返回空表（「没有聊天记录」≠「读失败」）。
    """
    path = find_jsonl(cid)
    if path is None:
        return []
    messages: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
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
                    raw = provider.get("model")
                    if isinstance(raw, str) and raw.strip():
                        model = raw.strip()
                timestamp = data.get("timestamp")
                messages.append(
                    {
                        "role": role,
                        "text": text,
                        "at": timestamp if isinstance(timestamp, int) else None,
                        "model": model,
                    }
                )
    except OSError:
        return []
    return messages
