"""成长任务 API 自动化模块测试（`chaierww.domain.wb_tasks`）。

不碰真实网络：把 `_Proto` 的 HTTP 方法换成假实现，验证闭环判定
（列表 → accept → 动作上报 → 轮询 → 领奖）与幂等语义。
"""

from __future__ import annotations

from typing import Any

from chaierww.domain import wb_tasks as mod
from chaierww.domain.wb_tasks import (
    ACTIONS,
    GrowthTask,
    GrowthTaskRunner,
)


def _credential(token: str = "tok") -> dict[str, Any]:
    return {
        "format": "oauth-token",
        "accessToken": token,
        "uid": "uid-1",
        "domain": "www.workbuddy.cn",
    }


def _task(code: str, status: str = "not_accepted", current: int = 0, target: int = 1) -> GrowthTask:
    return GrowthTask(code=code, title=code, accept_status=status, target=target, current=current)


class _FakeProto:
    """顶掉 runner 构造的 `_Proto`，记录调用、按脚本返回。"""

    def __init__(self) -> None:
        self.listed: list[GrowthTask] = []
        self.accepted: list[list[str]] = []
        self.actions: list[str] = []
        self.claims: list[str] = []
        self.claim_results: dict[str, tuple[float, float, str]] = {}
        # 动作执行后要回读的任务状态（用于模拟「异步计分落定」）
        self.after: dict[str, GrowthTask] = {}

    def list_tasks(self) -> tuple[list[GrowthTask], str]:
        return self.listed, ""

    def accept(self, codes: list[str]) -> str:
        self.accepted.append(codes)
        return ""

    def task_waiting(self, code: str) -> GrowthTask | None:
        return self.after.get(code)

    def claim(self, code: str) -> tuple[float, float, str]:
        self.claims.append(code)
        return self.claim_results.get(code, (0.0, 0.0, ""))


class _FakeAction:
    """动作表里的占位动作：记录是否被调用。"""

    def __init__(self) -> None:
        self.called: list[str] = []

    def __call__(self, proto, task: GrowthTask) -> str:  # noqa: ANN001
        self.called.append(task.code)
        return "已上报"


def test_parse_tasks_handles_nested_and_flat_progress():
    data = {
        "tasks": [
            {"task_code": "a", "title": "A", "accept_status": "claimed",
             "progress": {"current": 2, "target": 2}},
            {"task_code": "b", "title": "B", "accept_status": "not_accepted",
             "current": 1, "target": 5},
        ]
    }
    tasks = mod._parse_tasks(data)
    assert len(tasks) == 2
    assert tasks[0].claimed is True
    assert tasks[1].target == 5
    assert tasks[1].current == 1


def test_runner_skips_claimed_and_locked(monkeypatch):
    runner = GrowthTaskRunner()
    proto = _FakeProto()
    action = _FakeAction()
    proto.listed = [
        _task("chat_5", status="claimed"),
        _task("template_5", status="not_accepted", current=0, target=5),
        _task("expert_chat", status="not_accepted"),  # 无动作，跳过
    ]
    monkeypatch.setattr(mod, "_Proto", lambda *a, **k: proto)
    monkeypatch.setitem(mod.ACTIONS, "template_5", ("使用 5 个模板", action))

    report = runner.run_all(_credential())
    assert report["error"] is None
    # 只有 template_5（有动作且未领）被处理；claimed 的完全不碰。
    assert action.called == ["template_5"]
    # 无动作的 expert 任务不执行动作，但要替用户接受（否则客户端做了也不计进度）。
    assert proto.accepted == [["template_5"], ["expert_chat"]]
    skipped = [item for item in report["outcomes"] if item["status"] == "skipped"]
    assert [item["code"] for item in skipped] == ["expert_chat"]
    assert "已接受任务" in skipped[0]["message"]
    runner.close()


def test_runner_claims_completed_task_without_action(monkeypatch):
    """客户端里已经做完、只是没领奖的任务：直接领，不需要动作表覆盖。"""
    runner = GrowthTaskRunner()
    proto = _FakeProto()
    proto.listed = [_task("expert_5", status="accepted", current=5, target=5)]
    proto.claim_results["expert_5"] = (100.0, 0.0, "")
    monkeypatch.setattr(mod, "_Proto", lambda *a, **k: proto)

    report = runner.run_all(_credential())
    assert proto.accepted == []
    assert proto.claims == ["expert_5"]
    outcome = report["outcomes"][0]
    assert outcome["status"] == "ok"
    assert outcome["credit"] == 100.0
    runner.close()


def test_runner_accepts_then_runs_action_then_claims(monkeypatch):
    runner = GrowthTaskRunner()
    proto = _FakeProto()
    proto.listed = [_task("template_5", status="not_accepted", current=0, target=5)]
    proto.after["template_5"] = _task("template_5", status="accepted", current=5, target=5)
    proto.claim_results["template_5"] = (100.0, 2.0, "")
    monkeypatch.setattr(mod, "_Proto", lambda *a, **k: proto)
    monkeypatch.setitem(mod.ACTIONS, "template_5", ("使用 5 个模板", _FakeAction()))

    report = runner.run_all(_credential())
    assert report["error"] is None
    outcomes = report["outcomes"]
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome["status"] == "ok"
    assert outcome["credit"] == 100.0
    assert "已领奖" in outcome["message"]
    # 顺序：accept → 动作 → claim
    assert proto.accepted == [["template_5"]]
    assert proto.claims == ["template_5"]
    runner.close()


def test_runner_reports_already_claimed_as_skipped(monkeypatch):
    runner = GrowthTaskRunner()
    proto = _FakeProto()
    proto.listed = [_task("create_canvas", status="completed", current=1, target=1)]
    proto.claim_results["create_canvas"] = (0.0, 0.0, "")
    monkeypatch.setattr(mod, "_Proto", lambda *a, **k: proto)
    monkeypatch.setitem(mod.ACTIONS, "create_canvas", ("画布", _FakeAction()))

    report = runner.run_all(_credential())
    outcome = report["outcomes"][0]
    assert outcome["status"] == "skipped"
    assert "已领取过" in outcome["message"]
    runner.close()


def test_runner_action_error_is_failed_not_crash(monkeypatch):
    runner = GrowthTaskRunner()
    proto = _FakeProto()
    proto.listed = [_task("template_5", status="not_accepted", current=0, target=5)]

    def boom(proto, task):  # noqa: ANN001
        raise mod.TaskActionError("上报失败")

    monkeypatch.setattr(mod, "_Proto", lambda *a, **k: proto)
    monkeypatch.setitem(mod.ACTIONS, "template_5", ("使用 5 个模板", boom))

    report = runner.run_all(_credential())
    outcome = report["outcomes"][0]
    assert outcome["status"] == "failed"
    assert "上报失败" in outcome["message"]
    runner.close()


def test_runner_returns_error_when_list_fails(monkeypatch):
    runner = GrowthTaskRunner()
    proto = _FakeProto()

    def failing_list():
        return [], "token 失效"

    proto.list_tasks = failing_list  # type: ignore[assignment]
    monkeypatch.setattr(mod, "_Proto", lambda *a, **k: proto)

    report = runner.run_all(_credential())
    assert report["outcomes"] == []
    assert "token 失效" in report["error"]
    runner.close()


def test_summarize_outcomes_counts_statuses():
    outcomes = [
        {"status": "ok"},
        {"status": "ok"},
        {"status": "skipped"},
        {"status": "failed"},
    ]
    assert mod.summarize_outcomes(outcomes) == {"ok": 2, "skipped": 1, "failed": 1}


def test_actions_table_covers_only_implemented_codes():
    # 动作表里的码必须能和「可自动」判定对齐（activities.py 复用同一张表）
    assert "chat_5" in ACTIONS
    assert "template_5" in ACTIONS
    assert "expert_chat" not in ACTIONS
    assert "skill_1" not in ACTIONS
