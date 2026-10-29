"""OIDC 错误分支与密钥算法覆盖（与 test_oidc 的正常路径互补）。"""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa, utils

from coding_agent.control.oidc import (
    OidcClient,
    OidcError,
    OidcSettings,
    OidcStateStore,
    _b64url_decode,
    _b64url_encode,
    _jwt_parts,
    _key_from_jwk,
    _verify_signature,
)
from coding_agent.control.store import ControlStore

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
                "n": _b64(
                    numbers.n.to_bytes(
                        (numbers.n.bit_length() + 7) // 8, "big"
                    )
                ),
                "e": _b64(
                    numbers.e.to_bytes(
                        (numbers.e.bit_length() + 7) // 8, "big"
                    )
                ),
            }
        ]
    }


def _claims(**overrides: object) -> dict:
    now = int(time.time())
    data = {
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sub": "user-1",
        "nonce": "nonce-1",
        "iat": now,
        "exp": now + 300,
    }
    data.update(overrides)
    return data


def _settings(**overrides) -> OidcSettings:
    values = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": "",
        "redirect_uri": "https://app/callback",
    }
    values.update(overrides)
    return OidcSettings(**values)


def _discovery() -> dict:
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
    }


def test_oidc_settings_validation() -> None:
    ns = SimpleNamespace(
        oidc_issuer=ISSUER + "/",
        oidc_client_id=CLIENT_ID,
        oidc_client_secret="",
        oidc_redirect_uri="https://app/callback",
        oidc_scopes="openid email",
        oidc_allow_insecure_http=False,
        oidc_http_timeout_s=3,
    )
    parsed = OidcSettings.from_settings(ns)
    assert parsed.issuer == ISSUER
    assert parsed.scopes == "openid email"

    with pytest.raises(OidcError, match="issuer / client_id / redirect_uri"):
        _settings(issuer="", client_id="").validate()
    with pytest.raises(OidcError, match="绝对 URL"):
        _settings(issuer="not-a-url").validate()
    with pytest.raises(OidcError, match="HTTPS"):
        _settings(issuer="http://idp.example").validate()
    assert (
        _settings(
            issuer="http://idp.example", allow_insecure_http=True
        ).validate()
        is None
    )
    with pytest.raises(OidcError, match="query/fragment"):
        _settings(issuer=ISSUER + "?x=1").validate()


def test_jwt_part_errors() -> None:
    with pytest.raises(OidcError, match="JWT 编码非法"):
        _b64url_decode("a")
    with pytest.raises(OidcError, match="三段式"):
        _jwt_parts("only-one-part")
    with pytest.raises(OidcError, match="合法 JSON"):
        _jwt_parts(
            f"{_b64url_encode(b'not-json')}.{_b64url_encode(b'{}')}.AA"
        )
    with pytest.raises(OidcError, match="结构非法"):
        _jwt_parts(f"{_b64url_encode(b'[]')}.{_b64url_encode(b'{}')}.AA")


def test_ec_keys_and_signature_algorithms() -> None:
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.public_key().public_numbers()

    def encoded(value: int) -> str:
        return _b64url_encode(
            value.to_bytes((value.bit_length() + 7) // 8, "big")
        )

    key = _key_from_jwk(
        {
            "kty": "EC",
            "crv": "P-256",
            "x": encoded(numbers.x),
            "y": encoded(numbers.y),
        }
    )
    assert key.key_size == 256
    with pytest.raises(OidcError, match="不支持的 EC curve"):
        _key_from_jwk({"kty": "EC", "crv": "P-999", "x": "", "y": ""})

    data = b"payload"
    der = private.sign(data, ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(der)
    size = (private.public_key().key_size + 7) // 8
    raw = r.to_bytes(size, "big") + s.to_bytes(size, "big")
    _verify_signature(key, "ES256", data, raw)

    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(OidcError, match="密钥类型不匹配"):
        _verify_signature(rsa_key.public_key(), "ES256", data, raw)
    with pytest.raises(OidcError, match="alg=none"):
        _verify_signature(rsa_key.public_key(), "none", data, b"")
    with pytest.raises(OidcError, match="不支持的 JWT alg"):
        _verify_signature(rsa_key.public_key(), "RS999", data, b"")

    ps_signature = rsa_key.sign(
        data,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=hashes.SHA256().digest_size,
        ),
        hashes.SHA256(),
    )
    _verify_signature(rsa_key.public_key(), "PS256", data, ps_signature)


def test_discovery_failures_and_cache() -> None:
    client = OidcClient(
        _settings(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(500, text="boom")
        ),
    )
    with pytest.raises(OidcError, match="请求失败"):
        client.discover()

    client = OidcClient(
        _settings(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=[])
        ),
    )
    with pytest.raises(OidcError, match="不是 JSON 对象"):
        client.discover()

    calls = {"count": 0}

    def good(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=_discovery())

    client = OidcClient(_settings(), transport=httpx.MockTransport(good))
    assert client.discover()["issuer"] == ISSUER
    assert client.discover()["issuer"] == ISSUER
    assert calls["count"] == 1

    mismatched = dict(_discovery(), issuer="https://other.example")
    with pytest.raises(OidcError, match="issuer 与配置不一致"):
        OidcClient(
            _settings(),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=mismatched)
            ),
        ).discover()

    missing = dict(_discovery())
    missing.pop("token_endpoint")
    with pytest.raises(OidcError, match="缺少 token_endpoint"):
        OidcClient(
            _settings(),
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(200, json=missing)
            ),
        ).discover()


def test_exchange_code_failures_and_client_secret() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(200, json=_discovery())
        if request.url.path == "/token":
            return httpx.Response(200, json={"id_token": "signed"})
        raise AssertionError(request.url)

    client = OidcClient(
        _settings(client_secret="secret"),
        transport=httpx.MockTransport(handler),
    )
    assert (
        client.exchange_code(
            code="c", code_verifier="v", redirect_uri="https://app/callback"
        )["id_token"]
        == "signed"
    )

    def token_error(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(200, json=_discovery())
        return httpx.Response(500, text="nope")

    client = OidcClient(
        _settings(), transport=httpx.MockTransport(token_error)
    )
    with pytest.raises(OidcError, match="token 交换失败"):
        client.exchange_code(
            code="c", code_verifier="v", redirect_uri="https://app/callback"
        )

    def non_object(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(200, json=_discovery())
        return httpx.Response(200, json=[])

    client = OidcClient(
        _settings(), transport=httpx.MockTransport(non_object)
    )
    with pytest.raises(OidcError, match="不是 JSON 对象"):
        client.exchange_code(
            code="c", code_verifier="v", redirect_uri="https://app/callback"
        )

    def missing_token(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(200, json=_discovery())
        return httpx.Response(200, json={"access_token": "a"})

    client = OidcClient(
        _settings(), transport=httpx.MockTransport(missing_token)
    )
    with pytest.raises(OidcError, match="缺少 id_token"):
        client.exchange_code(
            code="c", code_verifier="v", redirect_uri="https://app/callback"
        )


def test_jwks_edges_and_claim_edges() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    calls = {"jwks": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(200, json=_discovery())
        calls["jwks"] += 1
        return httpx.Response(200, json=_jwks(key))

    client = OidcClient(_settings(), transport=httpx.MockTransport(handler))
    token = _jwt(_claims(), key)
    assert client.verify_id_token(token, nonce="nonce-1")["sub"] == "user-1"
    assert client.verify_id_token(token, nonce="nonce-1")["sub"] == "user-1"
    assert calls["jwks"] == 1

    other = _jwt(_claims(), key, kid="other")
    with pytest.raises(OidcError, match="kid 不在 JWKS"):
        client.verify_id_token(other, nonce="nonce-1")
    # 首次走缓存，未命中后强制刷新一次。
    assert calls["jwks"] == 2

    def empty_jwks(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(200, json=_discovery())
        return httpx.Response(200, json={"keys": []})

    client = OidcClient(
        _settings(), transport=httpx.MockTransport(empty_jwks)
    )
    with pytest.raises(OidcError, match="JWKS 没有 keys"):
        client.verify_id_token(token, nonce="nonce-1")

    client = OidcClient(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(OidcError, match="缺少合法 exp/iat"):
        client.verify_id_token(_jwt(_claims(exp="bad"), key), nonce="nonce-1")
    with pytest.raises(OidcError, match="iat 在未来"):
        client.verify_id_token(
            _jwt(_claims(iat=int(time.time()) + 600), key),
            nonce="nonce-1",
        )


def test_state_empty_and_purge_expired(tmp_path: Path, monkeypatch) -> None:
    import coding_agent.control.oidc as oidc_module

    store = ControlStore(f"sqlite:///{tmp_path / 'control.db'}")
    states = OidcStateStore(store.engine)
    assert states.consume("") is None

    created = states.create(
        tenant="default", redirect_uri="https://app/callback", ttl_s=60
    )
    assert created["state"]
    real_time = time.time()
    monkeypatch.setattr(
        oidc_module.time, "time", lambda: real_time + 3600
    )
    assert states.purge_expired() == 1
    assert states.purge_expired() == 0
    store.close()
