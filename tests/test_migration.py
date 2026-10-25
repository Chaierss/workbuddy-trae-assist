"""迁移模块单元测试：记忆合并 / 连接器合并 / 会话复制。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chaierww.domain.migration import (
    MigrationError,
    MigrationSelection,
    merge_json_text,
    merge_memory_files,
    merge_memory_text,
    migrate_account_data,
    normalize_memory_line,
)

# ---------- 记忆：规范化 ----------


def test_normalize_collapses_whitespace_and_strips_bullets():
    assert normalize_memory_line("   - 用户偏好表格展示  ") == normalize_memory_line(
        "- 用户偏好表格展示"
    )
    assert normalize_memory_line("用户偏好   表格展示") == "用户偏好 表格展示"
    assert normalize_memory_line("- 偏好 A") == normalize_memory_line("* 偏好 A")
    assert normalize_memory_line("1. 偏好 A") == normalize_memory_line("2) 偏好 A")
    assert normalize_memory_line("## 偏好") == normalize_memory_line("### 偏好")
    assert normalize_memory_line("# 偏好") == normalize_memory_line("偏好")


def test_normalize_rejects_blank_and_separator():
    for raw in ["", "   ", "\t", "---", "***", "___", "- - -", "  ---  "]:
        assert normalize_memory_line(raw) is None, f"应忽略：{raw!r}"


def test_normalize_does_not_mangle_hashtag_or_negative():
    assert normalize_memory_line("#tag") == "#tag"
    assert normalize_memory_line("-5 度") == "-5 度"


# ---------- 记忆：合并 ----------


def test_merge_into_empty_target_copies_source():
    source = "# 记忆\n\n- 偏好表格\n  - 缩进保留\n"
    merged, result = merge_memory_text("", source, "uid-old")
    assert result.appended == 3
    assert "  - 缩进保留" in merged
    assert "迁移自" not in merged


def test_merge_skips_formatting_only_diff():
    target = "## 偏好\n- 表格展示\n"
    source = "   ### 偏好\n  * 表格展示\n"
    merged, result = merge_memory_text(target, source, "uid-old")
    assert result.appended == 0
    assert result.skipped_duplicate == 2
    assert merged == target


def test_merge_appends_only_new_lines():
    target = "## 偏好\n- 表格展示\n"
    source = "## 偏好\n- 表格展示\n- 直接执行指令\n"
    merged, result = merge_memory_text(target, source, "uid-old")
    assert result.appended == 1
    assert result.skipped_duplicate == 2
    assert "迁移自 uid-old" in merged
    assert "- 直接执行指令" in merged


def test_merge_is_idempotent():
    target = "## 偏好\n- 表格\n"
    source = "## 偏好\n- 表格\n- 结构化表达\n"
    once, first = merge_memory_text(target, source, "uid-old")
    assert first.appended == 1
    twice, second = merge_memory_text(once, source, "uid-old")
    assert second.appended == 0
    assert twice == once


def test_merge_same_uid_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "chaierww.domain.migration.workbuddy_data_dir",
        lambda: tmp_path,
    )
    with pytest.raises(MigrationError, match="相同"):
        merge_memory_files("uid-a", "uid-a")


def test_merge_memory_files_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "chaierww.domain.migration.workbuddy_data_dir",
        lambda: tmp_path,
    )
    # 源记忆
    src = tmp_path / "memory" / "src_memory.md"
    src.parent.mkdir(parents=True)
    src.write_text("## 偏好\n- 表格\n- 新内容\n", encoding="utf-8")

    result = merge_memory_files("src", "dst")
    assert result.appended >= 1
    dst = tmp_path / "memory" / "dst_memory.md"
    assert dst.is_file()
    assert "新内容" in dst.read_text(encoding="utf-8")

    # 幂等：再合并一次不新增
    result2 = merge_memory_files("src", "dst")
    assert result2.appended == 0


def test_merge_memory_files_backs_up_existing_target(tmp_path, monkeypatch):
    """回归：目标记忆已存在且本次有新增时必须产出备份路径（曾因 str/str 拼接崩 TypeError）。"""
    monkeypatch.setattr(
        "chaierww.domain.migration.workbuddy_data_dir",
        lambda: tmp_path,
    )
    memory = tmp_path / "memory"
    memory.mkdir(parents=True)
    (memory / "src_memory.md").write_text("## 偏好\n- 旧条目\n- 新增条目\n", encoding="utf-8")
    (memory / "dst_memory.md").write_text("## 偏好\n- 旧条目\n", encoding="utf-8")

    result = merge_memory_files("src", "dst")

    assert result.appended == 1
    assert result.backup is not None
    # 备份里必须是改前原文（含旧条目），而不是合并后的内容。
    assert "新增条目" not in Path(result.backup).read_text(encoding="utf-8")


def test_migrate_account_data_skips_disabled_blocks(tmp_path, monkeypatch):
    """设置里关掉全部迁移块后，什么也不做、不产生任何块报告。"""
    monkeypatch.setattr(
        "chaierww.domain.migration.workbuddy_data_dir",
        lambda: tmp_path,
    )

    report = migrate_account_data(
        "src",
        "dst",
        selection=MigrationSelection(
            memory=False,
            connectors=False,
            sessions=False,
            profile=False,
            workspaces=False,
        ),
    )

    assert report.memory is None
    assert report.connectors is None
    assert report.sessions is None
    assert report.profile is None
    assert report.workspaces is None
    assert report.payload()["changed"] is False


# ---------- 个人档案与偏好（profile） ----------


def _touch(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _patch_data_dir(monkeypatch, tmp_path) -> None:
    """profile 与 migration 各自 import 了 workbuddy_data_dir，两处都要打桩。"""
    monkeypatch.setattr("chaierww.domain.migration.workbuddy_data_dir", lambda: tmp_path)
    monkeypatch.setattr("chaierww.domain.profile_migration.workbuddy_data_dir", lambda: tmp_path)


def test_merge_profile_merges_md_and_json(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    from chaierww.domain.profile_migration import merge_profile_files

    _touch(tmp_path / "user-src-personal" / "IDENTITY.md", "## 身份\n- 甲\n- 新增\n")
    _touch(tmp_path / "user-src-personal" / "settings.json", '{"theme":"dark","newKey":1}')
    _touch(tmp_path / "user-dst-personal" / "IDENTITY.md", "## 身份\n- 甲\n")
    _touch(tmp_path / "user-dst-personal" / "settings.json", '{"theme":"light"}')

    result = merge_profile_files("src", "dst")

    assert result.changed
    identity = (tmp_path / "user-dst-personal" / "IDENTITY.md").read_text(encoding="utf-8")
    assert "新增" in identity
    merged = (tmp_path / "user-dst-personal" / "settings.json").read_text(encoding="utf-8")
    data = json.loads(merged)
    assert data["theme"] == "light"  # 标量冲突保留目标
    assert data["newKey"] == 1  # 源新增键并入

    # 幂等：再合并一次不产生改动
    second = merge_profile_files("src", "dst")
    assert not second.changed


def test_merge_profile_carries_security_authorizations_only(tmp_path, monkeypatch):
    """security 里只带走授权记录与规则；账号本位的加密口令库必须留在原处。"""
    _patch_data_dir(monkeypatch, tmp_path)
    from chaierww.domain.profile_migration import merge_profile_files

    _touch(
        tmp_path / "security" / "src" / "cipher" / "session-authorizations.jsonl",
        '{"s":"a"}\n{"s":"b"}\n',
    )
    _touch(tmp_path / "security" / "src" / "cipher" / "detect-rules.toml", "rule = 1\n")
    _touch(tmp_path / "security" / "src" / "cipher" / "entries.json.enc", "ENCRYPTED")
    _touch(
        tmp_path / "security" / "dst" / "cipher" / "session-authorizations.jsonl",
        '{"s":"a"}\n',
    )

    result = merge_profile_files("src", "dst")

    merged = (tmp_path / "security" / "dst" / "cipher" / "session-authorizations.jsonl").read_text(
        encoding="utf-8"
    )
    assert merged.splitlines() == ['{"s":"a"}', '{"s":"b"}']
    assert (tmp_path / "security" / "dst" / "cipher" / "detect-rules.toml").is_file()
    assert not (tmp_path / "security" / "dst" / "cipher" / "entries.json.enc").exists()
    assert any("entries.json.enc" in note for note in result.skipped)
    assert any("security/cipher" in where for where in result.merged_files)


def test_migrate_account_data_disabled_profile_not_in_report(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "chaierww.domain.migration.workbuddy_data_dir",
        lambda: tmp_path,
    )
    _touch(tmp_path / "user-src-personal" / "IDENTITY.md", "## 身份\n- 甲\n")

    report = migrate_account_data(
        "src",
        "dst",
        selection=MigrationSelection(
            memory=False,
            connectors=False,
            sessions=False,
            profile=False,
            workspaces=True,
        ),
    )
    assert report.profile is None
    assert report.workspaces is not None


# ---------- 工作区与项目元数据（workspaces） ----------


def test_merge_workspace_metadata_unions_into_root(tmp_path, monkeypatch):
    _patch_data_dir(monkeypatch, tmp_path)
    from chaierww.domain.profile_migration import merge_workspace_metadata

    _touch(
        tmp_path / "user-src-personal" / "workspace-state.json",
        '{"workspaces":[{"path":"/a"},{"path":"/b"}]}',
    )
    _touch(tmp_path / "workspace-state.json", '{"workspaces":[{"path":"/a"}]}')

    result = merge_workspace_metadata("src", "dst")

    assert result.changed
    data = json.loads((tmp_path / "workspace-state.json").read_text(encoding="utf-8"))
    paths = [item["path"] for item in data["workspaces"]]
    assert paths == ["/a", "/b"]  # 目标顺序优先 + 去重并集

    # 幂等
    second = merge_workspace_metadata("src", "dst")
    assert not second.changed


# ---------- 连接器：JSON 合并 ----------


def test_merge_json_recurses_into_objects():
    target = json.loads('{"mcpServers": {"a": {"command": "npx", "args": ["-y"]}}}')
    source = json.loads('{"mcpServers": {"a": {"command": "npx", "env": {"K": "V"}}}}')
    merged, result = merge_json_text(
        json.dumps(target), json.dumps(source)
    )
    data = json.loads(merged)
    assert data["mcpServers"]["a"]["command"] == "npx"
    assert data["mcpServers"]["a"]["args"] == ["-y"]  # 目标保留
    assert data["mcpServers"]["a"]["env"] == {"K": "V"}  # 源新增
    assert result.changed


def test_merge_json_arrays_deduplicate():
    target = json.loads('{"scope": ["a", "b"]}')
    source = json.loads('{"scope": ["b", "c", "a"]}')
    merged, result = merge_json_text(json.dumps(target), json.dumps(source))
    data = json.loads(merged)
    assert data["scope"] == ["a", "b", "c"]  # 目标顺序优先 + 去重
    assert result.dropped_duplicate_elements == 2


def test_merge_json_scalar_conflict_keeps_target():
    target = json.loads('{"key": "target-value"}')
    source = json.loads('{"key": "source-value"}')
    merged, result = merge_json_text(json.dumps(target), json.dumps(source))
    assert json.loads(merged)["key"] == "target-value"
    assert result.kept_target_keys == 1
    assert not result.changed


def test_merge_json_object_key_order_independent():
    # 对象键序不同不影响数组元素去重（对象里的数组字段才做元素级去重）。
    t = json.loads('{"items": [{"a":1,"b":2}]}')
    s = json.loads('{"items": [{"b":2,"a":1}]}')
    merged, result = merge_json_text(json.dumps(t), json.dumps(s))
    assert result.dropped_duplicate_elements == 1
