"""Phase F：控制面密钥不落库/不返回，以及脱敏边界。"""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from ubismart import web
from ubismart.config import Settings
from ubismart.control.credentials import CredentialStore
from ubismart.control.schema import ControlProviderProfile
from ubismart.control.store import ControlStore
from ubismart.security.secrets import redact_secrets

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


def test_control_db_stores_only_ciphertext_for_provider_keys(tmp_path) -> None:
    """REQ-135 的可执行断言：密文列与数据库字节都不得出现明文。"""
    url = f"sqlite:///{tmp_path / 'control.db'}"
    store = ControlStore(url)
    credentials = CredentialStore(store.engine, b"k" * 32)
    secrets = [
        "sk-live-phasef-9876",
        "ghp_phasef_token_2468",
        "AKIAIOSFODNN7PHASE",
    ]
    for index, secret in enumerate(secrets):
        credentials.set(
            "default", "alice", f"provider-{index}", secret
        )
    with store.engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT ciphertext, last4 FROM control_provider_keys"
        ).fetchall()
    assert len(rows) == len(secrets)
    ciphertext = "".join(str(row[0]) for row in rows)
    for secret in secrets:
        assert secret not in ciphertext
        assert secret[:8] not in ciphertext
    assert {str(row[1]) for row in rows} == {
        secret[-4:] for secret in secrets
    }
    raw = (tmp_path / "control.db").read_bytes()
    for secret in secrets:
        assert secret.encode() not in raw
        assert secret[:8].encode() not in raw
    store.close()


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
