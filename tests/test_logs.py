from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chaierww.config import Settings
from chaierww.main import create_app

LOG_BODY = "\n".join(
    [
        "2026-10-19 10:00:00 INFO    chaierww 启动完成",
        "2026-10-19 10:00:01 WARNING chaierww 客户端仍在运行",
        "2026-10-19 10:00:02 ERROR   chaierww 切换失败 token=abcdef123456 password=hunter2",
        "2026-10-19 10:00:03 INFO    chaierww 鉴权头 Authorization: Bearer eyJhbGciOiJIUzI1NiJ9",
        "",
    ]
)


@pytest.fixture
def client(tmp_path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir(parents=True)
    (logs_dir / "chaierww.log").write_text(LOG_BODY, encoding="utf-8")
    app = create_app(Settings(home=tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def _entries(client: TestClient, **params) -> list[dict]:
    response = client.get("/api/logs", params=params)
    assert response.status_code == 200, response.text
    return response.json()["entries"]


def test_lists_log_files(client):
    body = client.get("/api/logs/files").json()
    names = [item["name"] for item in body["files"]]
    assert "chaierww.log" in names
    assert body["dir"].endswith("logs")


def test_newest_line_comes_first(client):
    entries = _entries(client)
    assert len(entries) == 4
    assert entries[0]["at"] == "2026-10-19 10:00:03"
    assert entries[-1]["at"] == "2026-10-19 10:00:00"


def test_entries_are_redacted(client):
    entries = _entries(client)
    bearer = entries[0]["message"]
    assert "Bearer ***" in bearer
    assert "eyJhbGciOiJIUzI1NiJ9" not in bearer

    error_line = next(item for item in entries if item["level"] == "ERROR")
    assert "token=***" in error_line["message"]
    assert "password=***" in error_line["message"]
    assert "abcdef123456" not in error_line["message"]
    assert "hunter2" not in error_line["message"]


def test_level_filter(client):
    entries = _entries(client, level="warning")
    assert [item["level"] for item in entries] == ["WARNING"]


def test_keyword_filter(client):
    entries = _entries(client, keyword="客户端")
    assert len(entries) == 1
    assert "客户端仍在运行" in entries[0]["message"]

    assert _entries(client, keyword="并不存在的关键字") == []


def test_pagination_reports_more(client):
    body = client.get("/api/logs", params={"page": 1, "page_size": 2}).json()
    assert len(body["entries"]) == 2
    assert body["hasMore"] is True

    last = client.get("/api/logs", params={"page": 2, "page_size": 2}).json()
    assert len(last["entries"]) == 2
    assert last["hasMore"] is False


def test_unknown_level_is_rejected(client):
    response = client.get("/api/logs", params={"level": "TRACE"})
    assert response.status_code == 400


def test_file_name_must_not_escape_dir(client):
    response = client.get("/api/logs", params={"file": "../chaierww.db"})
    assert response.status_code == 400

    missing = client.get("/api/logs", params={"file": "nope.log"})
    assert missing.status_code == 404
