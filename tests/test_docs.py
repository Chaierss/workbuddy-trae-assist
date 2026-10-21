from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chaierww.config import Settings
from chaierww.main import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(home=tmp_path / "home"))
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def docs_dir(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    (root / "guide").mkdir(parents=True)
    (root / "README.md").write_text("# 文档首页\n\n正文\n", encoding="utf-8")
    (root / "guide" / "01-start.md").write_text("# 快速开始\n\n步骤\n", encoding="utf-8")
    (root / "notes.txt").write_text("不是 markdown", encoding="utf-8")
    monkeypatch.setenv("CHAIERWW_DOCS", str(root))
    return root


def test_lists_only_markdown_with_titles(client, docs_dir):
    body = client.get("/api/docs").json()
    paths = {item["path"] for item in body["files"]}

    assert paths == {"README.md", "guide/01-start.md"}
    assert body["root"] == str(docs_dir)

    by_path = {item["path"]: item for item in body["files"]}
    assert by_path["README.md"]["title"] == "文档首页"
    assert by_path["guide/01-start.md"]["title"] == "快速开始"
    assert by_path["guide/01-start.md"]["group"] == "guide"
    assert by_path["README.md"]["group"] == ""


def test_content_returns_markdown_body(client, docs_dir):
    body = client.get("/api/docs/content", params={"path": "guide/01-start.md"}).json()
    assert body["title"] == "快速开始"
    assert "步骤" in body["content"]


def test_content_rejects_path_traversal(client, docs_dir, tmp_path):
    secret = tmp_path / "secret.md"
    secret.write_text("# 机密", encoding="utf-8")

    response = client.get("/api/docs/content", params={"path": "../secret.md"})
    assert response.status_code == 400
    assert "机密" not in response.text


def test_content_rejects_non_markdown(client, docs_dir):
    response = client.get("/api/docs/content", params={"path": "notes.txt"})
    assert response.status_code == 400


def test_content_missing_doc_is_404(client, docs_dir):
    response = client.get("/api/docs/content", params={"path": "nope.md"})
    assert response.status_code == 404


def test_missing_docs_dir_degrades_to_empty_list(client, monkeypatch, tmp_path):
    monkeypatch.setenv("CHAIERWW_DOCS", str(tmp_path / "not-here"))
    body = client.get("/api/docs").json()
    assert body == {"root": None, "files": []}


def test_wiki_api_does_not_shadow_docs_page(client):
    """/docs 现在是我们自建的 Swagger 壳，Wiki 接口挂在 /api/docs 下，两者不能互相盖住。"""
    wiki = client.get("/api/docs")
    assert wiki.status_code == 200
    assert "files" in wiki.json()

    docs_page = client.get("/docs")
    assert "cdn.jsdelivr.net" not in docs_page.text


def test_openapi_catalog_derives_from_routes(client):
    body = client.get("/api/docs/openapi").json()

    assert body["count"] == len(body["endpoints"])
    pairs = {(item["method"], item["path"]) for item in body["endpoints"]}
    assert ("POST", "/api/switch") in pairs
    assert ("GET", "/api/accounts") in pairs

    # 每个接口都必须有中文摘要与分组，否则接口文档页会退化成一张裸路径表。
    for item in body["endpoints"]:
        assert item["summary"], f"{item['method']} {item['path']} 缺少 summary"
        assert item["tags"], f"{item['method']} {item['path']} 缺少 tags"


def test_openapi_catalog_reflects_param_types(client):
    endpoints = {
        (item["method"], item["path"]): item
        for item in client.get("/api/docs/openapi").json()["endpoints"]
    }
    params = {p["name"]: p for p in endpoints[("GET", "/api/accounts")]["parameters"]}

    assert params["scope"]["required"] is True
    assert params["scope"]["type"] == "string"
    # 可选参数被 pydantic 包成 anyOf，不能把整段 schema 直接甩给前端。
    assert params["q"]["required"] is False
    assert params["q"]["type"] == "string"
