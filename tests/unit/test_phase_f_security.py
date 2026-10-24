"""Phase F：控制面密钥不落库/不返回，以及脱敏边界。"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from coding_agent import web
from coding_agent.config import Settings
from coding_agent.control.schema import ControlProviderProfile
from coding_agent.control.store import ControlStore
from coding_agent.security.secrets import redact_secrets

SECRET = "sk-phasef-SECRET-0123456789"


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key=SECRET,
        wsl_workspace=str(tmp_path / "ws"),
        control_db_url=f"sqlite:///{tmp_path / 'control.db'}",
    )


def test_provider_profile_schema_has_no_secret_column() -> None:
    columns = {column.name for column in ControlProviderProfile.__table__.columns}
    assert "api_key" not in columns
    assert "secret" not in columns
    assert "token" not in columns
    assert "password" not in columns


def test_control_db_does_not_contain_provider_key(tmp_path) -> None:
    app = web.create_app(_settings(tmp_path))
    client = TestClient(app)
    assert (
        client.post("/api/sessions", json={"thread_id": "t1"}).status_code
        == 200
    )
    store = ControlStore(
        f"sqlite:///{tmp_path / 'control.db'}", auto_create=False
    )
    store.index_audit(
        "local",
        [
            {
                "hash": "h1",
                "tool": "shell_exec",
                "level": "L0",
                "decision": "auto",
                "ok": True,
                "detail": SECRET,
                "command": SECRET,
            }
        ],
    )
    store.close()
    raw = (tmp_path / "control.db").read_bytes()
    assert SECRET.encode() not in raw


def test_providers_api_never_returns_key(tmp_path) -> None:
    app = web.create_app(_settings(tmp_path))
    client = TestClient(app)
    response = client.get("/api/providers")
    assert response.status_code == 200
    assert SECRET not in response.text
    assert "api_key" not in response.text


def test_secret_is_not_logged(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    app = web.create_app(_settings(tmp_path))
    client = TestClient(app)
    client.get("/api/providers")
    client.post("/api/sessions", json={"thread_id": "t1"})
    assert SECRET not in caplog.text


def test_redaction_removes_provider_key() -> None:
    text = f"command failed with {SECRET}"
    redacted = redact_secrets(text)
    assert SECRET not in redacted
    assert "REDACTED" in redacted
