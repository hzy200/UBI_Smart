"""OIDC Authorization Code + PKCE（RFC-0004）。

只依赖标准库、`httpx` 与 `cryptography`：

- discovery / JWKS 缓存；issuer 必须与 discovery 返回值一致；
- `state`/`nonce`/`code_verifier` 一次性落控制面库，消费即删；
- ID token 验签支持 RS256/384/512、PS256/384/512、ES256/384/512；
- 校验 `iss`、`aud`、`exp`、`iat`、`nonce`；拒绝 `alg=none`。

不记录 token、ID token 或 claims 原文到日志/审计。
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from sqlalchemy import delete
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from ubismart.control.schema import ControlOidcState


class OidcError(RuntimeError):
    """OIDC 配置、网络或 token 校验失败。"""


@dataclass(frozen=True)
class OidcSettings:
    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: str = "openid profile email"
    allow_insecure_http: bool = False
    timeout_s: float = 10.0

    @classmethod
    def from_settings(cls, settings: Any) -> OidcSettings:
        return cls(
            issuer=str(settings.oidc_issuer or "").rstrip("/"),
            client_id=str(settings.oidc_client_id or ""),
            client_secret=str(settings.oidc_client_secret or ""),
            redirect_uri=str(settings.oidc_redirect_uri or ""),
            scopes=str(settings.oidc_scopes or "openid profile email"),
            allow_insecure_http=bool(settings.oidc_allow_insecure_http),
            timeout_s=float(settings.oidc_http_timeout_s or 10.0),
        )

    def validate(self) -> None:
        if not self.issuer or not self.client_id or not self.redirect_uri:
            raise OidcError(
                "OIDC 需要 issuer / client_id / redirect_uri"
            )
        parsed = urlsplit(self.issuer)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise OidcError("OIDC issuer 必须是绝对 URL")
        if parsed.scheme == "http" and not self.allow_insecure_http:
            raise OidcError(
                "OIDC issuer 必须是 HTTPS；本地开发可显式设置 "
                "AGENT_OIDC_ALLOW_INSECURE_HTTP=true"
            )
        if parsed.query or parsed.fragment:
            raise OidcError("OIDC issuer 不能带 query/fragment")


def _b64url_decode(value: str) -> bytes:
    pad = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + pad)
    except (ValueError, TypeError) as exc:
        raise OidcError("JWT 编码非法") from exc


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _jwt_parts(token: str) -> tuple[dict[str, Any], dict[str, Any], bytes, bytes]:
    parts = str(token or "").split(".")
    if len(parts) != 3:
        raise OidcError("ID token 不是三段式 JWT")
    header_raw, payload_raw, signature_raw = parts
    try:
        header = json.loads(_b64url_decode(header_raw))
        payload = json.loads(_b64url_decode(payload_raw))
    except (ValueError, UnicodeDecodeError) as exc:
        raise OidcError("ID token header/payload 不是合法 JSON") from exc
    if not isinstance(header, dict) or not isinstance(payload, dict):
        raise OidcError("ID token header/payload 结构非法")
    return (
        header,
        payload,
        f"{header_raw}.{payload_raw}".encode("ascii"),
        _b64url_decode(signature_raw),
    )


def _key_from_jwk(jwk: dict[str, Any]) -> Any:
    kty = str(jwk.get("kty") or "")
    if kty == "RSA":
        n = int.from_bytes(_b64url_decode(str(jwk["n"])), "big")
        e = int.from_bytes(_b64url_decode(str(jwk["e"])), "big")
        return rsa.RSAPublicNumbers(e, n).public_key()
    if kty == "EC":
        curves = {
            "P-256": ec.SECP256R1(),
            "P-384": ec.SECP384R1(),
            "P-521": ec.SECP521R1(),
        }
        curve = curves.get(str(jwk.get("crv") or ""))
        if curve is None:
            raise OidcError(f"不支持的 EC curve：{jwk.get('crv')}")
        x = int.from_bytes(_b64url_decode(str(jwk["x"])), "big")
        y = int.from_bytes(_b64url_decode(str(jwk["y"])), "big")
        return ec.EllipticCurvePublicNumbers(x, y, curve).public_key()
    raise OidcError(f"不支持的 JWK kty：{kty}")


_HASHES = {
    "256": hashes.SHA256,
    "384": hashes.SHA384,
    "512": hashes.SHA512,
}


def _verify_signature(
    key: Any, alg: str, signing_input: bytes, signature: bytes
) -> None:
    if alg == "none" or not alg:
        raise OidcError("拒绝 alg=none")
    suffix = alg[2:]
    hash_cls = _HASHES.get(suffix)
    if hash_cls is None:
        raise OidcError(f"不支持的 JWT alg：{alg}")
    hash_alg = hash_cls()
    if alg.startswith("RS"):
        key.verify(
            signature, signing_input, padding.PKCS1v15(), hash_alg
        )
        return
    if alg.startswith("PS"):
        key.verify(
            signature,
            signing_input,
            padding.PSS(
                mgf=padding.MGF1(hash_alg),
                salt_length=hash_alg.digest_size,
            ),
            hash_alg,
        )
        return
    if alg.startswith("ES"):
        if not isinstance(key, ec.EllipticCurvePublicKey):
            raise OidcError("EC alg 与密钥类型不匹配")
        size = (key.key_size + 7) // 8
        if len(signature) == size * 2:
            from cryptography.hazmat.primitives.asymmetric.utils import (
                encode_dss_signature,
            )

            r = int.from_bytes(signature[:size], "big")
            s = int.from_bytes(signature[size:], "big")
            signature = encode_dss_signature(r, s)
        key.verify(signature, signing_input, ec.ECDSA(hash_alg))
        return
    raise OidcError(f"不支持的 JWT alg：{alg}")


class OidcClient:
    """发现、换码与 ID token 校验；可注入 httpx transport 便于测试。"""

    def __init__(
        self,
        settings: OidcSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.settings.validate()
        self._transport = transport
        self._discovery: dict[str, Any] | None = None
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.settings.timeout_s,
            trust_env=False,
            transport=self._transport,
            follow_redirects=False,
        )

    def _get_json(self, url: str) -> dict[str, Any]:
        try:
            with self._client() as client:
                response = client.get(url)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError(f"OIDC 请求失败：{url}") from exc
        if not isinstance(payload, dict):
            raise OidcError(f"OIDC 响应不是 JSON 对象：{url}")
        return payload

    def discover(self) -> dict[str, Any]:
        if self._discovery is not None:
            return self._discovery
        url = f"{self.settings.issuer}/.well-known/openid-configuration"
        data = self._get_json(url)
        if str(data.get("issuer") or "").rstrip("/") != self.settings.issuer:
            raise OidcError("OIDC discovery 的 issuer 与配置不一致")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            endpoint = str(data.get(key) or "")
            parsed = urlsplit(endpoint)
            if parsed.scheme not in {"https", "http"} or not parsed.netloc:
                raise OidcError(f"OIDC discovery 缺少 {key}")
            if parsed.scheme != "https" and not self.settings.allow_insecure_http:
                raise OidcError(f"OIDC {key} 必须是 HTTPS")
        self._discovery = data
        return data

    def authorization_url(
        self, *, state: str, nonce: str, code_challenge: str
    ) -> str:
        endpoint = str(self.discover()["authorization_endpoint"])
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.redirect_uri,
                "scope": self.settings.scopes,
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{endpoint}?{query}"

    def exchange_code(
        self, *, code: str, code_verifier: str, redirect_uri: str
    ) -> dict[str, Any]:
        endpoint = str(self.discover()["token_endpoint"])
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": self.settings.client_id,
            "code_verifier": code_verifier,
        }
        auth = None
        if self.settings.client_secret:
            auth = (
                self.settings.client_id,
                self.settings.client_secret,
            )
        try:
            with self._client() as client:
                response = client.post(endpoint, data=data, auth=auth)
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise OidcError("OIDC token 交换失败") from exc
        if not isinstance(payload, dict):
            raise OidcError("OIDC token 响应不是 JSON 对象")
        if not payload.get("id_token"):
            raise OidcError("OIDC token 响应缺少 id_token")
        return payload

    def _fetch_jwks(self) -> dict[str, Any]:
        if self._jwks is not None and time.time() - self._jwks_at < 3600:
            return self._jwks
        uri = str(self.discover()["jwks_uri"])
        data = self._get_json(uri)
        keys = data.get("keys")
        if not isinstance(keys, list) or not keys:
            raise OidcError("OIDC JWKS 没有 keys")
        self._jwks = data
        self._jwks_at = time.time()
        return data

    def verify_id_token(self, token: str, *, nonce: str) -> dict[str, Any]:
        header, payload, signing_input, signature = _jwt_parts(token)
        alg = str(header.get("alg") or "")
        kid = str(header.get("kid") or "")
        keys = [
            item
            for item in self._fetch_jwks().get("keys", [])
            if isinstance(item, dict)
            and (not kid or str(item.get("kid") or "") == kid)
            and str(item.get("use") or "sig") == "sig"
        ]
        if not keys:
            # kid 轮换：强制刷新一次。
            self._jwks = None
            keys = [
                item
                for item in self._fetch_jwks().get("keys", [])
                if isinstance(item, dict)
                and (not kid or str(item.get("kid") or "") == kid)
                and str(item.get("use") or "sig") == "sig"
            ]
        if not keys:
            raise OidcError("ID token 的 kid 不在 JWKS 中")
        verified = False
        for jwk in keys:
            try:
                _verify_signature(
                    _key_from_jwk(jwk), alg, signing_input, signature
                )
                verified = True
                break
            except (InvalidSignature, ValueError, KeyError, OidcError):
                continue
        if not verified:
            raise OidcError("ID token 签名校验失败")
        self._validate_claims(payload, nonce=nonce)
        return payload

    def _validate_claims(
        self, payload: dict[str, Any], *, nonce: str
    ) -> None:
        now = int(time.time())
        issuer = str(payload.get("iss") or "").rstrip("/")
        if issuer != self.settings.issuer:
            raise OidcError("ID token issuer 不匹配")
        aud = payload.get("aud")
        audiences = [aud] if isinstance(aud, str) else list(aud or [])
        if self.settings.client_id not in audiences:
            raise OidcError("ID token aud 不包含 client_id")
        try:
            exp = int(payload["exp"])
            iat = int(payload.get("iat") or 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise OidcError("ID token 缺少合法 exp/iat") from exc
        if exp <= now - 60:
            raise OidcError("ID token 已过期")
        if iat and iat > now + 120:
            raise OidcError("ID token iat 在未来")
        if nonce and str(payload.get("nonce") or "") != nonce:
            raise OidcError("ID token nonce 不匹配")


class OidcStateStore:
    """一次性 state/nonce/verifier；消费后删除，过期由登录路径顺带清理。"""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def create(
        self,
        *,
        tenant: str,
        redirect_uri: str,
        return_to: str = "",
        ttl_s: int = 600,
    ) -> dict[str, str]:
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url_encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        )
        now = int(time.time())
        with Session(self.engine) as session, session.begin():
            session.add(
                ControlOidcState(
                    state=state,
                    tenant=str(tenant),
                    nonce=nonce,
                    code_verifier=verifier,
                    redirect_uri=str(redirect_uri),
                    return_to=str(return_to or "")[:512],
                    created_at=str(now),
                    expires_at=str(now + max(60, int(ttl_s))),
                )
            )
        return {
            "state": state,
            "nonce": nonce,
            "code_verifier": verifier,
            "code_challenge": challenge,
        }

    def consume(self, state: str) -> dict[str, str] | None:
        text = str(state or "")
        if not text:
            return None
        now = int(time.time())
        with Session(self.engine) as session, session.begin():
            row = session.get(ControlOidcState, text)
            if row is None:
                return None
            deleted = session.execute(
                delete(ControlOidcState).where(
                    ControlOidcState.state == text,
                    ControlOidcState.expires_at >= str(now),
                )
            ).rowcount
            if not deleted:
                return None
            return {
                "tenant": row.tenant,
                "nonce": row.nonce,
                "code_verifier": row.code_verifier,
                "redirect_uri": row.redirect_uri,
                "return_to": row.return_to,
            }

    def purge_expired(self) -> int:
        now = int(time.time())
        with Session(self.engine) as session, session.begin():
            return int(
                session.execute(
                    delete(ControlOidcState).where(
                        ControlOidcState.expires_at < str(now)
                    )
                ).rowcount
                or 0
            )
