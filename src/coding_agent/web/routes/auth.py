"""企业账号登录、登出与当前用户信息。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.costs import CurrencyConfig
from coding_agent.web.auth import AuthConfig, AuthUser
from coding_agent.web.dependencies import UserDep

_LOG = logging.getLogger(__name__)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=200)


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


def register_auth_routes(app: FastAPI, deps: AuthRouteDeps) -> None:
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
        request_id = str(
            getattr(request.state, "request_id", "")
            or request.headers.get("X-Request-ID", "")
        )

        def _event(kind: str, username: str) -> None:
            if deps.auth_events is None:
                return
            try:
                deps.auth_events.record(
                    username, kind, ip=ip, request_id=request_id
                )
            except Exception:  # noqa: BLE001 - 审计失败不阻断登录
                _LOG.warning("认证事件写入失败", exc_info=True)

        try:
            deps.login_rate_limit(ip)
        except HTTPException:
            _event("login_rate_limited", payload.username)
            raise
        # 登录失败也计数，避免暴力尝试绕过限速。
        deps.rate_limit(AuthUser(name=payload.username))
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
