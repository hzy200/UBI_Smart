"""CORS 允许列表、预检与错误响应头。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from coding_agent import web
from coding_agent.config import Settings
from coding_agent.web.cors import CorsConfigError, parse_allowed_origins

ORIGIN = "https://app.example.com"

ALLOWED_REQUEST_HEADERS = {
    "authorization",
    "content-type",
    "idempotency-key",
    "last-event-id",
    "x-request-id",
    "x-frontend-version",
    "x-csrf-token",
}


def _settings(tmp_path, **updates) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        web_data_root=str(tmp_path / "web-data"),
        web_cors_allowed_origins=updates.pop(
            "web_cors_allowed_origins", ORIGIN
        ),
        **updates,
    )


def test_parse_allowed_origins_normalizes_and_dedupes() -> None:
    assert parse_allowed_origins("") == []
    assert parse_allowed_origins(
        " https://App.Example.com/ , http://localhost:5173,"
        "https://app.example.com "
    ) == [ORIGIN, "http://localhost:5173"]


@pytest.mark.parametrize(
    "raw",
    [
        "*",
        "null",
        "https://app.example.com/path",
        "ftp://app.example.com",
        "https://user:pass@app.example.com",
        "https://app.example.com?x=1",
        "https://app.example.com#frag",
        "https://*.example.com",
        "https://app.example.com:bad",
    ],
)
def test_parse_allowed_origins_rejects_invalid_entries(raw: str) -> None:
    with pytest.raises(CorsConfigError):
        parse_allowed_origins(raw)


def test_invalid_cors_config_fails_app_startup(tmp_path) -> None:
    settings = _settings(tmp_path, web_cors_allowed_origins="*")
    with pytest.raises(CorsConfigError):
        web.create_app(settings)


def test_preflight_allows_exact_origin_and_headers(tmp_path) -> None:
    client = TestClient(web.create_app(_settings(tmp_path)))
    response = client.options(
        "/api/capabilities",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "authorization,idempotency-key,"
                "x-frontend-version,x-csrf-token"
            ),
        },
    )
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "POST" in response.headers["access-control-allow-methods"]
    allowed = {
        item.strip().lower()
        for item in response.headers["access-control-allow-headers"].split(",")
    }
    assert ALLOWED_REQUEST_HEADERS <= allowed
    assert response.headers["access-control-max-age"] == "600"
    assert response.headers["x-request-id"]
    assert "access-control-allow-credentials" not in response.headers


def test_upload_preflight_allows_authorization_and_idempotency(
    tmp_path,
) -> None:
    client = TestClient(web.create_app(_settings(tmp_path)))
    response = client.options(
        "/api/sessions/t1/uploads",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": (
                "authorization,idempotency-key,content-type,"
                "x-frontend-version"
            ),
        },
    )
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == ORIGIN
    allowed = response.headers["access-control-allow-headers"].lower()
    for header in (
        "authorization",
        "idempotency-key",
        "content-type",
        "x-frontend-version",
    ):
        assert header in allowed


def test_simple_request_and_disallowed_origin(tmp_path) -> None:
    client = TestClient(web.create_app(_settings(tmp_path)))

    allowed = client.get("/healthz", headers={"Origin": ORIGIN})
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == ORIGIN
    assert "origin" in allowed.headers.get("vary", "").lower()

    denied = client.get(
        "/healthz", headers={"Origin": "https://evil.example.com"}
    )
    assert denied.status_code == 200
    assert "access-control-allow-origin" not in denied.headers


def test_cors_headers_survive_auth_and_version_errors(tmp_path) -> None:
    locked = _settings(
        tmp_path,
        web_auth_tokens='{"secret-token": "alice"}',
    )
    client = TestClient(web.create_app(locked))

    unauthorized = client.get(
        "/api/capabilities", headers={"Origin": ORIGIN}
    )
    assert unauthorized.status_code == 401
    assert unauthorized.headers["access-control-allow-origin"] == ORIGIN
    exposed = unauthorized.headers["access-control-expose-headers"].lower()
    assert "x-request-id" in exposed
    assert unauthorized.json()["code"] == "auth.required"

    old = client.get(
        "/api/capabilities",
        headers={"Origin": ORIGIN, "X-Frontend-Version": "0.0.1"},
    )
    assert old.status_code == 426
    assert old.headers["access-control-allow-origin"] == ORIGIN
    assert old.json()["code"] == "version.frontend_too_old"


def test_cors_headers_survive_body_limit_errors(tmp_path) -> None:
    settings = _settings(tmp_path, web_max_body_bytes=32)
    client = TestClient(web.create_app(settings))
    response = client.post(
        "/api/run",
        content=b"x" * 64,
        headers={
            "Origin": ORIGIN,
            "Content-Type": "application/json",
        },
    )
    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert response.json()["code"] == "request.too_large"


def test_no_cors_headers_when_allowlist_is_empty(tmp_path) -> None:
    settings = _settings(tmp_path, web_cors_allowed_origins="")
    client = TestClient(web.create_app(settings))
    response = client.get("/healthz", headers={"Origin": ORIGIN})
    assert "access-control-allow-origin" not in response.headers
