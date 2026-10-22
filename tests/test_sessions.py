"""本机对话数据测试。

用量页把云端计费行关联到本机 `projects/{workspace}/{cid}.jsonl` 的正文，
这里覆盖标题兜底、工作区判据与正文提取（系统注入块必须被丢掉）。
测试用临时目录驱动，不碰用户真实数据目录。
"""

from __future__ import annotations

import json
from pathlib import Path

from chaierww.domain.sessions import (
    _extract_text,
    build_history_index,
    display_title,
    is_claw_workspace,
    read_conversation_messages,
)

# ---------- 纯函数辅助 ----------


def test_display_title_prefers_custom():
    assert display_title("原标题", "改过的标题") == "改过的标题"
    assert display_title("原标题", None) == "原标题"
    assert display_title("  ", "") == "(无标题)"


def test_is_claw_workspace():
    assert is_claw_workspace("/home/x/claw") is True
    assert is_claw_workspace("D:/x/Claw") is True
    assert is_claw_workspace("/home/x/project") is False


def test_build_history_index(tmp_path):
    ws = tmp_path / "ws1"
    ws.mkdir()
    (ws / "abc.jsonl").write_text("{}")
    (ws / "def.txt").write_text("x")
    assert build_history_index(tmp_path) == {"abc"}


def test_extract_text_keeps_user_query_only():
    """客户端把真实话术裹在 <user_query> 里，前面是系统注入块 —— 只留前者。"""
    text = (
        '<system-reminder data-role="user-context">\n一大段系统上下文\n</system-reminder>\n'
        "<user_query>帮我看看这个 bug</user_query>"
    )
    assert _extract_text([{"type": "input_text", "text": text}]) == "帮我看看这个 bug"


def test_extract_text_drops_pure_injection():
    text = '<system-reminder data-role="user-context">\n只有系统上下文\n</system-reminder>'
    assert _extract_text([{"type": "input_text", "text": text}]) == ""


# ---------- 对话正文 ----------


def _jsonl(tmp_path: Path, cid: str, lines: list[dict]) -> None:
    ws = tmp_path / "projects" / "ws1"
    ws.mkdir(parents=True, exist_ok=True)
    (ws / f"{cid}.jsonl").write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in lines), encoding="utf-8"
    )


def test_read_messages_keeps_only_user_and_assistant(tmp_path, monkeypatch):
    monkeypatch.setattr("chaierww.domain.sessions.workbuddy_data_dir", lambda: tmp_path)
    _jsonl(
        tmp_path,
        "cid-1",
        [
            {
                "type": "message",
                "role": "user",
                "timestamp": 1000,
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            '<system-reminder data-role="user-context">ctx</system-reminder>\n'
                            "<user_query>你好</user_query>"
                        ),
                    }
                ],
            },
            {"type": "reasoning", "timestamp": 1500, "text": "内部推理"},
            {"type": "function_call", "timestamp": 1600, "name": "grep"},
            {
                "type": "message",
                "role": "assistant",
                "timestamp": 2000,
                "providerData": {"model": "glm-5"},
                "content": [{"type": "output_text", "text": "你好呀"}],
            },
            {"type": "message", "role": "system", "timestamp": 2100, "content": []},
            {"type": "file-history-snapshot", "timestamp": 2200},
        ],
    )
    messages = read_conversation_messages("cid-1")
    assert [item["role"] for item in messages] == ["user", "assistant"]
    assert messages[0]["text"] == "你好"
    assert messages[0]["at"] == 1000
    assert messages[1]["text"] == "你好呀"
    assert messages[1]["model"] == "glm-5"


def test_read_messages_missing_file_is_empty(tmp_path, monkeypatch):
    """正文文件不在 → 空表（「没有聊天记录」，不是读失败）。"""
    monkeypatch.setattr("chaierww.domain.sessions.workbuddy_data_dir", lambda: tmp_path)
    assert read_conversation_messages("nope") == []
