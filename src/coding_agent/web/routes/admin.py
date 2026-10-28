"""企业管理员 API：用户、配额、上游 Key 与用量。

所有路由都经过 `AdminDep`；前端隐藏入口不构成授权。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.control.accounts import AccountError
from coding_agent.costs import CurrencyConfig
from coding_agent.secrets_store import SecretStoreUnavailable
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import AdminDep


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=200)
    email: str = Field(default="", max_length=255)
    role: str = Field(default="member", pattern="^(admin|member)$")


class UpdateUserRequest(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    role: str | None = Field(default=None, pattern="^(owner|admin|member)$")
    email: str | None = Field(default=None, max_length=255)


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=200)


class QuotaRequest(BaseModel):
    daily_tokens: int = Field(default=0, ge=0)
    daily_cost_cny: float = Field(default=0.0, ge=0)
    enabled: bool = False


class ProviderKeyRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=500)


@dataclass(frozen=True)
class AdminDeps:
    settings: Settings
    accounts: Any | None
    sessions: Any | None
    control_store: Any | None
    provider_rows: Callable[[Settings, Any], list[dict[str, Any]]]
    user_store: Callable[[AuthUser], Any]
    auth_events: Any | None = None
    currency: CurrencyConfig = CurrencyConfig()


def _usage_view(row: dict[str, Any], currency: CurrencyConfig) -> dict[str, Any]:
    item = dict(row)
    item["cost_cny"] = float(currency.from_usd(item.get("cost_usd", 0)))
    item["billed_cny"] = float(
        currency.from_usd(item.get("billed_usd", 0))
    )
    item["currency"] = currency.code
    return item


class _EmptyStore:
    def get(self, name: str) -> str:
        return ""

    def has(self, name: str) -> bool:
        return False


def _day(text: str, fallback: date) -> date:
    try:
        return datetime.strptime(str(text), "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return fallback


def register_admin_routes(app: FastAPI, deps: AdminDeps) -> None:
    def _accounts():
        if deps.accounts is None:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "auth.accounts_disabled",
                    "message": "账号后端未启用",
                },
            )
        return deps.accounts

    def _account(user_id: str) -> dict[str, Any]:
        row = _accounts().get(user_id)
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        return row

    @app.get("/api/admin/users")
    async def list_users(admin: AdminDep) -> dict:
        return {"users": _accounts().list(deps.settings.web_account_tenant)}

    @app.post("/api/admin/users")
    async def create_user(payload: CreateUserRequest, admin: AdminDep) -> dict:
        try:
            row = _accounts().create(
                payload.username,
                payload.password,
                email=payload.email,
                role=payload.role,
                tenant=deps.settings.web_account_tenant,
            )
        except AccountError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"user": row}

    @app.patch("/api/admin/users/{user_id}")
    async def update_user(
        user_id: str, payload: UpdateUserRequest, admin: AdminDep
    ) -> dict:
        existing = _account(user_id)
        if existing["role"] == "owner" and (
            payload.status == "disabled"
            or (payload.role is not None and payload.role != "owner")
        ):
            if _accounts().count_active_owners(existing["tenant"]) <= 1:
                raise HTTPException(
                    status_code=409,
                    detail="不能禁用或降级最后一个 owner",
                )
        try:
            row = _accounts().update(
                user_id,
                status=payload.status,
                role=payload.role,
                email=payload.email,
            )
        except AccountError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if payload.status == "disabled" and deps.sessions is not None:
            deps.sessions.revoke_user(user_id)
        return {"user": row}

    @app.post("/api/admin/users/{user_id}/reset-password")
    async def reset_password(
        user_id: str, payload: ResetPasswordRequest, admin: AdminDep
    ) -> dict:
        _account(user_id)
        try:
            _accounts().reset_password(user_id, payload.password)
        except AccountError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if deps.sessions is not None:
            deps.sessions.revoke_user(user_id)
        return {"ok": True}

    @app.get("/api/admin/users/{user_id}/quotas")
    async def get_quota(user_id: str, admin: AdminDep) -> dict:
        row = _account(user_id)
        if deps.control_store is None:
            raise HTTPException(status_code=503, detail="控制面数据库不可用")
        today = datetime.now(UTC).date().isoformat()
        usage = deps.control_store.get_usage_day(
            row["tenant"], row["username"], today
        )
        return {
            "quota": deps.control_store.get_quota(
                row["tenant"], row["username"]
            ),
            "usage": _usage_view(usage, deps.currency),
        }

    @app.put("/api/admin/users/{user_id}/quotas")
    async def save_quota(
        user_id: str, payload: QuotaRequest, admin: AdminDep
    ) -> dict:
        row = _account(user_id)
        if deps.control_store is None:
            raise HTTPException(status_code=503, detail="控制面数据库不可用")
        quota = deps.control_store.set_quota(
            row["tenant"],
            row["username"],
            daily_tokens=payload.daily_tokens,
            daily_cost_cny=payload.daily_cost_cny,
            enabled=payload.enabled,
            updated_by=admin.name,
        )
        return {"quota": quota}

    @app.get("/api/admin/users/{user_id}/providers")
    async def user_providers(user_id: str, admin: AdminDep) -> dict:
        row = _account(user_id)
        target = AuthUser(
            name=row["username"],
            user_id=row["id"],
            tenant=row["tenant"],
            role=row["role"],
        )
        store = deps.user_store(target)
        providers = deps.provider_rows(deps.settings, store)
        return {
            "providers": [
                {
                    "provider": item.get("name", ""),
                    "model": item.get("model", ""),
                    "base_url": item.get("base_url", ""),
                    "has_key": bool(item.get("has_key")),
                    "last4": item.get("last4", ""),
                }
                for item in providers
            ]
        }

    @app.put("/api/admin/users/{user_id}/providers/{provider}")
    async def save_provider_key(
        user_id: str,
        provider: str,
        payload: ProviderKeyRequest,
        admin: AdminDep,
    ) -> dict:
        row = _account(user_id)
        target = AuthUser(
            name=row["username"],
            user_id=row["id"],
            tenant=row["tenant"],
            role=row["role"],
        )
        store = deps.user_store(target)
        available = {
            str(item.get("name") or "")
            for item in deps.provider_rows(deps.settings, store)
        }
        if provider not in available:
            raise HTTPException(status_code=404, detail="provider 不存在")
        try:
            if hasattr(store, "set"):
                store.set(provider, payload.api_key)
            else:  # pragma: no cover - 适配器都应实现 set
                raise HTTPException(status_code=503, detail="密钥存储不可用")
        except SecretStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 加密/主密钥错误
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        last4 = payload.api_key[-4:]
        getter = getattr(store, "last4", None)
        if callable(getter):
            last4 = str(getter(provider) or last4)
        return {"ok": True, "last4": last4}

    @app.delete("/api/admin/users/{user_id}/providers/{provider}")
    async def delete_provider_key(
        user_id: str, provider: str, admin: AdminDep
    ) -> dict:
        row = _account(user_id)
        target = AuthUser(
            name=row["username"],
            user_id=row["id"],
            tenant=row["tenant"],
            role=row["role"],
        )
        store = deps.user_store(target)
        try:
            store.delete(provider)
        except SecretStoreUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return {"ok": True}

    @app.get("/api/admin/providers")
    async def deployment_providers(admin: AdminDep) -> dict:
        rows = deps.provider_rows(deps.settings, _EmptyStore())
        return {
            "providers": [
                {
                    "provider": item.get("name", ""),
                    "model": item.get("model", ""),
                    "base_url": item.get("base_url", ""),
                    "has_key": bool(item.get("has_key")),
                    "last4": "",
                }
                for item in rows
            ]
        }

    @app.get("/api/admin/auth-events")
    async def auth_events(
        admin: AdminDep,
        user: str = "",
        limit: int = Query(50, ge=1, le=200),
    ) -> dict:
        if deps.auth_events is None:
            return {"events": []}
        return {
            "events": deps.auth_events.list_recent(
                username=user.strip(),
                tenant=admin.tenant,
                limit=limit,
            )
        }

    @app.get("/api/admin/usage")
    async def usage(
        admin: AdminDep,
        from_day: str = Query(alias="from"),
        to_day: str = Query(alias="to"),
        user: str = "",
        limit: int = Query(1000, ge=1, le=5000),
        offset: int = Query(0, ge=0),
    ) -> dict:
        if deps.control_store is None:
            raise HTTPException(status_code=503, detail="控制面数据库不可用")
        today = datetime.now(UTC).date()
        start = _day(from_day, today - timedelta(days=6))
        end = _day(to_day, today)
        if start > end:
            raise HTTPException(status_code=422, detail="开始日期晚于结束日期")
        if (end - start).days > 366:
            raise HTTPException(status_code=422, detail="查询区间不能超过一年")
        report = deps.control_store.usage_report(
            admin.tenant,
            start.isoformat(),
            end.isoformat(),
            user=user.strip() or None,
            limit=limit,
            offset=offset,
        )
        report["rows"] = [
            _usage_view(row, deps.currency) for row in report["rows"]
        ]
        report["totals"] = _usage_view(report["totals"], deps.currency)
        report["currency"] = deps.currency.code
        report["usd_to_cny"] = float(deps.currency.usd_to_cny)
        return report
