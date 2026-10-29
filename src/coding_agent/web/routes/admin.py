"""企业管理员 API：用户、配额、上游 Key 与用量。

所有路由都经过 `AdminDep`；前端隐藏入口不构成授权。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.control.accounts import AccountError
from coding_agent.control.approvals import SelfApprovalError
from coding_agent.costs import CurrencyConfig
from coding_agent.observability import inc
from coding_agent.secrets_store import SecretStoreUnavailable
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import AdminDep, ApproverDep, AuditorDep


class CreateUserRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=8, max_length=200)
    email: str = Field(default="", max_length=255)
    # owner 只能由现有 owner 通过 update 产生，避免新建即抢所有权。
    role: str = Field(
        default="member", pattern="^(admin|approver|auditor|member)$"
    )
    max_access_mode: str = Field(
        default="write_auto", pattern="^(read_only|write_ask|write_auto)$"
    )


class UpdateUserRequest(BaseModel):
    status: str | None = Field(default=None, pattern="^(active|disabled)$")
    role: str | None = Field(
        default=None, pattern="^(owner|admin|approver|auditor|member)$"
    )
    max_access_mode: str | None = Field(
        default=None, pattern="^(read_only|write_ask|write_auto)$"
    )
    email: str | None = Field(default=None, max_length=255)


class ResetPasswordRequest(BaseModel):
    password: str = Field(min_length=8, max_length=200)


class QuotaRequest(BaseModel):
    daily_tokens: int = Field(default=0, ge=0)
    daily_cost_cny: float = Field(default=0.0, ge=0)
    enabled: bool = False


class ProviderKeyRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=500)


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    reason: str = Field(default="", max_length=500)


class WorkspaceAclRequest(BaseModel):
    grantee: str = Field(min_length=1, max_length=128)
    # 目前只支持只读共享；写入/执行必须由 workspace.owner 自己做。
    permission: Literal["read"] = "read"


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
    # RFC-0004：四眼审批队列与管理员 MFA 重置；None = 未启用。
    approvals: Any | None = None
    mfa: Any | None = None
    # RFC-0001 工作区注册表；None = 未启用，ACL 端点返回 503。
    workspace_store: Any | None = None


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
    async def list_users(admin: AuditorDep) -> dict:
        return {"users": _accounts().list(deps.settings.web_account_tenant)}

    @app.post("/api/admin/users")
    async def create_user(payload: CreateUserRequest, admin: AdminDep) -> dict:
        try:
            row = _accounts().create(
                payload.username,
                payload.password,
                email=payload.email,
                role=payload.role,
                max_access_mode=payload.max_access_mode,
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
                max_access_mode=payload.max_access_mode,
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
    async def get_quota(user_id: str, admin: AuditorDep) -> dict:
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
    async def user_providers(user_id: str, admin: AuditorDep) -> dict:
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
    async def deployment_providers(admin: AuditorDep) -> dict:
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
        admin: AuditorDep,
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
        admin: AuditorDep,
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

    def _workspace_store():
        if deps.workspace_store is None:
            raise HTTPException(
                status_code=503,
                detail="工作区注册表未启用或控制面数据库不可用",
            )
        return deps.workspace_store

    def _workspace_in_tenant(workspace_id: str, admin: AuthUser) -> dict:
        row = _workspace_store().get(workspace_id)
        if row is None:
            raise HTTPException(status_code=404, detail="工作区不存在")
        owner = _accounts().get_by_username(row["owner"])
        if owner is None or owner.get("tenant") != admin.tenant:
            # 不跨账号租户管理别人的工作区。
            raise HTTPException(status_code=404, detail="工作区不存在")
        return row

    @app.get("/api/admin/workspaces/{workspace_id}/acl")
    async def list_workspace_acl(
        workspace_id: str, admin: AdminDep
    ) -> dict:
        _workspace_in_tenant(workspace_id, admin)
        return {
            "workspace_id": workspace_id,
            "grants": _workspace_store().acl_list(
                admin.tenant, workspace_id
            ),
        }

    @app.put("/api/admin/workspaces/{workspace_id}/acl")
    async def grant_workspace_acl(
        workspace_id: str,
        payload: WorkspaceAclRequest,
        admin: AdminDep,
    ) -> dict:
        _workspace_in_tenant(workspace_id, admin)
        grantee = payload.grantee.strip()
        target = _accounts().get_by_username(grantee)
        if target is None or target.get("tenant") != admin.tenant:
            raise HTTPException(status_code=404, detail="被授权用户不存在")
        grant = _workspace_store().acl_grant(
            admin.tenant,
            workspace_id,
            grantee,
            granted_by=admin.name,
        )
        return {"ok": True, "grant": grant}

    @app.delete("/api/admin/workspaces/{workspace_id}/acl/{grantee}")
    async def revoke_workspace_acl(
        workspace_id: str, grantee: str, admin: AdminDep
    ) -> dict:
        _workspace_in_tenant(workspace_id, admin)
        removed = _workspace_store().acl_revoke(
            admin.tenant, workspace_id, grantee
        )
        return {"ok": True, "removed": bool(removed)}

    @app.post("/api/admin/users/{user_id}/mfa-reset")
    async def reset_user_mfa(user_id: str, admin: AdminDep) -> dict:
        if deps.mfa is None:
            raise HTTPException(
                status_code=503, detail="MFA 后端未启用"
            )
        row = deps.accounts.get(user_id) if deps.accounts else None
        if row is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        deps.mfa.reset(user_id)
        if deps.sessions is not None:
            deps.sessions.revoke_user(user_id)
        if deps.auth_events is not None:
            try:
                deps.auth_events.record(
                    row.get("username", ""), "mfa_reset"
                )
            except Exception:  # noqa: BLE001 - 审计失败不阻断重置
                pass
        return {"ok": True}

    @app.get("/api/admin/approvals")
    async def pending_approvals(
        admin: ApproverDep,
        limit: int = Query(200, ge=1, le=1000),
    ) -> dict:
        if deps.approvals is None:
            return {"approvals": [], "enabled": False}
        return {
            "enabled": True,
            "approvals": deps.approvals.pending(
                admin.tenant, limit=limit
            ),
        }

    @app.post("/api/admin/approvals/{approval_id}/decide")
    async def decide_approval(
        approval_id: str,
        payload: ApprovalDecisionRequest,
        admin: ApproverDep,
    ) -> dict:
        if deps.approvals is None:
            raise HTTPException(
                status_code=503, detail="四眼审批未启用"
            )
        try:
            row = deps.approvals.decide(
                tenant=admin.tenant,
                approval_id=approval_id,
                approver=admin.name,
                approved=payload.approved,
            )
        except SelfApprovalError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "approval.self",
                    "message": str(exc),
                },
            ) from exc
        if row is None:
            raise HTTPException(status_code=404, detail="审批不存在")
        inc(
            "agent_approvals_total",
            {"decision": "approved" if payload.approved else "denied"},
        )
        if deps.auth_events is not None:
            try:
                deps.auth_events.record(
                    admin.name,
                    "approval_decided",
                )
            except Exception:  # noqa: BLE001
                pass
        return {"ok": True, "approval": row}
