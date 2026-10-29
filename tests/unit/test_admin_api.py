"""企业登录、管理员 API 与 CSRF。"""

from __future__ import annotations

import base64
import json
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

    listed_ws = admin.get("/api/admin/workspaces").json()["workspaces"]
    mine = [
        row for row in listed_ws if row["workspace_id"] == workspace_id
    ]
    assert mine and mine[0]["acl_role"] == "admin"

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
    audit_rows = audit.get("/api/admin/workspaces").json()["workspaces"]
    audit_mine = [
        row for row in audit_rows if row["workspace_id"] == workspace_id
    ]
    assert audit_mine and "root_path" not in audit_mine[0]
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


def test_admin_audit_query_and_health(tmp_path) -> None:
    settings = _settings(tmp_path)
    _seed_admin(settings)
    store = ControlStore(settings.control_db_url)
    try:
        store.index_audit(
            "admin",
            [
                {
                    "hash": "h1",
                    "thread_id": "t1",
                    "ts": "2026-10-29T10:00:00+00:00",
                    "kind": "tool_call",
                    "tool": "shell_exec",
                    "level": "L0",
                    "decision": "auto",
                    "ok": True,
                },
                {
                    "hash": "h2",
                    "thread_id": "t1",
                    "ts": "2026-10-29T11:00:00+00:00",
                    "kind": "tool_call",
                    "tool": "file_edit",
                    "level": "L2",
                    "decision": "ask",
                    "ok": False,
                },
            ],
        )
    finally:
        store.close()

    client = TestClient(web_app.create_app(settings))
    _login(client, "admin", "password-123")

    data = client.get("/api/admin/audit?user=admin").json()
    assert data["total"] == 2
    assert [row["record_hash"] for row in data["records"]] == ["h2", "h1"]
    assert data["records"][0]["tenant"] == "admin"

    failed = client.get("/api/admin/audit?user=admin&ok=false").json()
    assert failed["total"] == 1
    assert failed["records"][0]["tool"] == "file_edit"

    paged = client.get(
        "/api/admin/audit?user=admin&limit=1&offset=1"
    ).json()
    assert paged["total"] == 2
    assert [row["record_hash"] for row in paged["records"]] == ["h1"]
    assert paged["has_more"] is False

    health = client.get("/api/admin/health").json()
    assert health["ok"] is True
    assert health["database"]["ok"] is True
    assert health["api_version"] == "v1"
    assert isinstance(health["metrics"], dict)


def test_approval_history_and_batch(tmp_path) -> None:
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
        approvals = ApprovalStore(control.engine)
        for call_id in ("c1", "c2"):
            approvals.record_pending(
                tenant="default",
                thread_id="t1",
                call_id=call_id,
                tool="shell_exec",
                command="ls",
                level="L3",
                level_int=3,
                requested_by=created["username"],
            )
        approvals.record_pending(
            tenant="default",
            thread_id="t2",
            call_id="self",
            tool="shell_exec",
            command="rm -rf build",
            level="L3",
            level_int=3,
            requested_by="admin",
        )
    finally:
        control.close()

    history = admin.get(
        "/api/admin/approvals/history?status=pending"
    ).json()
    assert history["enabled"] is True
    assert history["total"] == 3
    assert all(row["created_at_iso"] for row in history["records"])

    ids = [
        row["id"]
        for row in history["records"]
        if row["requested_by"] == "bob"
    ]
    batch = admin.post(
        "/api/admin/approvals/batch",
        headers={"X-CSRF-Token": csrf},
        json={"ids": ids, "approved": True},
    )
    assert batch.status_code == 200, batch.text
    assert batch.json()["updated"] == 2

    self_row = next(
        row for row in history["records"] if row["requested_by"] == "admin"
    )
    denied = admin.post(
        "/api/admin/approvals/batch",
        headers={"X-CSRF-Token": csrf},
        json={"ids": [self_row["id"]], "approved": True},
    )
    assert denied.json()["updated"] == 0
    assert denied.json()["results"][0]["ok"] is False

    approved = admin.get(
        "/api/admin/approvals/history?status=approved&user=bob"
    ).json()
    assert approved["total"] == 2
    assert all(row["status"] == "approved" for row in approved["records"])


def test_personal_access_token_scopes(tmp_path) -> None:
    settings = _settings(tmp_path)
    _seed_admin(settings)
    app = web_app.create_app(settings)
    client = TestClient(app)
    csrf = _login(client, "admin", "password-123")

    created = client.post(
        "/api/tokens",
        headers={"X-CSRF-Token": csrf},
        json={"name": "ci", "scopes": ["read"], "expires_in_days": 7},
    )
    assert created.status_code == 200, created.text
    token = created.json()["token"]
    assert token.startswith("ca_pat_")
    listed = client.get("/api/tokens").json()
    assert listed["enabled"] is True
    assert token not in str(listed["tokens"])

    read_pat = TestClient(
        app, headers={"Authorization": f"Bearer {token}"}
    )
    assert read_pat.get("/api/me").status_code == 200
    assert (
        read_pat.post(
            "/api/sessions",
            json={"thread_id": "t1", "workspace": "/tmp/ws"},
        ).status_code
        == 403
    )
    # PAT 不能管理 PAT/Webhook 或访问管理端。
    assert read_pat.get("/api/tokens").status_code == 403
    assert read_pat.get("/api/webhooks").status_code == 403
    assert read_pat.get("/api/admin/users").status_code == 403

    run_created = client.post(
        "/api/tokens",
        headers={"X-CSRF-Token": csrf},
        json={"name": "runner", "scopes": ["run"], "expires_in_days": 1},
    )
    run_token = run_created.json()["token"]
    run_pat = TestClient(
        app, headers={"Authorization": f"Bearer {run_token}"}
    )
    session_response = run_pat.post(
        "/api/sessions",
        json={"thread_id": "t1", "workspace": "/tmp/ws"},
    )
    assert session_response.status_code != 403

    audit_created = client.post(
        "/api/tokens",
        headers={"X-CSRF-Token": csrf},
        json={"name": "audit", "scopes": ["audit"], "expires_in_days": 1},
    )
    audit_token = audit_created.json()["token"]
    audit_pat = TestClient(
        app, headers={"Authorization": f"Bearer {audit_token}"}
    )
    assert read_pat.get("/api/audit").status_code == 403
    # audit scope 已通过授权；本地无 WSL 时下游可能 503，这里只验证不是 403。
    assert audit_pat.get("/api/audit").status_code != 403

    token_id = created.json()["record"]["id"]
    revoked = client.delete(
        f"/api/tokens/{token_id}",
        headers={"X-CSRF-Token": csrf},
    )
    assert revoked.status_code == 200
    assert read_pat.get("/api/me").status_code == 401


def test_admin_reindex_search(tmp_path) -> None:
    settings = _settings(tmp_path)
    _seed_admin(settings)
    client = TestClient(web_app.create_app(settings))
    csrf = _login(client, "admin", "password-123")
    response = client.post(
        "/api/admin/search/reindex",
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert response.json()["reindexed"] >= 0


def test_approval_stream_disabled_sends_one_frame(tmp_path) -> None:
    settings = _settings(tmp_path)
    _seed_admin(settings)
    client = TestClient(web_app.create_app(settings))
    _login(client, "admin", "password-123")
    with client.stream("GET", "/api/admin/approvals/stream") as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith(
            "text/event-stream"
        )
        frame = ""
        for line in response.iter_lines():
            if line.startswith("data:"):
                frame = line[5:].strip()
                break
    assert json.loads(frame)["enabled"] is False
