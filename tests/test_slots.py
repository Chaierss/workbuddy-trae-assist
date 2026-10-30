"""多程序位（阶段 5.2）单测：注册表、位解析与检测出参。

只验证「哪些位可写、哪些位只展示」，以及未适配的位**绝不**被写：
本机没有其它宿主客户端，写路径不实测就不该开。
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chaierww.adapters import trae as trae_module
from chaierww.adapters import workbuddy as workbuddy_module
from chaierww.adapters.base import AdapterError
from chaierww.adapters.trae import TraeAdapter, TraePaths
from chaierww.adapters.workbuddy import (
    STATE_FILENAME,
    WorkBuddyAdapter,
    WorkBuddyPaths,
)
from chaierww.config import Settings
from chaierww.main import create_app


@pytest.fixture
def workbuddy(tmp_path, monkeypatch):
    state_dir = tmp_path / "auth"
    state_dir.mkdir()
    instance = WorkBuddyAdapter(
        paths=WorkBuddyPaths(
            state_dir=state_dir,
            state_file=state_dir / STATE_FILENAME,
            app_candidates=(),
        )
    )
    monkeypatch.setattr(instance, "is_running", lambda: False)
    return instance


@pytest.fixture
def trae(tmp_path, monkeypatch):
    state_dir = tmp_path / "globalStorage"
    state_dir.mkdir()
    instance = TraeAdapter(
        paths=TraePaths(
            state_dir=state_dir,
            storage_file=state_dir / "storage.json",
            app_candidates=(),
        )
    )
    monkeypatch.setattr(instance, "is_running", lambda: False)
    return instance


def test_workbuddy_registers_five_slots(workbuddy):
    slots = workbuddy.slots()
    assert [item.key for item in slots] == [
        "desktop",
        "codebuddy-cn-ide",
        "codebuddy-cli",
        "vscode-extension",
        "jetbrains-plugin",
    ]
    desktop = slots[0]
    assert desktop.writable is True
    assert desktop.state_file == workbuddy._paths.state_file
    assert desktop.reason == ""

    for item in slots[1:]:
        assert item.writable is False
        assert item.state_file is None  # 未适配 → 连路径都不声明
        assert "尚未适配" in item.reason


def test_trae_registers_one_writable_slot(trae):
    slots = trae.slots()
    assert len(slots) == 1
    assert slots[0].key == "ide"
    assert slots[0].writable is True


def test_slot_adapter_only_resolves_writable_slots(workbuddy):
    assert workbuddy.slot_adapter("desktop") is workbuddy

    with pytest.raises(AdapterError, match="未知程序位"):
        workbuddy.slot_adapter("nope")


def test_slot_adapter_rejects_unadapted_slots(workbuddy):
    with pytest.raises(AdapterError) as caught:
        workbuddy.slot_adapter("codebuddy-cli")
    assert "尚未适配" in str(caught.value)
    assert "CodeBuddy CLI" in str(caught.value)


def test_slot_payload_shape(workbuddy):
    payload = workbuddy.slots()[0].payload()
    assert payload == {
        "key": "desktop",
        "label": "桌面端",
        "writable": True,
        "installed": False,
        "stateFile": str(workbuddy._paths.state_file),
        "appPath": None,
        "reason": "",
    }


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(home=tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def test_detect_endpoint_reports_slots(client):
    body = client.get("/api/detect", params={"scope": "workbuddy-cn"}).json()
    keys = [item["key"] for item in body["slots"]]
    assert keys[0] == "desktop"
    assert len(keys) == 5


def test_detection_endpoint_reports_slots_per_client(client):
    body = client.get("/api/detection").json()
    by_scope = {item["scope"]: item for item in body["clients"]}
    assert [item["key"] for item in by_scope["workbuddy-cn"]["slots"]][0] == "desktop"
    assert [item["key"] for item in by_scope["trae-cn"]["slots"]] == ["ide"]


def test_dry_run_with_unadapted_slot_is_rejected(client, tmp_path):
    account_id = client.app.state.repository.upsert(
        scope="workbuddy-cn",
        external_id="u-1",
        display_name="账号甲",
        credential={"access_token": "tok"},
    ).account.id
    response = client.post(
        "/api/switch/dry-run",
        json={"accountId": account_id, "slot": "vscode-extension"},
    )
    assert response.status_code == 400
    assert "尚未适配" in response.json()["detail"]


# ---------- Linux 路径解析（0.12.0） ----------
# 这两条只验纯路径逻辑，在 Windows 上也能跑：`_linux_paths()` 不读注册表、不碰 DPAPI。


def test_linux_trae_paths_prefer_existing_product_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("CHAIERWW_TRAE_STATE_DIR", raising=False)
    (tmp_path / "Trae" / "User" / "globalStorage").mkdir(parents=True)

    paths = trae_module._linux_paths()

    assert paths.state_dir == tmp_path / "Trae" / "User" / "globalStorage"
    assert paths.storage_file.name == "storage.json"


def test_linux_trae_state_dir_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("CHAIERWW_TRAE_STATE_DIR", str(tmp_path / "custom"))

    paths = trae_module._linux_paths()

    assert paths.state_dir == tmp_path / "custom"


def test_linux_workbuddy_paths_follow_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("CHAIERWW_WORKBUDDY_STATE_DIR", raising=False)

    paths = workbuddy_module._linux_paths()

    assert paths.state_dir == (
        tmp_path / "CodeBuddyExtension" / "Data" / "Public" / "auth"
    )
    assert paths.state_file.name == STATE_FILENAME
