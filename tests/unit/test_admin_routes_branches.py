"""管理员路由的错误/降级分支（与 test_admin_api 的端到端用例互补）。"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from ubismart.config import Settings
from ubismart.control.accounts import AccountError
from ubismart.platform.secrets_store import SecretStoreUnavailable
from ubismart.web.auth import AuthUser
from ubismart.web.dependencies import current_user
from ubismart.web.routes.admin import (
    AdminDeps,
    _day,
    _EmptyStore,
    _epoch,
    register_admin_routes,
)


class _Accounts:
    def __init__(self) -> None:
        self.row: dict[str, Any] | None = {
            "id": "u1",
            "tenant": "default",
            "username": "alice",
            "role": "member",
            "status": "active",
        }
        self.create_error = ""
        self.update_error = ""
        self.reset_error = ""

    def get(self, user_id: str):
        return self.row

    def list(self, tenant: str):
        return [self.row] if self.row else []

    def create(self, *args, **kwargs):
        if self.create_error:
            raise AccountError(self.create_error)
        return self.row

    def update(self, *args, **kwargs):
        if self.update_error:
            raise AccountError(self.update_error)
        return self.row

    def reset_password(self, *args, **kwargs):
        if self.reset_error:
            raise AccountError(self.reset_error)

    def count_active_owners(self, tenant: str) -> int:
        return 2

    def get_by_username(self, username: str):
        return self.row


class _Sessions:
    def __init__(self) -> None:
        self.revoked: list[str] = []

    def revoke_user(self, user_id: str) -> None:
        self.revoked.append(user_id)


class _Control:
    def get_usage_day(self, tenant: str, user: str, day: str) -> dict:
        return {"tokens": 3, "cost_usd": 0.1, "billed_usd": 0.1}

    def get_quota(self, tenant: str, user: str) -> dict:
        return {"daily_tokens": 10, "daily_cost_cny": 1.0, "enabled": True}

    def set_quota(self, *args, **kwargs) -> dict:
        return {"daily_tokens": 10}

    def usage_report(self, *args, **kwargs) -> dict:
        return {"rows": [], "totals": {}}


def _deps(**overrides) -> AdminDeps:
    values: dict[str, Any] = {
        "settings": Settings(_env_file=None),
        "accounts": _Accounts(),
        "sessions": _Sessions(),
        "control_store": _Control(),
        "provider_rows": lambda settings, store: [],
        "user_store": lambda user: _EmptyStore(),
    }
    values.update(overrides)
    return AdminDeps(**values)


def _client(deps: AdminDeps) -> TestClient:
    app = FastAPI()
    register_admin_routes(app, deps)
    app.dependency_overrides[current_user] = lambda: AuthUser(
        name="root", tenant="default", role="owner"
    )
    return TestClient(app)


def test_empty_store_and_day_helpers() -> None:
    store = _EmptyStore()
    assert store.get("x") == ""
    assert store.has("x") is False
    fallback = date(2026, 10, 28)
    assert _day("not-a-date", fallback) == fallback
    assert _day("", fallback) == fallback
    assert _day("2026-10-01", fallback) == date(2026, 10, 1)


def test_accounts_disabled_returns_503() -> None:
    client = _client(_deps(accounts=None))
    response = client.get("/api/admin/users")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "auth.accounts_disabled"


def test_missing_account_and_create_conflict() -> None:
    deps = _deps()
    deps.accounts.row = None
    client = _client(deps)
    assert client.get("/api/admin/users/u1/quotas").status_code == 404

    deps = _deps()
    deps.accounts.create_error = "用户名已存在"
    client = _client(deps)
    response = client.post(
        "/api/admin/users",
        json={"username": "alice", "password": "password-123"},
    )
    assert response.status_code == 409


def test_update_and_reset_password_error_paths() -> None:
    deps = _deps()
    deps.accounts.update_error = "非法角色"
    client = _client(deps)
    assert client.patch(
        "/api/admin/users/u1", json={"role": "auditor"}
    ).status_code == 422

    deps = _deps()
    deps.accounts.reset_error = "用户不存在"
    client = _client(deps)
    assert client.post(
        "/api/admin/users/u1/reset-password",
        json={"password": "password-123"},
    ).status_code == 422


def test_reset_password_revokes_sessions() -> None:
    deps = _deps()
    client = _client(deps)
    response = client.post(
        "/api/admin/users/u1/reset-password",
        json={"password": "password-123"},
    )
    assert response.status_code == 200
    assert deps.sessions.revoked == ["u1"]


def test_quota_read_and_missing_control_store() -> None:
    deps = _deps()
    client = _client(deps)
    response = client.get("/api/admin/users/u1/quotas")
    assert response.status_code == 200
    assert response.json()["quota"]["daily_tokens"] == 10

    deps = _deps(control_store=None)
    client = _client(deps)
    assert client.get("/api/admin/users/u1/quotas").status_code == 503
    assert client.put(
        "/api/admin/users/u1/quotas", json={"daily_tokens": 1}
    ).status_code == 503
    assert (
        client.get("/api/admin/usage?from=2026-10-01&to=2026-10-02").status_code
        == 503
    )


def test_usage_rejects_invalid_ranges() -> None:
    client = _client(_deps())
    assert client.get(
        "/api/admin/usage?from=2026-10-10&to=2026-10-01"
    ).status_code == 422
    assert client.get(
        "/api/admin/usage?from=2024-01-01&to=2026-10-01"
    ).status_code == 422


def test_auth_events_disabled_returns_empty() -> None:
    client = _client(_deps(auth_events=None))
    assert client.get("/api/admin/auth-events").json() == {"events": []}


def test_deployment_providers_uses_empty_store() -> None:
    client = _client(_deps())
    response = client.get("/api/admin/providers")
    assert response.status_code == 200
    assert response.json() == {"providers": []}


def test_epoch_accepts_date_and_iso_forms() -> None:
    assert _epoch("") == ""
    assert _epoch("not-a-date") == ""
    start = int(datetime(2026, 10, 1, tzinfo=UTC).timestamp())
    end = int(datetime(2026, 10, 1, 23, 59, 59, tzinfo=UTC).timestamp())
    assert _epoch("2026-10-01") == str(start)
    assert _epoch("2026-10-01", end_of_day=True) == str(end)
    aware = int(datetime(2026, 10, 1, 12, tzinfo=UTC).timestamp())
    assert _epoch("2026-10-01T12:00:00Z") == str(aware)
    # 无时区的 ISO 按 UTC 解释。
    assert _epoch("2026-10-01T12:00:00") == str(aware)


class _KeyStore:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.deleted: list[str] = []

    def delete(self, provider: str) -> None:
        if self.error is not None:
            raise self.error
        self.deleted.append(provider)


def test_delete_provider_key_and_storage_error() -> None:
    store = _KeyStore()
    deps = _deps(
        provider_rows=lambda settings, store: [{"name": "deepseek"}],
        user_store=lambda user: store,
    )
    client = _client(deps)
    response = client.delete("/api/admin/users/u1/providers/deepseek")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert store.deleted == ["deepseek"]

    broken = _KeyStore(SecretStoreUnavailable("主密钥不可用"))
    client = _client(_deps(user_store=lambda user: broken))
    response = client.delete("/api/admin/users/u1/providers/deepseek")
    assert response.status_code == 503


def test_audit_disabled_and_unknown_user() -> None:
    client = _client(_deps(control_store=None))
    assert client.get("/api/admin/audit").status_code == 503

    client = _client(_deps())
    response = client.get("/api/admin/audit", params={"user": "nobody"})
    assert response.status_code == 200
    assert response.json() == {"records": [], "total": 0, "has_more": False}
