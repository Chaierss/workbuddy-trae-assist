"""会话正文时间线合并：链前缀、分支排序、父链重挂、回退边界。"""

from __future__ import annotations

import json

from chaierww.domain.sync.merge import merge_timeline


def _line(
    node_id: str,
    parent: str | None,
    ts: int,
    *,
    kind: str = "message",
    content: str = "",
) -> str:
    data = {
        "sessionId": "sk-test",
        "id": node_id,
        "timestamp": ts,
        "type": kind,
    }
    if parent:
        data["parentId"] = parent
    if kind == "message":
        data["role"] = "user"
        data["message"] = content or node_id
    return json.dumps(data, ensure_ascii=False)


def _text(*lines: str) -> str:
    return "\n".join(lines)


def _ids(text: str) -> list[str]:
    return [json.loads(line)["id"] for line in text.splitlines() if line.strip()]


def _by_id(text: str) -> dict[str, dict]:
    return {
        json.loads(line)["id"]: json.loads(line)
        for line in text.splitlines()
        if line.strip()
    }


def test_same_and_ancestor_fast_paths():
    base = _text(_line("m1", None, 100))
    longer = _text(_line("m1", None, 100), _line("m2", "m1", 200))

    assert merge_timeline(base, base).strategy == "same"
    forward = merge_timeline(base, longer)
    assert forward.ok and forward.strategy == "append" and forward.text == longer
    backward = merge_timeline(longer, base)
    assert backward.ok and backward.strategy == "local_newer" and backward.text == longer


def test_two_branches_merge_in_timeline_order():
    base = _line("m1", None, 100)
    local = _text(
        base,
        _line("m2a", "m1", 200, content="A1"),
        _line("m3a", "m2a", 300, content="A2"),
    )
    mirror = _text(
        base,
        _line("m2b", "m1", 250, content="B1"),
    )

    result = merge_timeline(local, mirror)

    assert result.ok, result.reason
    assert result.strategy == "timeline"
    assert _ids(result.text) == ["m1", "m2a", "m3a", "m2b"]
    # 后一段的首节点必须挂到前一段的末尾，否则客户端看到两条断链。
    assert _by_id(result.text)["m2b"]["parentId"] == "m3a"


def test_later_branch_goes_second():
    base = _line("m1", None, 100)
    local = _text(base, _line("m2b", "m1", 400, content="B1"))
    mirror = _text(
        base,
        _line("m2a", "m1", 200, content="A1"),
        _line("m3a", "m2a", 300, content="A2"),
    )

    result = merge_timeline(local, mirror)

    assert result.ok, result.reason
    assert _ids(result.text) == ["m1", "m2a", "m3a", "m2b"]
    assert _by_id(result.text)["m2b"]["parentId"] == "m3a"


def test_subset_branch_uses_merged_superset():
    base = _line("m1", None, 100)
    merged = _text(
        base,
        _line("m2a", "m1", 200, content="A1"),
        _line("m3a", "m2a", 300, content="A2"),
        _line("m2b", "m3a", 250, content="B1"),
    )
    local = _text(base, _line("m2b", "m1", 250, content="B1"))

    result = merge_timeline(local, merged)

    assert result.ok, result.reason
    assert result.strategy == "append"
    assert result.text == merged


def test_same_id_with_different_content_falls_back_to_conflict():
    base = _line("m1", None, 100)
    local = _text(base, _line("m2", "m1", 200, content="改过"))
    mirror = _text(base, _line("m2", "m1", 200, content="原版"))

    result = merge_timeline(local, mirror)

    assert not result.ok
    assert "内容不同" in result.reason or "重复" in result.reason


def test_missing_chain_id_or_common_prefix_falls_back():
    no_id = _text(
        json.dumps({"type": "message", "timestamp": 100, "message": "x"}),
        _line("m2", None, 200),
    )
    other = _text(_line("z1", None, 100))
    base = _text(_line("m1", None, 100))

    assert not merge_timeline(no_id, base).ok
    assert not merge_timeline(base, other).ok


def test_metadata_is_kept_once_across_branches():
    title = json.dumps({"type": "ai-title", "id": "t1", "timestamp": 50, "aiTitle": "标题"})
    base = _line("m1", None, 100)
    local = _text(title, base, _line("m2a", "m1", 200))
    mirror = _text(title, base, _line("m2b", "m1", 300))

    result = merge_timeline(local, mirror)

    assert result.ok, result.reason
    titles = [line for line in result.text.splitlines() if '"ai-title"' in line]
    assert len(titles) == 1
