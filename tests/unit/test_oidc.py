"""RFC-0004：OIDC PKCE、discovery/JWKS 验签与 state 一次性。"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding_agent.config import Settings
from coding_agent.control.accounts import SessionStore, UserStore
from coding_agent.control.oidc import (
    OidcClient,
    OidcError,
    OidcSettings,
    OidcStateStore,
)
from coding_agent.control.store import ControlStore
from coding_agent.web.auth import AuthConfig
from coding_agent.web.routes.auth import AuthRouteDeps, register_auth_routes

ISSUER = "https://idp.example"
CLIENT_ID = "coding-agent"
KID = "key-1"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwt(claims: dict, key: rsa.RSAPrivateKey, *, kid: str = KID) -> str:
    header = {"alg": "RS256", "kid": kid, "typ": "JWT"}
    head = _b64(json.dumps(header).encode())
    payload = _b64(json.dumps(claims).encode())
    signing = f"{head}.{payload}".encode("ascii")
    signature = key.sign(signing, padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{payload}.{_b64(signature)}"


def _jwks(key: rsa.RSAPrivateKey) -> dict:
    numbers = key.public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": KID,
                "use": "sig",
                "alg": "RS256",
                "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }


def _client(key: rsa.RSAPrivateKey) -> OidcClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json=_jwks(key))
        raise AssertionError(f"unexpected URL: {request.url}")

    settings = OidcSettings(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret="",
        redirect_uri="https://app.example/api/auth/oidc/callback",
    )
    return OidcClient(settings, transport=httpx.MockTransport(handler))


def _claims(**overrides: object) -> dict:
    now = int(time.time())
    data = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "user-1",
        "email": "alice@example.com",
        "preferred_username": "alice",
        "nonce": "nonce-1",
        "iat": now,
        "exp": now + 300,
    }
    data.update(overrides)
    return data


def test_authorization_url_uses_pkce_and_nonce() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    url = _client(key).authorization_url(
        state="s1", nonce="n1", code_challenge="challenge"
    )
    assert url.startswith(f"{ISSUER}/authorize?")
    assert "response_type=code" in url
    assert "code_challenge=challenge" in url
    assert "code_challenge_method=S256" in url
    assert "state=s1" in url
    assert "nonce=n1" in url


def test_valid_id_token_is_verified() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _jwt(_claims(), key)
    claims = _client(key).verify_id_token(token, nonce="nonce-1")
    assert claims["sub"] == "user-1"


@pytest.mark.parametrize(
    "overrides",
    [
        {"nonce": "wrong"},
        {"exp": int(time.time()) - 3600},
        {"aud": "other-client"},
        {"iss": "https://evil.example"},
    ],
)
def test_bad_claims_are_rejected(overrides: dict) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _jwt(_claims(**overrides), key)
    with pytest.raises(OidcError):
        _client(key).verify_id_token(token, nonce="nonce-1")


def test_bad_signature_is_rejected() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _jwt(_claims(), other)
    with pytest.raises(OidcError):
        _client(key).verify_id_token(token, nonce="nonce-1")


def test_exchange_code_posts_pkce_fields() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": ISSUER,
                    "authorization_endpoint": f"{ISSUER}/authorize",
                    "token_endpoint": f"{ISSUER}/token",
                    "jwks_uri": f"{ISSUER}/jwks",
                },
            )
        if request.url.path == "/jwks":
            return httpx.Response(200, json=_jwks(key))
        if request.url.path == "/token":
            seen.update(
                dict(httpx.QueryParams(request.content.decode()))
            )
            return httpx.Response(
                200, json={"id_token": "signed", "access_token": "a"}
            )
        raise AssertionError(request.url)

    settings = OidcSettings(
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret="",
        redirect_uri="https://app/callback",
    )
    client = OidcClient(settings, transport=httpx.MockTransport(handler))
    token = client.exchange_code(
        code="code-1",
        code_verifier="verifier-1",
        redirect_uri="https://app/callback",
    )
    assert token["access_token"] == "a"
    assert seen["grant_type"] == "authorization_code"
    assert seen["code"] == "code-1"
    assert seen["code_verifier"] == "verifier-1"
    assert seen["client_id"] == CLIENT_ID


def test_discovery_rejects_non_https_endpoint() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/authorize",
                "token_endpoint": "http://idp.example/token",
                "jwks_uri": f"{ISSUER}/jwks",
            },
        )

    client = OidcClient(
        OidcSettings(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret="",
            redirect_uri="https://app/callback",
        ),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(OidcError):
        client.discover()


def test_state_is_one_time_and_expires(tmp_path: Path, monkeypatch) -> None:
    store = ControlStore(f"sqlite:///{tmp_path / 'control.db'}")
    states = OidcStateStore(store.engine)
    created = states.create(
        tenant="default",
        redirect_uri="https://app/callback",
        ttl_s=600,
    )
    consumed = states.consume(created["state"])
    assert consumed is not None
    assert consumed["nonce"] == created["nonce"]
    assert consumed["code_verifier"] == created["code_verifier"]
    assert states.consume(created["state"]) is None

    created = states.create(
        tenant="default",
        redirect_uri="https://app/callback",
        ttl_s=600,
    )
    import coding_agent.control.oidc as oidc_module

    original = time.time()
    monkeypatch.setattr(
        oidc_module.time, "time", lambda: original + 3600
    )
    assert states.consume(created["state"]) is None
    store.close()


def test_callback_creates_session_and_state_is_single_use(
    tmp_path: Path,
) -> None:
    store = ControlStore(f"sqlite:///{tmp_path / 'control.db'}")
    accounts = UserStore(store.engine, tenant="default")
    sessions = SessionStore(store.engine, ttl_s=3600)
    states = OidcStateStore(store.engine)
    settings = Settings(
        _env_file=None,
        web_accounts_enabled=True,
        web_account_tenant="default",
        oidc_issuer=ISSUER,
        oidc_client_id=CLIENT_ID,
        oidc_redirect_uri="https://app.example/callback",
        secret_master_key="",
    )

    class _FakeClient:
        settings = SimpleNamespace(
            redirect_uri="https://app.example/callback",
            issuer=ISSUER,
        )

        def exchange_code(self, **_kwargs):  # noqa: ANN003
            return {"id_token": "signed"}

        def verify_id_token(self, _token, *, nonce):  # noqa: ANN001
            assert nonce
            return {
                "sub": "subject-1",
                "preferred_username": "alice",
                "email": "alice@example.com",
            }

    created = states.create(
        tenant="default",
        redirect_uri="https://app.example/callback",
    )
    app = FastAPI()
    register_auth_routes(
        app,
        AuthRouteDeps(
            settings=settings,
            auth=AuthConfig(tokens={}, allow_anonymous_local=False),
            accounts=accounts,
            sessions=sessions,
            control_store=store,
            rate_limit=lambda _user: None,
            login_rate_limit=lambda _ip: None,
            oidc_client=_FakeClient(),
            oidc_states=states,
            oidc_enabled=True,
        ),
    )
    client = TestClient(app, follow_redirects=False)
    response = client.get(
        "/api/auth/oidc/callback",
        params={"code": "code-1", "state": created["state"]},
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "ca_session" in response.cookies
    row = accounts.get_by_username("alice")
    assert row is not None

    again = client.get(
        "/api/auth/oidc/callback",
        params={"code": "code-1", "state": created["state"]},
    )
    assert again.status_code == 303
    assert again.headers["location"] == "/?auth_error=oidc_state"
    store.close()
