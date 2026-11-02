"""C0 探针脚本的解析与结论：用固定样本，不碰真实客户端目录。

探针是「平台覆盖能否进入实现」的唯一依据，所以它的判定必须可复现：
输入固定 JSON → 输出固定键名集合与三档结论，任何一个漂移都要在单测里暴露。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "_probe_scope.py"
_SPEC = importlib.util.spec_from_file_location("_probe_scope", SCRIPT_PATH)
assert _SPEC and _SPEC.loader
probe_scope = importlib.util.module_from_spec(_SPEC)
# dataclass 会在生成 __repr__/__eq__ 时按模块名回查命名空间，必须先登记再执行。
sys.modules[_SPEC.name] = probe_scope
_SPEC.loader.exec_module(probe_scope)


def test_inspect_payload_extracts_keys_without_values():
    """只返回键名与布尔标记：样本里塞了 token 明文，结果里也不能带出来。"""
    sample = {
        "account": {
            "uid": "u-1",
            "accessToken": "secret-token-value",
            "expiresAt": "2026-12-01T00:00:00Z",
            "deviceId": "dev-1",
        }
    }
    info = probe_scope.inspect_payload(sample)

    assert info["uid"] is True
    assert info["tokenKeys"] == ["accessToken"]
    assert info["expiryKeys"] == ["expiresAt"]
    assert info["deviceKeys"] == ["deviceId"]
    dumped = repr(info)
    assert "secret-token-value" not in dumped
    assert "u-1" not in dumped


def test_inspect_payload_without_credentials_reports_nothing():
    info = probe_scope.inspect_payload({"theme": "dark", "window": {"width": 1}})
    assert info == {
        "uid": False,
        "tokenKeys": [],
        "expiryKeys": [],
        "deviceKeys": [],
    }


def _result(**overrides):
    base = {
        "scope": "trae-global",
        "platform": "trae",
        "region": "global",
        "label": "Trae 国际版",
        "implemented": False,
        "path": "/tmp/x",
        "kind": "数据目录",
        "exists": True,
    }
    base.update(overrides)
    return probe_scope.ProbeResult(**base)


def test_verdict_tiers_are_fixed():
    """三档结论按顺序短接：不存在 > 与国内版共用 > 不可读 > 可导入 > 仅识别 > 不可读。"""
    assert _result(exists=False).verdict() == "路径不存在"
    assert (
        _result(note="与国内版共用同一个状态目录，切换会互相覆盖").verdict()
        == "与国内版共用（不算可导入）"
    )
    assert _result(readable=False).verdict() == "存在但不可读"
    assert _result(readable=True, uid=True, token_keys=["accessToken"]).verdict() == "可导入"
    assert _result(readable=True, uid=True).verdict() == "仅能识别文件"
    assert _result(readable=True, token_keys=["token"]).verdict() == "仅能识别文件"
    assert _result(readable=True).verdict() == "完全不可读"


def test_render_report_switches_between_go_and_no_go():
    no_go = probe_scope.render_report(
        [_result(readable=False)],
        day="2026-11-02",
    )
    assert "**No-Go**" in no_go
    assert "达到「可导入」的作用域：**无**" in no_go

    go = probe_scope.render_report(
        [_result(readable=True, uid=True, token_keys=["accessToken"])],
        day="2026-11-02",
    )
    assert "**Go**" in go
    assert "trae-global" in go
