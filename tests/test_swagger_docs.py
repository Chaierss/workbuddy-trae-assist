from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from chaierww.config import Settings
from chaierww.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("CHAIERWW_DISABLE_DOCS", raising=False)
    app = create_app(Settings(home=tmp_path))
    with TestClient(app) as test_client:
        yield test_client


def test_docs_page_is_self_hosted_swagger(client):
    response = client.get("/docs")
    assert response.status_code == 200
    html = response.text

    # 必须是自己的页面：不引用任何 CDN，否则离线/内网直接白屏。
    assert "cdn.jsdelivr.net" not in html
    assert "/docs-assets/swagger-ui-bundle.js" in html
    assert "/api/openapi.json" in html
    assert "chaierww 接口文档" in html


def test_docs_page_allows_same_origin_iframe(client):
    response = client.get("/docs")
    # 默认 DENY 会让设置里的「接口文档」tab 白屏。
    assert response.headers["x-frame-options"] == "SAMEORIGIN"
    assert "no-cache" in response.headers["cache-control"]


def test_docs_assets_are_served_locally(client):
    assert client.get("/docs-assets/swagger-ui.css").status_code == 200
    assert client.get("/docs-assets/swagger-ui-bundle.js").status_code == 200


def test_docs_status_reports_available(client):
    body = client.get("/api/docs/status").json()
    assert body["available"] is True
    assert body["docsUrl"] == "/docs"
    assert body["openapiUrl"] == "/api/openapi.json"
    assert body["reason"] == ""


def test_openapi_json_is_the_single_source(client):
    spec = client.get("/api/openapi.json").json()
    paths = spec["paths"]
    assert "/api/switch" in paths
    assert "/api/logs" in paths
    assert spec["info"]["title"] == "chaierww"


def test_docs_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAIERWW_DISABLE_DOCS", "1")
    app = create_app(Settings(home=tmp_path))
    with TestClient(app) as test_client:
        status = test_client.get("/api/docs/status").json()
        assert status["available"] is False
        assert "CHAIERWW_DISABLE_DOCS" in status["reason"]

        # 禁用时不能把 SPA 的 index.html 兜进来，否则表现为「文档里套着整个应用」。
        assert test_client.get("/docs").status_code == 404
