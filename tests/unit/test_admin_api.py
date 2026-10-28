"""企业登录、管理员 API 与 CSRF。"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coding_agent.config import Settings
from coding_agent.control.accounts import UserStore
from coding_agent.control.store import ControlStore
from coding_agent.web import app as web_app

MASTER = base64.b64encode(b"k" * 32).decode("ascii")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        deepseek_api_key="sk-env",
        wsl_workspace="/tmp/ws",
        web_data_root=str(tmp_path / "web-data"),
        web_auth_tokens="",
        web_allow_anonymous_local=False,
        control_db_url=f"sqlite:///{tmp_path / 'control.db'}",
        control_db_auto_create=True,
        web_accounts_enabled=True,
        web_accounts_require_mysql=False,
        web_account_tenant="default",
        secret_master_key=MASTER,
    )


def test_accounts_require_mysql_by_default(tmp_path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={"web_accounts_require_mysql": True}
    )
    with pytest.raises(RuntimeError, match="MySQL"):
        web_app.create_app(settings)


def _seed_admin(settings: Settings) -> None:
    store = ControlStore(settings.control_db_url)
    try:
        UserStore(store.engine, tenant="default").create(
            "admin", "password-123", role="owner"
        )
    finally:
        store.close()


def _login(client: TestClient, username: str, password: str) -> str:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def test_login_admin_flow_and_csrf(tmp_path) -> None:
    settings = _settings(tmp_path)
    _seed_admin(settings)
    client = TestClient(web_app.create_app(settings))

    csrf = _login(client, "admin", "password-123")
    me = client.get("/api/me")
    assert me.status_code == 200
    assert me.json()["user"]["role"] == "owner"
    events = client.get("/api/admin/auth-events?user=admin").json()["events"]
    assert events and events[0]["event"] == "login_ok"

    # Cookie 身份的非幂等请求必须带 CSRF
    denied = client.post(
        "/api/admin/users",
        json={"username": "alice", "password": "password-123"},
    )
    assert denied.status_code == 403

    created = client.post(
        "/api/admin/users",
        headers={"X-CSRF-Token": csrf},
        json={
            "username": "alice",
            "password": "password-123",
            "email": "a@example.com",
            "role": "member",
        },
    )
    assert created.status_code == 200, created.text
    user_id = created.json()["user"]["id"]

    quota = client.put(
        f"/api/admin/users/{user_id}/quotas",
        headers={"X-CSRF-Token": csrf},
        json={"daily_tokens": 1000, "daily_cost_cny": 7.2, "enabled": True},
    )
    assert quota.status_code == 200
    assert quota.json()["quota"]["daily_tokens"] == 1000

    me_id = me.json()["user"]["id"]
    last_owner = client.patch(
        f"/api/admin/users/{me_id}",
        headers={"X-CSRF-Token": csrf},
        json={"status": "disabled"},
    )
    assert last_owner.status_code == 409

    providers = client.get(f"/api/admin/users/{user_id}/providers").json()
    assert any(item["provider"] == "deepseek" for item in providers["providers"])

    saved = client.put(
        f"/api/admin/users/{user_id}/providers/deepseek",
        headers={"X-CSRF-Token": csrf},
        json={"api_key": "sk-user-key-5678"},
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["last4"] == "5678"

    usage = client.get("/api/admin/usage?from=2026-10-01&to=2026-10-31")
    assert usage.status_code == 200
    assert usage.json()["rows"] == []

    assert client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": csrf}
    ).status_code == 200
    assert client.get("/api/me").status_code == 401


def test_login_is_rate_limited_per_ip(tmp_path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={"web_login_rate_limit_per_min": 2}
    )
    _seed_admin(settings)
    client = TestClient(web_app.create_app(settings))
    for _ in range(2):
        response = client.post(
            "/api/auth/login",
            json={"username": "admin", "password": "wrong"},
        )
        assert response.status_code == 401
    limited = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "wrong"},
    )
    assert limited.status_code == 429


def test_non_admin_is_forbidden_and_disable_revokes(tmp_path) -> None:
    settings = _settings(tmp_path)
    _seed_admin(settings)
    admin = TestClient(web_app.create_app(settings))
    csrf = _login(admin, "admin", "password-123")
    created = admin.post(
        "/api/admin/users",
        headers={"X-CSRF-Token": csrf},
        json={"username": "bob", "password": "password-123"},
    ).json()["user"]

    member = TestClient(web_app.create_app(settings))
    _login(member, "bob", "password-123")
    assert member.get("/api/admin/users").status_code == 403

    disabled = admin.patch(
        f"/api/admin/users/{created['id']}",
        headers={"X-CSRF-Token": csrf},
        json={"status": "disabled"},
    )
    assert disabled.status_code == 200
    assert member.get("/api/me").status_code == 401
