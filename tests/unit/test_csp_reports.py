"""CSP 违规上报：匿名、限流、URL 脱敏与认证边界。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coding_agent.config import Settings
from coding_agent.web import app as web_app
from coding_agent.web.auth import AuthConfig

LEGACY = {
    "csp-report": {
        "document-uri": "https://example.test/session?token=secret#frag",
        "violated-directive": "script-src 'self'",
        "effective-directive": "script-src",
        "blocked-uri": "https://evil.test/x.js?q=1",
        "source-file": "https://example.test/app.js",
        "line-number": 12,
        "column-number": 3,
        "disposition": "enforce",
        "script-sample": "alert(1)",
    }
}


def _settings(tmp_path: Path, **updates) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        audit_dir=str(tmp_path / "audit"),
        web_data_root=str(tmp_path / "web-data"),
        web_allow_anonymous_local=False,
        **updates,
    )


def _client(settings: Settings) -> TestClient:
    auth = AuthConfig(tokens={"tok": "alice"}, allow_anonymous_local=False)
    return TestClient(web_app.create_app(settings, auth))


def test_csp_reports_are_anonymous_and_redacted(tmp_path) -> None:
    settings = _settings(tmp_path)
    client = _client(settings)

    legacy = client.post(
        "/api/client-errors",
        json=LEGACY,
        headers={"Content-Type": "application/csp-report"},
    )
    assert legacy.status_code == 200, legacy.text

    reporting = client.post(
        "/api/client-errors",
        json=[
            {
                "type": "csp-violation",
                "body": {
                    "document-uri": "https://example.test/",
                    "effective-directive": "img-src",
                    "blocked-uri": "data:image/png",
                    "disposition": "report",
                },
            }
        ],
        headers={"Content-Type": "application/reports+json"},
    )
    assert reporting.status_code == 200, reporting.text

    text = (
        Path(settings.resolved_audit_dir) / "client-errors.jsonl"
    ).read_text(encoding="utf-8")
    assert '"kind": "csp"' in text
    assert '"user": "anonymous"' in text
    assert "token=secret" not in text
    assert "evil.test/x.js?q=1" not in text
    assert "evil.test/x.js" in text
    assert '"disposition": "report"' in text


def test_client_errors_still_require_auth(tmp_path) -> None:
    client = _client(_settings(tmp_path))
    denied = client.post(
        "/api/client-errors", json={"message": "boom"}
    )
    assert denied.status_code == 401

    allowed = client.post(
        "/api/client-errors",
        json={"message": "boom"},
        headers={"Authorization": "Bearer tok"},
    )
    assert allowed.status_code == 200
    bad = client.post(
        "/api/client-errors",
        json={"message": ""},
        headers={"Authorization": "Bearer tok"},
    )
    assert bad.status_code == 422


def test_csp_reports_are_rate_limited_by_ip(tmp_path) -> None:
    client = _client(
        _settings(tmp_path, web_csp_report_rate_limit_per_min=1)
    )
    first = client.post("/api/client-errors", json=LEGACY)
    assert first.status_code == 200
    second = client.post("/api/client-errors", json=LEGACY)
    assert second.status_code == 429


def test_readyz_reports_collaboration_backends(tmp_path) -> None:
    client = _client(_settings(tmp_path))
    data = client.get("/readyz").json()
    assert data["checks"]["event_transport"] in {"memory", "none"}
    assert data["checks"]["rate_limit"] == "memory"
    assert data["checks"]["credentials"] in {
        "keyring",
        "unconfigured",
        "db-encrypted",
    }
    assert data["checks"]["shared_state"] == "false"


def test_redis_transport_requires_stream_buffer(tmp_path) -> None:
    settings = _settings(
        tmp_path,
        event_transport="redis",
        event_transport_url="redis://127.0.0.1:6379/0",
    )
    with pytest.raises(ValueError, match="stream_buffer"):
        web_app.create_app(settings)


def test_csp_header_exposes_report_endpoint(tmp_path) -> None:
    client = _client(_settings(tmp_path))
    response = client.get("/")
    csp = response.headers["content-security-policy"]
    assert "report-uri /api/client-errors" in csp
    assert "report-to csp-endpoint" in csp
    assert (
        response.headers["reporting-endpoints"]
        == 'csp-endpoint="/api/client-errors"'
    )
