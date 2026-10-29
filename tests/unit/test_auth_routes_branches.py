"""认证路由的错误/降级分支与角色映射。"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from coding_agent.config import Settings
from coding_agent.control.mfa import MfaError
from coding_agent.control.oidc import OidcError
from coding_agent.web.auth import AuthConfig, AuthUser
from coding_agent.web.dependencies import current_user
from coding_agent.web.routes.auth import (
    AuthRouteDeps,
    _oidc_role,
    register_auth_routes,
)

ROW = {
    "id": "u1",
    "tenant": "default",
    "username": "alice",
    "email": "alice@example.com",
    "role": "member",
    "status": "active",
}


class _Accounts:
    def __init__(self) -> None:
        self.row: dict[str, Any] | None = dict(ROW)
        self.password_ok = True

    def authenticate(self, username: str, password: str):
        return self.row if self.password_ok and password == "good" else None

    def get(self, user_id: str):
        return self.row

    def get_by_username(self, username: str):
        return self.row

    def get_by_email(self, email: str):
        return self.row

    def find_or_create_oidc(self, **kwargs):
        return self.row


class _Sessions:
    def __init__(self) -> None:
        self.purge_fails = False
        self.created: list[str] = []
        self.kept: list[str] = []
        self.rows: list[dict[str, Any]] = []

    def purge_expired(self) -> None:
        if self.purge_fails:
            raise RuntimeError("purge failed")

    def create(self, user_id: str) -> dict[str, str]:
        self.created.append(user_id)
        return {"token": "tok", "csrf": "csrf"}

    def revoke(self, token: str) -> None:
        pass

    def revoke_user(self, user_id: str) -> None:
        self.kept.append(f"user:{user_id}")

    def list_for_user(self, user_id: str, *, current_token: str = ""):
        self.kept.append(current_token)
        return list(self.rows)

    def revoke_others(self, user_id: str, *, keep_token: str = "") -> int:
        self.kept.append(keep_token)
        return 2


class _Events:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.fail_count = False
        self.failures = 0
        self.rows: list[tuple] = []

    def record(self, *args, **kwargs) -> None:
        if self.fail:
            raise RuntimeError("audit down")
        self.rows.append(args)

    def recent_failures(self, username: str, *, window_s: int = 900) -> int:
        if self.fail_count:
            raise RuntimeError("event store down")
        return self.failures


class _Control:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail

    def get_quota(self, tenant: str, user: str) -> dict:
        if self.fail:
            raise RuntimeError("db down")
        return {"daily_tokens": 10, "daily_cost_cny": 1.0, "enabled": True}

    def get_usage_day(self, tenant: str, user: str, day: str) -> dict:
        if self.fail:
            raise RuntimeError("db down")
        return {"tokens": 5, "cost_usd": 0.1, "billed_usd": 0.1}

    def usage_report(self, *args, **kwargs) -> dict:
        return {"rows": [], "totals": {}}


class _OidcSettings:
    redirect_uri = "https://app/callback"
    issuer = "https://idp.example"


class _OidcClient:
    def __init__(self) -> None:
        self.settings = _OidcSettings()
        self.start_error = False
        self.exchange_error = False
        self.claims: dict[str, Any] = {
            "sub": "subject-1",
            "preferred_username": "alice",
            "email": "alice@example.com",
        }

    def authorization_url(self, **_kwargs) -> str:
        if self.start_error:
            raise OidcError("discovery down")
        return "https://idp.example/authorize?state=s"

    def exchange_code(self, **_kwargs) -> dict:
        if self.exchange_error:
            raise OidcError("bad code")
        return {"id_token": "signed"}

    def verify_id_token(self, _token: str, *, nonce: str) -> dict:
        return self.claims


class _States:
    def __init__(self) -> None:
        self.consumed: dict[str, Any] | None = {
            "code_verifier": "verifier",
            "redirect_uri": "https://app/callback",
            "nonce": "nonce",
            "return_to": "/",
        }

    def purge_expired(self) -> int:
        return 0

    def create(self, **kwargs) -> dict[str, str]:
        return {"state": "s", "nonce": "n", "code_challenge": "c"}

    def consume(self, state: str):
        return self.consumed


class _Mfa:
    def __init__(self) -> None:
        self.status_data = {"enabled": False, "recovery_codes": 0}
        self.ok = True
        self.confirm_error = ""
        self.disabled = False

    def status(self, user_id: str) -> dict:
        return self.status_data

    def enroll(self, **kwargs) -> str:
        return "SECRET"

    def confirm(self, **kwargs) -> list[str]:
        if self.confirm_error:
            raise MfaError(self.confirm_error)
        return ["code-1"]

    def verify(self, **kwargs) -> bool:
        return self.ok

    def verify_recovery(self, **kwargs) -> bool:
        return self.ok

    def disable(self, user_id: str) -> None:
        self.disabled = True


class _Challenges:
    def __init__(self) -> None:
        self.row: dict[str, Any] | None = None
        self.attempts = 0
        self.deleted: list[str] = []

    def create(self, user_id: str, *, ttl_s: int) -> str:
        return "mfa-token"

    def get(self, token: str):
        return self.row

    def fail(self, token: str) -> int:
        self.attempts += 1
        return self.attempts

    def delete(self, token: str) -> None:
        self.deleted.append(token)


def _deps(**overrides) -> AuthRouteDeps:
    values: dict[str, Any] = {
        "settings": Settings(_env_file=None, web_accounts_enabled=True),
        "auth": AuthConfig(tokens={}, allow_anonymous_local=False),
        "accounts": _Accounts(),
        "sessions": _Sessions(),
        "control_store": _Control(),
        "rate_limit": lambda _user: None,
        "login_rate_limit": lambda _ip: None,
        "auth_events": _Events(),
    }
    values.update(overrides)
    return AuthRouteDeps(**values)


def _client(deps: AuthRouteDeps, *, user_id: str = "u1") -> TestClient:
    app = FastAPI()
    register_auth_routes(app, deps)

    def _auth(request: Request):
        request.state.session_token = "current-token"
        return AuthUser(
            name="alice", user_id=user_id, tenant="default", role="member"
        )

    app.dependency_overrides[current_user] = _auth
    return TestClient(app, follow_redirects=False)


@pytest.mark.parametrize(
    ("settings", "claims", "expected"),
    [
        (
            Settings(
                _env_file=None,
                oidc_role_map='{"admins": "admin"}',
                oidc_role_claim="groups",
                oidc_default_role="nope",
            ),
            {},
            "member",
        ),
        (
            Settings(
                _env_file=None,
                oidc_role_map="{bad-json",
                oidc_role_claim="groups",
            ),
            {"groups": ["admins"]},
            "member",
        ),
        (
            Settings(
                _env_file=None,
                oidc_role_map="[1]",
                oidc_role_claim="groups",
            ),
            {"groups": ["admins"]},
            "member",
        ),
        (
            Settings(
                _env_file=None,
                oidc_role_map='{"admins": "admin"}',
                oidc_role_claim="groups",
            ),
            {},
            "member",
        ),
        (
            Settings(
                _env_file=None,
                oidc_role_map='{"admins": "admin"}',
                oidc_role_claim="groups",
            ),
            {"groups": "admins"},
            "admin",
        ),
        (
            Settings(
                _env_file=None,
                oidc_role_map='{"ops": "approver"}',
                oidc_role_claim="groups",
            ),
            {"groups": ["other"]},
            "member",
        ),
        (
            Settings(
                _env_file=None,
                oidc_role_map='{"aud": "auditor"}',
                oidc_role_claim="groups",
            ),
            {"groups": 3},
            "member",
        ),
    ],
)
def test_oidc_role_mapping(settings, claims, expected) -> None:
    assert _oidc_role(claims, settings) == expected


def test_me_quota_degraded_paths() -> None:
    client = _client(_deps(control_store=None))
    body = client.get("/api/me").json()
    assert body["quotas"]["daily_tokens"] == 0

    client = _client(_deps(control_store=_Control(fail=True)))
    body = client.get("/api/me").json()
    assert body["usage_today"]["tokens"] == 0

    client = _client(_deps(control_store=None))
    assert client.get("/api/me/usage?from=2026-10-01&to=2026-10-02").json() == {
        "rows": [],
        "totals": {},
    }


def test_login_errors_and_degraded_paths() -> None:
    client = _client(_deps(accounts=None))
    assert client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "good"},
    ).status_code == 503

    deps = _deps(mfa_enabled=True)
    client = _client(deps)
    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "good"},
    )
    assert response.status_code == 503

    deps = _deps()
    deps.sessions.purge_fails = True
    deps.auth_events.fail = True
    client = _client(deps)
    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "good"},
    )
    assert response.status_code == 200
    assert response.json()["user"]["username"] == "alice"


def test_login_locks_account_after_repeated_failures() -> None:
    events = _Events()
    events.failures = 10
    client = _client(_deps(auth_events=events))
    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "good"},
    )
    assert response.status_code == 429
    assert response.headers["retry-after"] == "900"
    assert response.json()["detail"]["code"] == "auth.login_locked"


def test_login_survives_failure_counter_error() -> None:
    events = _Events()
    events.fail_count = True
    client = _client(_deps(auth_events=events))
    response = client.post(
        "/api/auth/login",
        json={"username": "alice", "password": "good"},
    )
    assert response.status_code == 200


def test_session_endpoints_without_user_id() -> None:
    client = _client(_deps(), user_id="")
    assert client.get("/api/auth/sessions").json() == {"sessions": []}
    assert client.post(
        "/api/auth/sessions/revoke-others"
    ).json() == {"ok": True, "revoked": 0}
    assert client.get("/api/me/usage?from=2026-10-01&to=2026-10-02").json() == {
        "rows": [],
        "totals": {},
    }


def test_session_listing_and_revoke_others() -> None:
    deps = _deps()
    deps.sessions.rows = [
        {"created_at": "a", "expires_at": "b", "current": True}
    ]
    client = _client(deps)
    listed = client.get("/api/auth/sessions")
    assert listed.status_code == 200
    assert listed.json()["sessions"][0]["current"] is True
    assert deps.sessions.kept[-1] == "current-token"

    revoked = client.post("/api/auth/sessions/revoke-others")
    assert revoked.status_code == 200
    assert revoked.json() == {"ok": True, "revoked": 2}
    assert deps.sessions.kept[-1] == "current-token"

    client = _client(_deps(sessions=None))
    assert client.get("/api/auth/sessions").status_code == 503
    assert client.post(
        "/api/auth/sessions/revoke-others"
    ).status_code == 503


def test_logout_ignores_audit_failure() -> None:
    deps = _deps(auth_events=_Events(fail=True))
    client = _client(deps)
    assert client.post("/api/auth/logout").status_code == 200


def test_oidc_status_and_start_branches() -> None:
    client = _client(_deps())
    assert client.get("/api/auth/oidc/status").json() == {"enabled": False}

    assert client.get("/api/auth/oidc/start").status_code == 503

    def limited(_ip: str) -> None:
        raise HTTPException(status_code=429, detail="slow down")

    deps = _deps(
        oidc_enabled=True,
        oidc_client=_OidcClient(),
        oidc_states=_States(),
        login_rate_limit=limited,
    )
    client = _client(deps)
    assert client.get("/api/auth/oidc/start").status_code == 429

    client = _client(
        _deps(
            oidc_enabled=True,
            oidc_client=_OidcClient(),
            oidc_states=_States(),
        )
    )
    assert client.get("/api/auth/oidc/start").json() == {
        "authorization_url": "https://idp.example/authorize?state=s"
    }

    broken = _OidcClient()
    broken.start_error = True
    client = _client(
        _deps(
            oidc_enabled=True,
            oidc_client=broken,
            oidc_states=_States(),
        )
    )
    assert client.get("/api/auth/oidc/start").status_code == 503


def test_oidc_callback_errors() -> None:
    client = _client(_deps())
    denied = client.get(
        "/api/auth/oidc/callback", params={"error": "access_denied"}
    )
    assert denied.headers["location"] == "/?auth_error=oidc_denied"
    missing = client.get("/api/auth/oidc/callback")
    assert missing.headers["location"] == "/?auth_error=oidc_state"
    unavailable = client.get(
        "/api/auth/oidc/callback", params={"code": "c", "state": "s"}
    )
    assert unavailable.headers["location"] == "/?auth_error=oidc_unavailable"

    client = _client(
        _deps(
            oidc_enabled=True,
            oidc_client=_OidcClient(),
            oidc_states=_States(),
        )
    )
    ok = client.get(
        "/api/auth/oidc/callback", params={"code": "c", "state": "s"}
    )
    assert ok.status_code == 303
    assert ok.headers["location"] == "/"

    broken = _OidcClient()
    broken.exchange_error = True
    client = _client(
        _deps(
            oidc_enabled=True,
            oidc_client=broken,
            oidc_states=_States(),
        )
    )
    failed = client.get(
        "/api/auth/oidc/callback", params={"code": "c", "state": "s"}
    )
    assert failed.headers["location"] == "/?auth_error=oidc_failed"

    no_subject = _OidcClient()
    no_subject.claims = {"preferred_username": "alice"}
    client = _client(
        _deps(
            oidc_enabled=True,
            oidc_client=no_subject,
            oidc_states=_States(),
        )
    )
    assert client.get(
        "/api/auth/oidc/callback", params={"code": "c", "state": "s"}
    ).headers["location"] == "/?auth_error=oidc_failed"

    no_user = _deps(
        oidc_enabled=True,
        oidc_client=_OidcClient(),
        oidc_states=_States(),
    )
    no_user.accounts.row = None
    client = _client(no_user)
    assert client.get(
        "/api/auth/oidc/callback", params={"code": "c", "state": "s"}
    ).headers["location"] == "/?auth_error=oidc_no_user"


def test_mfa_verify_branches() -> None:
    client = _client(_deps())
    assert client.post(
        "/api/auth/mfa/verify",
        json={"mfa_token": "t", "code": "123456"},
    ).status_code == 503

    challenges = _Challenges()
    deps = _deps(
        mfa_enabled=True,
        mfa_store=_Mfa(),
        mfa_challenges=challenges,
    )
    client = _client(deps)
    assert client.post(
        "/api/auth/mfa/verify",
        json={"mfa_token": "t", "code": "123456"},
    ).status_code == 401

    challenges.row = {"user_id": "u1", "attempts": 5}
    assert client.post(
        "/api/auth/mfa/verify",
        json={"mfa_token": "t", "code": "123456"},
    ).status_code == 429
    assert challenges.deleted == ["t"]

    challenges.row = {"user_id": "u1", "attempts": 0}
    deps.accounts.row = None
    assert client.post(
        "/api/auth/mfa/verify",
        json={"mfa_token": "t", "code": "123456"},
    ).status_code == 401

    deps.accounts.row = dict(ROW)
    deps.mfa_store.ok = False
    assert client.post(
        "/api/auth/mfa/verify",
        json={"mfa_token": "t", "code": "recovery-code"},
    ).status_code == 401

    challenges.attempts = 4
    assert client.post(
        "/api/auth/mfa/verify",
        json={"mfa_token": "t", "code": "recovery-code"},
    ).status_code == 429

    challenges.attempts = 0
    deps.mfa_store.ok = True
    response = client.post(
        "/api/auth/mfa/verify",
        json={"mfa_token": "t", "code": "123456"},
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_mfa_status_enabled_and_oidc_return_to_fallback() -> None:
    mfa = _Mfa()
    mfa.status_data = {"enabled": True, "recovery_codes": 3}
    client = _client(_deps(mfa_store=mfa))
    assert client.get("/api/auth/mfa/status").json() == {
        "enabled": True,
        "recovery_codes": 3,
    }

    states = _States()
    states.consumed["return_to"] = "//evil.example"
    client = _client(
        _deps(
            oidc_enabled=True,
            oidc_client=_OidcClient(),
            oidc_states=states,
        )
    )
    response = client.get(
        "/api/auth/oidc/callback", params={"code": "c", "state": "s"}
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_mfa_status_enroll_confirm_disable_branches() -> None:
    client = _client(_deps())
    assert client.get("/api/auth/mfa/status").json() == {
        "enabled": False,
        "recovery_codes": 0,
    }
    assert client.post(
        "/api/auth/mfa/enroll", json={"password": "good"}
    ).status_code == 503
    assert client.post(
        "/api/auth/mfa/confirm", json={"code": "123456"}
    ).status_code == 503
    assert client.post(
        "/api/auth/mfa/disable",
        json={"password": "good", "code": "123456"},
    ).status_code == 503

    mfa = _Mfa()
    deps = _deps(mfa_enabled=True, mfa_store=mfa, mfa_challenges=_Challenges())
    client = _client(deps)
    assert client.post(
        "/api/auth/mfa/enroll", json={"password": "bad"}
    ).status_code == 403
    enrollment = client.post(
        "/api/auth/mfa/enroll", json={"password": "good"}
    )
    assert enrollment.status_code == 200
    assert enrollment.json()["secret"] == "SECRET"

    mfa.confirm_error = "验证码不正确"
    assert client.post(
        "/api/auth/mfa/confirm", json={"code": "000000"}
    ).status_code == 400

    assert client.post(
        "/api/auth/mfa/disable",
        json={"password": "bad", "code": "123456"},
    ).status_code == 403
    mfa.ok = False
    assert client.post(
        "/api/auth/mfa/disable",
        json={"password": "good", "code": "123456"},
    ).status_code == 401
    mfa.ok = True
    assert client.post(
        "/api/auth/mfa/disable",
        json={"password": "good", "code": "123456"},
    ).status_code == 200
    assert mfa.disabled is True


class _Resets:
    def __init__(self) -> None:
        self.created: list[str] = []
        self.validate = True

    def create(self, user_id: str) -> str:
        self.created.append(user_id)
        return "reset-token"

    def consume(self, token: str, new_password: str):
        if self.validate and token == "reset-token":
            return "u1"
        return None


def test_password_reset_request_and_confirm(monkeypatch) -> None:
    import coding_agent.web.routes.auth as auth_routes

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        auth_routes,
        "send_password_reset",
        lambda _settings, email, value: sent.append((email, value)) or True,
    )
    resets = _Resets()
    deps = _deps(
        password_resets=resets,
        settings=Settings(
            _env_file=None,
            web_accounts_enabled=True,
            web_public_url="https://app.example",
        ),
    )
    client = _client(deps)
    response = client.post(
        "/api/auth/password-reset/request",
        json={"identifier": "alice@example.com"},
    )
    assert response.status_code == 200
    assert resets.created == ["u1"]
    assert sent and sent[0][0] == "alice@example.com"
    assert "reset_token=" in sent[0][1]

    # 未知账号也返回同样的 200，不创建 token。
    deps.accounts.row = None
    assert client.post(
        "/api/auth/password-reset/request",
        json={"identifier": "ghost"},
    ).json() == {"ok": True}
    assert resets.created == ["u1"]

    confirmed = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": "reset-token", "new_password": "new-password-123"},
    )
    assert confirmed.status_code == 200
    assert "user:u1" in deps.sessions.kept

    bad = client.post(
        "/api/auth/password-reset/confirm",
        json={"token": "bad", "new_password": "new-password-123"},
    )
    assert bad.status_code == 400


def test_password_reset_disabled() -> None:
    client = _client(_deps(password_resets=None))
    assert client.post(
        "/api/auth/password-reset/request",
        json={"identifier": "alice"},
    ).json() == {"ok": True}
    assert client.post(
        "/api/auth/password-reset/confirm",
        json={"token": "x", "new_password": "new-password-123"},
    ).status_code == 503
