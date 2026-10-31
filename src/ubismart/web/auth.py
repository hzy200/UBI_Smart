"""Web 认证：企业账号会话 + 静态 Bearer 兼容。

企业模式使用 `AGENT_WEB_ACCOUNTS_ENABLED` 打开数据库账号：用户名/密码登录，
服务端只存 session token 的 sha256，Cookie 为 HttpOnly；写请求走双提交
CSRF。`AGENT_WEB_AUTH_TOKENS` 继续作为内网/桌面过渡方案，优先级最高。

未配置任何认证时只允许 loopback 匿名访问（单用户本地模式）。远程匿名必须
显式打开 `web_allow_anonymous_local` 之外的开关 —— 当前实现直接拒绝，避免
误把公网端口暴露成无鉴权 RCE。
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from ubismart.config import Settings

_BEARER_PREFIX = "bearer "
_USER_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True, slots=True)
class AuthUser:
    name: str
    user_id: str = ""
    tenant: str = "local"
    role: str = "user"
    # 空 tuple = 普通会话（完整权限）；PAT 只带显式 scope。
    scopes: tuple[str, ...] = ()
    auth_via_pat: bool = False


@dataclass(slots=True)
class AuthConfig:
    """静态 token 映射 + 可选的企业账号/会话后端。"""

    tokens: dict[str, str]
    allow_anonymous_local: bool = True
    accounts: Any | None = None
    sessions: Any | None = None
    api_tokens: Any | None = None
    cookie_secure: bool = False
    session_cookie: str = "ca_session"
    csrf_cookie: str = "ca_csrf"
    csrf_header: str = "X-CSRF-Token"

    @classmethod
    def from_settings(cls, settings: Settings) -> AuthConfig:
        tokens: dict[str, str] = {}
        raw = str(settings.web_auth_tokens or "").strip()
        if raw:
            tokens.update(_parse_tokens(raw, source="AGENT_WEB_AUTH_TOKENS"))
        path = str(settings.web_auth_tokens_file or "").strip()
        if path:
            try:
                text = Path(path).read_text(encoding="utf-8")
            except OSError as exc:
                raise ValueError(
                    f"无法读取 AGENT_WEB_AUTH_TOKENS_FILE（{path}）：{exc}"
                ) from exc
            tokens.update(_parse_tokens(text, source=path))
        return cls(
            tokens=tokens,
            allow_anonymous_local=bool(settings.web_allow_anonymous_local),
            cookie_secure=bool(settings.web_cookie_secure),
        )

    def bind_accounts(self, accounts: Any, sessions: Any) -> None:
        self.accounts = accounts
        self.sessions = sessions

    @property
    def enabled(self) -> bool:
        return bool(self.tokens) or self.accounts is not None

    def authenticate(self, token: str) -> str | None:
        if not token or not self.tokens:
            return None
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for candidate, user in self.tokens.items():
            expected = candidate
            if candidate.startswith("sha256:"):
                expected = candidate[len("sha256:") :]
                if secrets.compare_digest(digest, expected):
                    return user
                continue
            if secrets.compare_digest(token, candidate):
                return user
        return None

    @staticmethod
    def is_loopback(host: str | None) -> bool:
        if not host:
            return False
        # Starlette TestClient 的 client.host 是 "testclient"；它不是可由
        # 外部网络伪造的来源地址，只用于测试环境。
        if host == "testclient":
            return True
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def user_from_request(self, request: Request) -> AuthUser:
        header = request.headers.get("Authorization", "")
        token = ""
        if header.lower().startswith(_BEARER_PREFIX):
            token = header[len(_BEARER_PREFIX) :].strip()
        if token:
            # Bearer 先匹配静态 token，再匹配个人访问令牌；失败直接 401。
            if self.tokens:
                user = self.authenticate(token)
                if user is not None:
                    return AuthUser(user)
            if (
                self.api_tokens is not None
                and self.accounts is not None
                and token.startswith("ca_pat_")
            ):
                resolved = self.api_tokens.resolve(token)
                if resolved is not None:
                    row = self.accounts.get(
                        str(resolved.get("user_id") or "")
                    )
                    if row is None or row.get("status") != "active":
                        raise _unauthorized()
                    return AuthUser(
                        name=str(row.get("username") or ""),
                        user_id=str(row.get("id") or ""),
                        tenant=str(row.get("tenant") or "default"),
                        role=str(row.get("role") or "member"),
                        scopes=tuple(resolved.get("scopes") or ()),
                        auth_via_pat=True,
                    )
            raise _unauthorized()

        if self.sessions is not None:
            raw = request.cookies.get(self.session_cookie, "")
            if raw:
                resolved = self.sessions.resolve(raw)
                if resolved is None:
                    raise _unauthorized()
                row, csrf = resolved
                request.state.auth_via_cookie = True
                request.state.auth_csrf = csrf
                request.state.session_token = raw
                return AuthUser(
                    name=str(row.get("username") or ""),
                    user_id=str(row.get("id") or ""),
                    tenant=str(row.get("tenant") or "default"),
                    role=str(row.get("role") or "member"),
                )

        if self.enabled:
            raise _unauthorized()

        # 未配置认证：只允许本机匿名。
        client = request.client.host if request.client else ""
        if self.allow_anonymous_local and self.is_loopback(client):
            return AuthUser("local")
        raise _unauthorized()

    @staticmethod
    def _normalized_path(path: str) -> str:
        text = str(path or "")
        if text.startswith("/api/v1/"):
            return "/api/" + text[len("/api/v1/") :]
        if text == "/api/v1":
            return "/api"
        return text

    def required_pat_scope(
        self, method: str, path: str
    ) -> str | None:
        """PAT 的最小 scope；None = 该路径不允许 PAT 访问。"""
        verb = str(method or "GET").upper()
        target = self._normalized_path(path)
        if target == "/api/admin" or target.startswith("/api/admin/"):
            return None
        if target.startswith("/api/tokens") or target.startswith(
            "/api/webhooks"
        ):
            return None
        if target.startswith("/api/push/subscriptions"):
            return None
        if target == "/api/audit":
            return "audit" if verb in {"GET", "HEAD"} else None
        run_prefixes = (
            "/api/steer/",
            "/api/resume",
            "/api/context/",
            "/api/fork/",
            "/api/rewind/",
        )
        if verb not in {"GET", "HEAD", "OPTIONS"} and (
            target == "/api/run"
            or target == "/api/sessions"
            or target.startswith(run_prefixes)
        ):
            return "run"
        if verb in {"GET", "HEAD", "OPTIONS"} and target.startswith(
            "/api/"
        ):
            return "read"
        return None

    def authorize_pat(self, request: Request, user: AuthUser) -> None:
        if not user.auth_via_pat:
            return
        required = self.required_pat_scope(
            request.method, request.url.path
        )
        if required is None or required not in user.scopes:
            raise _forbidden("个人访问令牌权限不足")

    def require_csrf(self, request: Request) -> None:
        """Cookie 身份的非幂等请求必须带双提交 CSRF token。"""
        if request.method.upper() not in _UNSAFE_METHODS:
            return
        if not getattr(request.state, "auth_via_cookie", False):
            return
        site = request.headers.get("sec-fetch-site", "").lower()
        if site == "cross-site":
            raise _forbidden("跨站请求被拒绝")
        origin = request.headers.get("origin", "")
        if origin and not _origin_matches(request, origin):
            raise _forbidden("请求来源不可信")
        header = request.headers.get(self.csrf_header, "")
        cookie = request.cookies.get(self.csrf_cookie, "")
        expected = str(getattr(request.state, "auth_csrf", "") or "")
        if not header or not cookie:
            raise _forbidden("缺少 CSRF token")
        if not (
            secrets.compare_digest(header, cookie)
            and expected
            and secrets.compare_digest(header, expected)
        ):
            raise _forbidden("CSRF token 不匹配")


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=401,
        detail="需要认证",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _forbidden(message: str) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={"code": "auth.forbidden", "message": message},
    )


def _origin_matches(request: Request, origin: str) -> bool:
    try:
        parsed = urlparse(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    candidates = {
        str(request.headers.get("host") or "").lower(),
        str(request.headers.get("x-forwarded-host") or "").lower(),
        str(request.url.netloc or "").lower(),
    }
    return parsed.netloc.lower() in candidates


def _parse_tokens(raw: str, *, source: str) -> dict[str, str]:
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{source} 不是合法 JSON：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{source} 必须是 {{token: user}} 对象")
    tokens: dict[str, str] = {}
    for token, user in payload.items():
        token_text = str(token).strip()
        user_text = str(user).strip()
        if not token_text or not user_text:
            raise ValueError(f"{source} 里存在空 token 或空用户名")
        if not _USER_RE.match(user_text) or user_text in {".", ".."}:
            raise ValueError(
                f"{source} 里用户名 {user_text!r} 非法："
                "只允许字母、数字、下划线、点、连字符（1–64）"
            )
        tokens[token_text] = user_text
    return tokens
