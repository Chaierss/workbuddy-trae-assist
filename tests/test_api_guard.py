from __future__ import annotations

from fastapi.testclient import TestClient

from chaierww.api.guard import CLIENT_HEADER, TOKEN_COOKIE
from chaierww.config import Settings
from chaierww.main import create_app


def _client(tmp_path) -> TestClient:
    app = create_app(Settings(home=tmp_path, api_guard=True))
    return TestClient(app, base_url="http://127.0.0.1:57910")


def test_health_is_public(tmp_path):
    with _client(tmp_path) as client:
        assert client.get("/api/health").status_code == 200


def test_api_requires_session_cookie(tmp_path):
    with _client(tmp_path) as client:
        assert client.get("/api/scopes").status_code == 401
        session = client.get("/api/auth/session")
        assert session.status_code == 200
        assert session.json() == {"guard": True}
        assert TOKEN_COOKIE in session.cookies
        assert client.get("/api/scopes").status_code == 200


def test_bearer_token_is_accepted(tmp_path):
    with _client(tmp_path) as client:
        token = (tmp_path / "api-token").read_text(encoding="utf-8").strip()
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get("/api/scopes", headers=headers).status_code == 200
        assert (
            client.get("/api/scopes", headers={"Authorization": "Bearer wrong"}).status_code
            == 401
        )


def test_host_and_origin_are_checked(tmp_path):
    with _client(tmp_path) as client:
        client.get("/api/auth/session")
        assert client.get("/api/health", headers={"host": "evil.com"}).status_code == 403
        assert (
            client.get("/api/scopes", headers={"origin": "http://evil.com"}).status_code
            == 403
        )
        assert (
            client.get(
                "/api/scopes", headers={"origin": "http://localhost:57911"}
            ).status_code
            == 200
        )
        assert (
            client.get("/api/scopes", headers={"sec-fetch-site": "cross-site"}).status_code
            == 403
        )


def test_unsafe_method_needs_client_header(tmp_path):
    with _client(tmp_path) as client:
        client.get("/api/auth/session")
        body = {"keys": ["workbuddy.daily_checkin"]}
        assert client.post("/api/activities/run", json=body).status_code == 403
        assert (
            client.post(
                "/api/activities/run",
                json=body,
                headers={CLIENT_HEADER: "web"},
            ).status_code
            == 200
        )


def test_guard_off_allows_everything(tmp_path):
    app = create_app(Settings(home=tmp_path))
    with TestClient(app) as client:
        assert client.get("/api/scopes").status_code == 200
        assert client.get("/api/auth/session").json() == {"guard": False}


def test_token_file_is_stable(tmp_path):
    with _client(tmp_path) as client:
        first = (tmp_path / "api-token").read_text(encoding="utf-8").strip()
    with _client(tmp_path) as client:
        second = (tmp_path / "api-token").read_text(encoding="utf-8").strip()
        assert first == second
        assert len(first) >= 32
        assert client.get("/api/health").status_code == 200
