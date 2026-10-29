"""企业登录、管理员 API 与 CSRF。"""

from __future__ import annotations

import base64
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from coding_agent.config import Settings
from coding_agent.control.accounts import UserStore
from coding_agent.control.approvals import ApprovalStore
from coding_agent.control.mfa import _totp
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


def test_mfa_login_requires_second_factor(tmp_path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={"mfa_enabled": True}
    )
    _seed_admin(settings)
    app = web_app.create_app(settings)
    client = TestClient(app)
    csrf = _login(client, "admin", "password-123")

    enrolled = client.post(
        "/api/auth/mfa/enroll",
        headers={"X-CSRF-Token": csrf},
        json={"password": "password-123"},
    )
    assert enrolled.status_code == 200, enrolled.text
    secret = enrolled.json()["secret"]
    step = int(time.time()) // 30
    confirmed = client.post(
        "/api/auth/mfa/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"code": _totp(secret, step)},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert len(confirmed.json()["recovery_codes"]) == 8

    second = TestClient(app)
    login = second.post(
        "/api/auth/login",
        json={"username": "admin", "password": "password-123"},
    )
    assert login.status_code == 200, login.text
    assert login.json()["mfa_required"] is True
    token = login.json()["mfa_token"]
    next_step = int(time.time()) // 30 + 1
    verified = second.post(
        "/api/auth/mfa/verify",
        json={"mfa_token": token, "code": _totp(secret, next_step)},
    )
    assert verified.status_code == 200, verified.text
    assert second.get("/api/me").status_code == 200


def test_four_eyes_admin_queue_and_decision(tmp_path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={"approval_four_eyes": True}
    )
    _seed_admin(settings)
    app = web_app.create_app(settings)
    admin = TestClient(app)
    csrf = _login(admin, "admin", "password-123")
    created = admin.post(
        "/api/admin/users",
        headers={"X-CSRF-Token": csrf},
        json={"username": "bob", "password": "password-123"},
    ).json()["user"]

    control = ControlStore(settings.control_db_url)
    try:
        ApprovalStore(control.engine).record_pending(
            tenant="default",
            thread_id="t1",
            call_id="c1",
            tool="shell_exec",
            command="rm -rf build",
            level="L3",
            level_int=3,
            requested_by=created["username"],
        )
    finally:
        control.close()

    pending = admin.get("/api/admin/approvals")
    assert pending.status_code == 200
    rows = pending.json()["approvals"]
    assert rows and rows[0]["requested_by"] == "bob"
    decided = admin.post(
        f"/api/admin/approvals/{rows[0]['id']}/decide",
        headers={"X-CSRF-Token": csrf},
        json={"approved": True},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["approval"]["status"] == "approved"


def test_admin_cannot_approve_own_request(tmp_path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={"approval_four_eyes": True}
    )
    _seed_admin(settings)
    admin = TestClient(web_app.create_app(settings))
    csrf = _login(admin, "admin", "password-123")
    control = ControlStore(settings.control_db_url)
    try:
        ApprovalStore(control.engine).record_pending(
            tenant="default",
            thread_id="t1",
            call_id="c-self",
            tool="shell_exec",
            command="rm -rf build",
            level="L3",
            level_int=3,
            requested_by="admin",
        )
    finally:
        control.close()
    row = admin.get("/api/admin/approvals").json()["approvals"][0]
    denied = admin.post(
        f"/api/admin/approvals/{row['id']}/decide",
        headers={"X-CSRF-Token": csrf},
        json={"approved": True},
    )
    assert denied.status_code == 409


def test_admin_mfa_reset_disables_and_revokes(tmp_path) -> None:
    from coding_agent.control.mfa import MfaStore

    settings = _settings(tmp_path).model_copy(
        update={"mfa_enabled": True}
    )
    _seed_admin(settings)
    app = web_app.create_app(settings)
    admin = TestClient(app)
    csrf = _login(admin, "admin", "password-123")
    me = admin.get("/api/me").json()["user"]
    enrolled = admin.post(
        "/api/auth/mfa/enroll",
        headers={"X-CSRF-Token": csrf},
        json={"password": "password-123"},
    ).json()
    step = int(time.time()) // 30
    confirmed = admin.post(
        "/api/auth/mfa/confirm",
        headers={"X-CSRF-Token": csrf},
        json={"code": _totp(enrolled["secret"], step)},
    )
    assert confirmed.status_code == 200, confirmed.text

    reset = admin.post(
        f"/api/admin/users/{me['id']}/mfa-reset",
        headers={"X-CSRF-Token": csrf},
    )
    assert reset.status_code == 200, reset.text
    control = ControlStore(settings.control_db_url)
    try:
        mfa = MfaStore(control.engine, base64.b64decode(MASTER))
        assert mfa.status(me["id"])["enabled"] is False
    finally:
        control.close()
    assert admin.get("/api/me").status_code == 401


def _seed_roles(settings: Settings) -> None:
    store = ControlStore(settings.control_db_url)
    try:
        users = UserStore(store.engine, tenant="default")
        users.create("admin", "password-123", role="owner")
        users.create("audit", "password-123", role="auditor")
        users.create("review", "password-123", role="approver")
    finally:
        store.close()


def test_auditor_is_read_only_across_admin_api(tmp_path) -> None:
    settings = _settings(tmp_path)
    _seed_roles(settings)
    client = TestClient(web_app.create_app(settings))
    csrf = _login(client, "audit", "password-123")

    assert client.get("/api/admin/users").status_code == 200
    assert client.get("/api/admin/usage?from=2026-10-01&to=2026-10-31").status_code == 200
    assert client.get("/api/admin/auth-events").status_code == 200
    # 审批队列不是审计只读角色的范围。
    assert client.get("/api/admin/approvals").status_code == 403

    denied = client.post(
        "/api/admin/users",
        headers={"X-CSRF-Token": csrf},
        json={"username": "mallory", "password": "password-123"},
    )
    assert denied.status_code == 403
    assert denied.json()["code"] == "auth.read_only"
    # 退出登录仍允许：只读不等于登不出去。
    assert client.post(
        "/api/auth/logout", headers={"X-CSRF-Token": csrf}
    ).status_code == 200


def test_approver_only_handles_approvals(tmp_path) -> None:
    settings = _settings(tmp_path)
    _seed_roles(settings)
    client = TestClient(web_app.create_app(settings))
    csrf = _login(client, "review", "password-123")

    approvals = client.get("/api/admin/approvals")
    assert approvals.status_code == 200
    assert approvals.json()["enabled"] is False
    assert client.get("/api/admin/users").status_code == 403
    denied = client.post(
        "/api/admin/users",
        headers={"X-CSRF-Token": csrf},
        json={"username": "mallory", "password": "password-123"},
    )
    assert denied.status_code == 403


def test_admin_grants_workspace_read_acl(tmp_path) -> None:
    settings = _settings(tmp_path).model_copy(
        update={
            "workspace_registry": True,
            "workspace_root": "/srv/ws",
        }
    )
    store = ControlStore(settings.control_db_url)
    try:
        users = UserStore(store.engine, tenant="default")
        users.create("admin", "password-123", role="owner")
        users.create("alice", "password-123", role="member")
        users.create("bob", "password-123", role="member")
        users.create("audit", "password-123", role="auditor")
    finally:
        store.close()
    app = web_app.create_app(settings)

    alice = TestClient(app)
    alice_csrf = _login(alice, "alice", "password-123")
    created = alice.post(
        "/api/workspaces",
        headers={"X-CSRF-Token": alice_csrf},
        json={"display_name": "Shared"},
    )
    assert created.status_code == 200, created.text
    workspace_id = created.json()["workspace_id"]

    admin = TestClient(app)
    csrf = _login(admin, "admin", "password-123")
    granted = admin.put(
        f"/api/admin/workspaces/{workspace_id}/acl",
        headers={"X-CSRF-Token": csrf},
        json={"grantee": "bob", "permission": "read"},
    )
    assert granted.status_code == 200, granted.text
    assert granted.json()["grant"]["grantee"] == "bob"

    listed = admin.get(f"/api/admin/workspaces/{workspace_id}/acl")
    assert [item["grantee"] for item in listed.json()["grants"]] == ["bob"]

    bob = TestClient(app)
    bob_csrf = _login(bob, "bob", "password-123")
    rows = bob.get("/api/workspaces").json()["workspaces"]
    assert [row["workspace_id"] for row in rows] == [workspace_id]
    assert rows[0]["acl_role"] == "read"
    assert (
        bob.delete(
            f"/api/workspaces/{workspace_id}",
            headers={"X-CSRF-Token": bob_csrf},
        ).status_code
        == 404
    )

    removed = admin.delete(
        f"/api/admin/workspaces/{workspace_id}/acl/bob",
        headers={"X-CSRF-Token": csrf},
    )
    assert removed.status_code == 200
    assert removed.json()["removed"] is True
    assert bob.get("/api/workspaces").json()["workspaces"] == []

    # 审计只读角色不能改 ACL。
    audit = TestClient(app)
    audit_csrf = _login(audit, "audit", "password-123")
    denied = audit.put(
        f"/api/admin/workspaces/{workspace_id}/acl",
        headers={"X-CSRF-Token": audit_csrf},
        json={"grantee": "bob"},
    )
    assert denied.status_code == 403


def test_admin_sets_user_access_limit(tmp_path) -> None:
    settings = _settings(tmp_path)
    _seed_admin(settings)
    client = TestClient(web_app.create_app(settings))
    csrf = _login(client, "admin", "password-123")
    created = client.post(
        "/api/admin/users",
        headers={"X-CSRF-Token": csrf},
        json={
            "username": "limited",
            "password": "password-123",
            "max_access_mode": "read_only",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["user"]["max_access_mode"] == "read_only"
    user_id = created.json()["user"]["id"]

    updated = client.patch(
        f"/api/admin/users/{user_id}",
        headers={"X-CSRF-Token": csrf},
        json={"max_access_mode": "write_ask"},
    )
    assert updated.status_code == 200
    assert updated.json()["user"]["max_access_mode"] == "write_ask"
