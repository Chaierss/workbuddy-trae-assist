from __future__ import annotations

import json
from pathlib import Path

import pytest

from chaierww.domain import usage_turns as mod


def _line(**kwargs) -> str:
    # 与客户端落盘一致：紧凑分隔符（锚点是 "conversationRequestId":"x"）
    return json.dumps(kwargs, ensure_ascii=False, separators=(",", ":"))


def _write_jsonl(root: Path, cid: str, lines: list[str]) -> Path:
    path = root / "workspace-a" / f"{cid}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def projects(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    root.mkdir()
    monkeypatch.setattr(mod, "projects_dir", lambda: root)
    mod.reset_cache()
    return root


def _sample(root: Path) -> None:
    _write_jsonl(
        root,
        "cid-1",
        [
            _line(
                type="message",
                role="user",
                content=[
                    {
                        "type": "input_text",
                        "text": "<system-reminder>上下文</system-reminder>\n"
                        "<user_query>把首页改一下</user_query>",
                    }
                ],
                providerData={"conversationRequestId": "r1"},
                timestamp=100,
            ),
            _line(type="reasoning", providerData={"conversationRequestId": "r1"}),
            _line(
                type="message",
                role="assistant",
                content=[{"type": "output_text", "text": "先看下结构"}],
                providerData={"conversationRequestId": "r1", "model": "hy4"},
                timestamp=101,
            ),
            _line(
                type="message",
                role="assistant",
                content=[{"type": "output_text", "text": "改好了"}],
                providerData={"conversationRequestId": "r1", "model": "hy4"},
                timestamp=102,
            ),
            # 同一轮里的纯注入块（任务通知）不进正文
            _line(
                type="message",
                role="user",
                content=[
                    {"type": "input_text", "text": "<task-notification>done</task-notification>"}
                ],
                providerData={"conversationRequestId": "r1"},
                timestamp=103,
            ),
            _line(
                type="message",
                role="user",
                content=[{"type": "input_text", "text": "<user_query>第二个问题</user_query>"}],
                providerData={"conversationRequestId": "r2"},
                timestamp=200,
            ),
            _line(
                type="message",
                role="assistant",
                content=[{"type": "output_text", "text": "回答二"}],
                providerData={"conversationRequestId": "r2", "model": "hy4"},
                timestamp=201,
            ),
        ],
    )


def test_locate_maps_request_to_conversation(projects):
    _sample(projects)
    found = mod.lookup("r1")
    assert found is not None
    assert found.conversation_id == "cid-1"
    assert mod.lookup("r2").conversation_id == "cid-1"
    assert mod.lookup("nope") is None


def test_allowed_limits_to_account_conversations(projects):
    _sample(projects)
    assert mod.lookup("r1", allowed={"cid-1"}) is not None
    assert mod.lookup("r1", allowed={"other"}) is None


def test_read_turn_extracts_real_dialogue(projects):
    _sample(projects)
    turn = mod.read_turn("r1")
    assert turn.available is True
    assert turn.conversation_id == "cid-1"
    assert [m["role"] for m in turn.messages] == ["user", "assistant", "assistant"]
    assert turn.messages[0]["text"] == "把首页改一下"  # 注入上下文被剥离
    assert turn.messages[1]["text"] == "先看下结构"
    assert turn.messages[1]["model"] == "hy4"
    assert turn.truncated is False


def test_read_turn_does_not_leak_other_turn(projects):
    _sample(projects)
    turn = mod.read_turn("r2")
    assert [m["text"] for m in turn.messages] == ["第二个问题", "回答二"]


def test_read_turn_missing_is_honest(projects):
    _sample(projects)
    turn = mod.read_turn("nope")
    assert turn.available is False
    assert turn.conversation_id is None
    assert turn.error


def test_read_turn_missing_projects_dir_is_empty(projects):
    assert mod.lookup("r1") is None
    assert mod.read_turn("r1").available is False


def test_truncation_keeps_first_user_prompt(projects):
    lines = [
        _line(
            type="message",
            role="user",
            content=[{"type": "input_text", "text": "第一个问题"}],
            providerData={"conversationRequestId": "r1"},
            timestamp=1,
        )
    ]
    for index in range(mod.MAX_TURN_MESSAGES + 5):
        lines.append(
            _line(
                type="message",
                role="assistant",
                content=[{"type": "output_text", "text": f"回应 {index}"}],
                providerData={"conversationRequestId": "r1"},
                timestamp=2 + index,
            )
        )
    _write_jsonl(projects, "cid-long", lines)
    turn = mod.read_turn("r1")
    assert turn.truncated is True
    assert len(turn.messages) == mod.MAX_TURN_MESSAGES
    assert turn.messages[0]["text"] == "第一个问题"  # 提问不会被尾部截断吃掉
    assert turn.messages[-1]["text"] == f"回应 {mod.MAX_TURN_MESSAGES + 4}"


def test_index_picks_up_new_file(projects, monkeypatch):
    _sample(projects)
    assert mod.lookup("r1") is not None
    # 新文件落盘后（模拟客户端新写），过了冷却期应能重建索引
    _write_jsonl(
        projects,
        "cid-2",
        [
            _line(
                type="message",
                role="user",
                content=[{"type": "input_text", "text": "新对话"}],
                providerData={"conversationRequestId": "r9"},
            )
        ],
    )
    monkeypatch.setattr(mod, "REBUILD_MIN_SECONDS", 0.0)
    found = mod.lookup("r9")
    assert found is not None
    assert found.conversation_id == "cid-2"
