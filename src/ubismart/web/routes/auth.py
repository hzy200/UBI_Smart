"""企业账号登录、登出与当前用户信息。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ubismart.config import Settings
from ubismart.control.mfa import MfaError, otpauth_uri
from ubismart.control.oidc import OidcError
from ubismart.platform.observability import inc
from ubismart.session.costs import CurrencyConfig
from ubismart.web.auth import AuthConfig, AuthUser
from ubismart.web.dependencies import UserDep
from ubismart.web.mailer import send_password_reset

_LOG = logging.getLogger(__name__)
# 按账号的失败锁定：与按 IP/用户名的分钟限流互补，避免换 IP 继续撞库。
_LOGIN_MAX_FAILURES = 10
_LOGIN_LOCKOUT_WINDOW_S = 900


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


class MfaVerifyRequest(BaseModel):
    mfa_token: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=64)


class MfaEnrollRequest(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class MfaConfirmRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


class MfaDisableRequest(BaseModel):
    password: str = Field(min_length=1, max_length=200)
    code: str = Field(min_length=1, max_length=64)


class PasswordResetRequest(BaseModel):
    identifier: str = Field(min_length=1, max_length=255)


class PasswordResetConfirm(BaseModel):
    token: str = Field(min_length=1, max_length=200)
    new_password: str = Field(min_length=8, max_length=200)


@dataclass(frozen=True)
class AuthRouteDeps:
    settings: Settings
    auth: AuthConfig
    accounts: Any | None
    sessions: Any | None
    control_store: Any | None
    rate_limit: Callable[[AuthUser], None]
    login_rate_limit: Callable[[str], None] = lambda _ip: None
    auth_events: Any | None = None
    currency: CurrencyConfig = CurrencyConfig()
    # RFC-0004：OIDC / MFA；None = 未启用。
    oidc_client: Any | None = None
    oidc_states: Any | None = None
    oidc_enabled: bool = False
    mfa_store: Any | None = None
    mfa_challenges: Any | None = None
    mfa_enabled: bool = False
    mfa_issuer: str = "CodingAgent"
    mfa_window_steps: int = 1
    mfa_max_attempts: int = 5
    mfa_challenge_ttl_s: int = 300
    # 自助密码重置；None = 未启用。
    password_resets: Any | None = None


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _quota_payload(deps: AuthRouteDeps, user: AuthUser) -> dict[str, Any]:
    empty_quota = {
        "daily_tokens": 0,
        "daily_cost_cny": 0.0,
        "enabled": False,
    }
    empty_usage = {
        "day": _today(),
        "input_tokens": 0,
        "output_tokens": 0,
        "worker_input_tokens": 0,
        "worker_output_tokens": 0,
        "tokens": 0,
        "cost_usd": 0.0,
        "billed_usd": 0.0,
        "runs": 0,
        "reserved_tokens": 0,
        "reserved_usd": 0.0,
    }
    if deps.control_store is None:
        return {"quota": empty_quota, "usage": empty_usage}
    try:
        quota = deps.control_store.get_quota(user.tenant, user.name)
        usage = deps.control_store.get_usage_day(user.tenant, user.name, _today())
    except Exception:  # noqa: BLE001 - 报表失败不阻断 /api/me
        return {"quota": empty_quota, "usage": empty_usage}
    usage_view = dict(usage or empty_usage)
    usage_view["cost_cny"] = float(
        deps.currency.from_usd(usage_view.get("cost_usd", 0))
    )
    usage_view["billed_cny"] = float(
        deps.currency.from_usd(usage_view.get("billed_usd", 0))
    )
    usage_view["currency"] = deps.currency.code
    return {
        "quota": quota or empty_quota,
        "usage": usage_view,
    }


def _set_auth_cookies(
    response: Response,
    deps: AuthRouteDeps,
    session: dict[str, str],
) -> None:
    secure = bool(deps.settings.web_cookie_secure)
    # 跨源独立部署必须 Secure + SameSite=None；同源反代用 Lax 即可。
    samesite = "none" if secure else "lax"
    max_age = int(deps.settings.web_session_ttl_s or 43200)
    response.set_cookie(
        "ca_session",
        session["token"],
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/",
    )
    response.set_cookie(
        "ca_csrf",
        session["csrf"],
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite=samesite,
        path="/",
    )


def _oidc_role(claims: dict[str, Any], settings: Settings) -> str:
    """只接受显式 role map 的映射，不直接把 IdP claim 当角色。"""
    default = str(settings.oidc_default_role or "member")
    if default not in {"owner", "admin", "approver", "auditor", "member"}:
        default = "member"
    raw = str(settings.oidc_role_map or "").strip()
    if not raw:
        return default
    try:
        mapping = json.loads(raw)
    except ValueError:
        return default
    if not isinstance(mapping, dict):
        return default
    values = claims.get(str(settings.oidc_role_claim or "groups"))
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return default
    roles = {str(mapping.get(str(item)) or "") for item in values}
    # 从高到低取第一个命中；approver/auditor 是更窄的角色，不能被
    # 同组里的 member 覆盖。
    for role in ("owner", "admin", "approver", "auditor", "member"):
        if role in roles:
            return role
    return default


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def _request_id(request: Request) -> str:
    return str(
        getattr(request.state, "request_id", "")
        or request.headers.get("X-Request-ID", "")
    )


def _oidc_error(code: str) -> RedirectResponse:
    # 不回显 IdP 的原始 error_description，避免把内部细节带到前端。
    return RedirectResponse(url=f"/?auth_error={code}", status_code=303)


def register_auth_routes(app: FastAPI, deps: AuthRouteDeps) -> None:
    def _auth_event(kind: str, username: str, request: Request) -> None:
        inc("agent_auth_events_total", {"kind": kind})
        if deps.auth_events is None:
            return
        try:
            deps.auth_events.record(
                username,
                kind,
                ip=_client_ip(request),
                request_id=_request_id(request),
            )
        except Exception:  # noqa: BLE001 - 审计失败不阻断认证
            _LOG.warning("认证事件写入失败", exc_info=True)

    def _mfa_challenge_payload(row: dict[str, Any]) -> dict | None:
        if not deps.mfa_enabled:
            return None
        if deps.mfa_store is None or deps.mfa_challenges is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "auth.mfa_unavailable",
                    "message": "MFA 后端未启用",
                },
            )
        status = deps.mfa_store.status(row["id"])
        if not status.get("enabled"):
            return None
        token = deps.mfa_challenges.create(
            row["id"], ttl_s=deps.mfa_challenge_ttl_s
        )
        return {
            "mfa_required": True,
            "mfa_token": token,
            "user": row,
        }

    @app.post("/api/auth/login")
    async def login(payload: LoginRequest, request: Request, response: Response) -> dict:
        if deps.accounts is None or deps.sessions is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "auth.accounts_disabled",
                    "message": "账号登录未启用",
                },
            )
        ip = request.client.host if request.client else ""

        def _event(kind: str, username: str) -> None:
            _auth_event(kind, username, request)

        try:
            deps.login_rate_limit(ip)
        except HTTPException:
            _event("login_rate_limited", payload.username)
            raise
        # 登录失败也计数，避免暴力尝试绕过限速。
        deps.rate_limit(AuthUser(name=payload.username))
        if deps.auth_events is not None:
            try:
                failures = deps.auth_events.recent_failures(
                    payload.username, window_s=_LOGIN_LOCKOUT_WINDOW_S
                )
            except Exception:  # noqa: BLE001 - 审计后端异常不阻断登录
                failures = 0
            if failures >= _LOGIN_MAX_FAILURES:
                _event("login_locked", payload.username)
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "auth.login_locked",
                        "message": "登录失败次数过多，请稍后再试",
                    },
                    headers={
                        "Retry-After": str(_LOGIN_LOCKOUT_WINDOW_S)
                    },
                )
        try:
            deps.sessions.purge_expired()
        except Exception:  # noqa: BLE001 - 清理失败不阻断登录
            pass
        row = deps.accounts.authenticate(payload.username, payload.password)
        if row is None:
            _event("login_failed", payload.username)
            _LOG.warning("企业账号登录失败 user=%s", payload.username)
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "auth.invalid_credentials",
                    "message": "用户名或密码错误",
                },
            )
        challenge = _mfa_challenge_payload(row)
        if challenge is not None:
            _event("mfa_required", row["username"])
            return challenge
        session = deps.sessions.create(row["id"])
        _set_auth_cookies(response, deps, session)
        _event("login_ok", row["username"])
        _LOG.info("企业账号登录成功 user=%s", row["username"])
        return {"ok": True, "user": row, "csrf_token": session["csrf"]}

    @app.post("/api/auth/logout")
    async def logout(
        response: Response, request: Request, user: UserDep
    ) -> dict:
        token = str(getattr(request.state, "session_token", "") or "")
        if deps.sessions is not None and token:
            deps.sessions.revoke(token)
        if deps.auth_events is not None:
            try:
                deps.auth_events.record(
                    user.name,
                    "logout",
                    ip=request.client.host if request.client else "",
                    request_id=str(
                        getattr(request.state, "request_id", "") or ""
                    ),
                )
            except Exception:  # noqa: BLE001
                pass
        response.delete_cookie("ca_session", path="/")
        response.delete_cookie("ca_csrf", path="/")
        return {"ok": True}

    @app.get("/api/auth/sessions")
    async def list_sessions(request: Request, user: UserDep) -> dict:
        if deps.sessions is None:
            raise HTTPException(status_code=503, detail="会话后端未启用")
        if not user.user_id:
            return {"sessions": []}
        token = str(getattr(request.state, "session_token", "") or "")
        return {
            "sessions": deps.sessions.list_for_user(
                user.user_id, current_token=token
            )
        }

    @app.post("/api/auth/sessions/revoke-others")
    async def revoke_other_sessions(
        request: Request, user: UserDep
    ) -> dict:
        if deps.sessions is None:
            raise HTTPException(status_code=503, detail="会话后端未启用")
        if not user.user_id:
            return {"ok": True, "revoked": 0}
        token = str(getattr(request.state, "session_token", "") or "")
        revoked = deps.sessions.revoke_others(
            user.user_id, keep_token=token
        )
        _auth_event("sessions_revoked", user.name, request)
        return {"ok": True, "revoked": int(revoked)}

    # ---------------- 自助密码重置 ----------------

    @app.post("/api/auth/password-reset/request")
    async def request_password_reset(
        payload: PasswordResetRequest, request: Request
    ) -> dict:
        deps.login_rate_limit(_client_ip(request))
        generic = {"ok": True}
        if deps.accounts is None or deps.password_resets is None:
            return generic
        identifier = payload.identifier.strip()
        row = deps.accounts.get_by_username(identifier) or (
            deps.accounts.get_by_email(identifier)
        )
        if (
            row is None
            or row.get("status") != "active"
            or not row.get("email")
        ):
            # 不区分“账号不存在/没邮箱/发送失败”，避免枚举。
            return generic
        token = await asyncio.to_thread(
            deps.password_resets.create, row["id"]
        )
        base = str(
            getattr(deps.settings, "web_public_url", "") or ""
        ).rstrip("/")
        value = (
            f"{base}/?reset_token={quote(token)}" if base else token
        )
        await asyncio.to_thread(
            send_password_reset, deps.settings, row["email"], value
        )
        _auth_event("password_reset_requested", row["username"], request)
        return generic

    @app.post("/api/auth/password-reset/confirm")
    async def confirm_password_reset(
        payload: PasswordResetConfirm, request: Request
    ) -> dict:
        if deps.password_resets is None:
            raise HTTPException(status_code=503, detail="密码重置未启用")
        user_id = await asyncio.to_thread(
            deps.password_resets.consume,
            payload.token,
            payload.new_password,
        )
        if not user_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "auth.reset_invalid",
                    "message": "重置令牌无效或已过期",
                },
            )
        if deps.sessions is not None:
            deps.sessions.revoke_user(str(user_id))
        _auth_event("password_reset_ok", "", request)
        return {"ok": True}

    # ---------------- OIDC（RFC-0004） ----------------

    @app.get("/api/auth/oidc/status")
    async def oidc_status() -> dict:
        return {
            "enabled": bool(
                deps.oidc_enabled and deps.oidc_client is not None
            )
        }

    @app.get("/api/auth/oidc/start")
    async def oidc_start(
        request: Request,
        return_to: str = Query(default="/", max_length=512),
    ) -> dict:
        if (
            not deps.oidc_enabled
            or deps.oidc_client is None
            or deps.oidc_states is None
            or deps.accounts is None
            or deps.sessions is None
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "auth.oidc_disabled",
                    "message": "OIDC 登录未启用",
                },
            )
        try:
            deps.login_rate_limit(_client_ip(request))
        except HTTPException:
            _auth_event("oidc_rate_limited", "", request)
            raise
        try:
            await asyncio.to_thread(deps.oidc_states.purge_expired)
            target = (
                return_to
                if return_to.startswith("/")
                and not return_to.startswith("//")
                else "/"
            )
            state = await asyncio.to_thread(
                deps.oidc_states.create,
                tenant=deps.settings.web_account_tenant,
                redirect_uri=deps.oidc_client.settings.redirect_uri,
                return_to=target,
                ttl_s=int(deps.settings.oidc_state_ttl_s or 600),
            )
            url = await asyncio.to_thread(
                deps.oidc_client.authorization_url,
                state=state["state"],
                nonce=state["nonce"],
                code_challenge=state["code_challenge"],
            )
        except OidcError as exc:
            _LOG.warning("OIDC 启动失败：%s", type(exc).__name__)
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "auth.oidc_unavailable",
                    "message": "OIDC 提供方不可用",
                },
            ) from exc
        return {"authorization_url": url}

    @app.get("/api/auth/oidc/callback")
    async def oidc_callback(
        request: Request,
        code: str = Query(default=""),
        state: str = Query(default=""),
        error: str = Query(default=""),
    ) -> Response:
        if error:
            _auth_event("oidc_login_failed", "", request)
            return _oidc_error("oidc_denied")
        if not code or not state:
            return _oidc_error("oidc_state")
        if (
            deps.oidc_client is None
            or deps.oidc_states is None
            or deps.accounts is None
            or deps.sessions is None
        ):
            return _oidc_error("oidc_unavailable")
        consumed = await asyncio.to_thread(
            deps.oidc_states.consume, state
        )
        if consumed is None:
            _auth_event("oidc_login_failed", "", request)
            return _oidc_error("oidc_state")
        try:
            token = await asyncio.to_thread(
                deps.oidc_client.exchange_code,
                code=code,
                code_verifier=consumed["code_verifier"],
                redirect_uri=consumed["redirect_uri"],
            )
            claims = await asyncio.to_thread(
                deps.oidc_client.verify_id_token,
                token["id_token"],
                nonce=consumed["nonce"],
            )
        except OidcError as exc:
            _LOG.warning("OIDC 回调失败：%s", type(exc).__name__)
            _auth_event("oidc_login_failed", "", request)
            return _oidc_error("oidc_failed")
        subject = str(claims.get("sub") or "")
        if not subject:
            return _oidc_error("oidc_failed")
        settings = deps.settings
        username = str(
            claims.get(settings.oidc_username_claim)
            or claims.get(settings.oidc_email_claim)
            or subject
        )[:64]
        email = str(
            claims.get(settings.oidc_email_claim) or ""
        )[:255]
        role = _oidc_role(claims, settings)
        row = await asyncio.to_thread(
            deps.accounts.find_or_create_oidc,
            issuer=deps.oidc_client.settings.issuer,
            subject=subject,
            username=username,
            email=email,
            role=role,
            auto_create=bool(settings.oidc_auto_create_users),
        )
        if row is None:
            _auth_event("oidc_login_failed", username, request)
            return _oidc_error("oidc_no_user")
        session = await asyncio.to_thread(
            deps.sessions.create, row["id"]
        )
        target = str(consumed.get("return_to") or "/")
        if not target.startswith("/") or target.startswith("//"):
            target = "/"
        response = RedirectResponse(url=target, status_code=303)
        _set_auth_cookies(response, deps, session)
        _auth_event("oidc_login_ok", row["username"], request)
        return response

    # ---------------- TOTP MFA（RFC-0004） ----------------

    @app.post("/api/auth/mfa/verify")
    async def mfa_verify(
        payload: MfaVerifyRequest, request: Request, response: Response
    ) -> dict:
        if (
            deps.mfa_store is None
            or deps.mfa_challenges is None
            or deps.accounts is None
            or deps.sessions is None
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "auth.mfa_unavailable",
                    "message": "MFA 未启用",
                },
            )
        deps.login_rate_limit(_client_ip(request))
        challenge = await asyncio.to_thread(
            deps.mfa_challenges.get, payload.mfa_token
        )
        if challenge is None:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "auth.mfa_challenge_invalid",
                    "message": "动态码挑战已失效，请重新登录",
                },
            )
        if int(challenge["attempts"]) >= int(deps.mfa_max_attempts):
            await asyncio.to_thread(
                deps.mfa_challenges.delete, payload.mfa_token
            )
            raise HTTPException(
                status_code=429,
                detail={
                    "code": "auth.mfa_locked",
                    "message": "动态码尝试次数过多，请重新登录",
                },
            )
        row = await asyncio.to_thread(
            deps.accounts.get, challenge["user_id"]
        )
        if row is None or row.get("status") != "active":
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "auth.invalid_credentials",
                    "message": "账号不可用",
                },
            )
        code = payload.code.strip()
        if len(code) == 6 and code.isdigit():
            ok = await asyncio.to_thread(
                deps.mfa_store.verify,
                tenant=row.get("tenant") or "default",
                user_id=row["id"],
                code=code,
                window=int(deps.mfa_window_steps),
            )
        else:
            ok = await asyncio.to_thread(
                deps.mfa_store.verify_recovery,
                user_id=row["id"],
                code=code,
            )
        if not ok:
            attempts = await asyncio.to_thread(
                deps.mfa_challenges.fail, payload.mfa_token
            )
            _auth_event("mfa_failed", row["username"], request)
            if attempts >= int(deps.mfa_max_attempts):
                await asyncio.to_thread(
                    deps.mfa_challenges.delete, payload.mfa_token
                )
                raise HTTPException(
                    status_code=429,
                    detail={
                        "code": "auth.mfa_locked",
                        "message": "动态码尝试次数过多，请重新登录",
                    },
                )
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "auth.mfa_invalid",
                    "message": "动态码不正确",
                },
            )
        await asyncio.to_thread(
            deps.mfa_challenges.delete, payload.mfa_token
        )
        session = await asyncio.to_thread(
            deps.sessions.create, row["id"]
        )
        _set_auth_cookies(response, deps, session)
        _auth_event("mfa_ok", row["username"], request)
        return {
            "ok": True,
            "user": row,
            "csrf_token": session["csrf"],
        }

    @app.get("/api/auth/mfa/status")
    async def mfa_status(user: UserDep) -> dict:
        if deps.mfa_store is None:
            return {"enabled": False, "recovery_codes": 0}
        return deps.mfa_store.status(user.user_id)

    @app.post("/api/auth/mfa/enroll")
    async def mfa_enroll(
        payload: MfaEnrollRequest, request: Request, user: UserDep
    ) -> dict:
        if (
            deps.mfa_store is None
            or deps.accounts is None
            or not user.user_id
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "auth.mfa_unavailable",
                    "message": "MFA 未启用",
                },
            )
        if deps.accounts.authenticate(user.name, payload.password) is None:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "auth.password_invalid",
                    "message": "密码不正确；SSO 账号的 MFA 由 IdP 管理",
                },
            )
        secret = deps.mfa_store.enroll(
            tenant=user.tenant, user_id=user.user_id
        )
        _auth_event("mfa_enroll", user.name, request)
        return {
            "secret": secret,
            "otpauth_uri": otpauth_uri(
                deps.mfa_issuer, user.name, secret
            ),
        }

    @app.post("/api/auth/mfa/confirm")
    async def mfa_confirm(
        payload: MfaConfirmRequest, user: UserDep
    ) -> dict:
        if deps.mfa_store is None or not user.user_id:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "auth.mfa_unavailable",
                    "message": "MFA 未启用",
                },
            )
        try:
            codes = deps.mfa_store.confirm(
                tenant=user.tenant,
                user_id=user.user_id,
                code=payload.code,
                window=int(deps.mfa_window_steps),
            )
        except MfaError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "auth.mfa_invalid",
                    "message": str(exc),
                },
            ) from exc
        return {"ok": True, "enabled": True, "recovery_codes": codes}

    @app.post("/api/auth/mfa/disable")
    async def mfa_disable(
        payload: MfaDisableRequest, user: UserDep
    ) -> dict:
        if (
            deps.mfa_store is None
            or deps.accounts is None
            or not user.user_id
        ):
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "auth.mfa_unavailable",
                    "message": "MFA 未启用",
                },
            )
        if deps.accounts.authenticate(user.name, payload.password) is None:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "auth.password_invalid",
                    "message": "密码不正确",
                },
            )
        code = payload.code.strip()
        ok = deps.mfa_store.verify(
            tenant=user.tenant,
            user_id=user.user_id,
            code=code,
            window=int(deps.mfa_window_steps),
        ) or deps.mfa_store.verify_recovery(
            user_id=user.user_id, code=code
        )
        if not ok:
            raise HTTPException(
                status_code=401,
                detail={
                    "code": "auth.mfa_invalid",
                    "message": "动态码不正确",
                },
            )
        deps.mfa_store.disable(user.user_id)
        return {"ok": True, "enabled": False}

    @app.get("/api/me")
    async def me(user: UserDep) -> dict:
        quota = _quota_payload(deps, user)
        profile = None
        if deps.accounts is not None and user.user_id:
            profile = deps.accounts.get(user.user_id)
        return {
            "user": profile
            or {
                "id": user.user_id,
                "username": user.name,
                "tenant": user.tenant,
                "role": user.role,
                "email": "",
                "status": "active",
                "created_at": "",
                "last_login_at": "",
            },
            "quotas": quota["quota"],
            "usage_today": quota["usage"],
            "currency": deps.currency.code,
        }

    @app.get("/api/me/usage")
    async def my_usage(
        user: UserDep,
        from_day: str = Query(alias="from"),
        to_day: str = Query(alias="to"),
    ) -> dict:
        if deps.control_store is None:
            return {"rows": [], "totals": {}}
        return deps.control_store.usage_report(
            user.tenant, from_day, to_day, user=user.name
        )
