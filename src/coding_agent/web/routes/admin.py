"""企业管理员 API：用户、配额、上游 Key 与用量。

所有路由都经过 `AdminDep`；前端隐藏入口不构成授权。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.control.accounts import AccountError
from coding_agent.control.approvals import SelfApprovalError
from coding_agent.costs import CurrencyConfig
from coding_agent.platform.observability import inc, snapshot
from coding_agent.platform.secrets_store import SecretStoreUnavailable
from coding_agent.sse_stream import sse_headers, with_heartbeat
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import (
    AdminDep,
    ApprovalReadDep,
    ApproverDep,
    AuditorDep,
)
from coding_agent.web.version import WEB_API_VERSION


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


class BatchApprovalRequest(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=200)
    approved: bool


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


def _epoch(value: str, *, end_of_day: bool = False) -> str:
    """日期/ISO → epoch 秒字符串；空或非法值返回空串（= 不过滤）。"""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        if len(text) == 10:
            parsed = datetime.strptime(text, "%Y-%m-%d").replace(
                tzinfo=UTC
            )
            if end_of_day:
                parsed += timedelta(days=1, seconds=-1)
            return str(int(parsed.timestamp()))
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return str(int(parsed.timestamp()))
    except ValueError:
        return ""


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

    @app.get("/api/admin/audit")
    async def audit(
        admin: AuditorDep,
        user: str = "",
        thread_id: str = "",
        kind: str = "",
        tool: str = "",
        ok: bool | None = None,
        since: str = "",
        until: str = "",
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict:
        """组织成员的审计索引；可信源仍是各租户的哈希链审计文件。"""
        if deps.control_store is None:
            raise HTTPException(status_code=503, detail="控制面数据库不可用")
        names = (
            [row["username"] for row in deps.accounts.list(admin.tenant)]
            if deps.accounts is not None
            else [admin.name]
        )
        if user and user not in names:
            return {"records": [], "total": 0, "has_more": False}
        return deps.control_store.list_audit(
            names,
            user=user.strip(),
            thread_id=thread_id.strip(),
            kind=kind.strip(),
            tool=tool.strip(),
            ok=ok,
            since=since.strip(),
            until=until.strip(),
            limit=limit,
            offset=offset,
        )

    @app.get("/api/admin/health")
    async def health(admin: AuditorDep) -> dict:
        """管理端健康摘要：数据库连通性 + 进程内计数/连接数。"""
        database_ok = bool(
            deps.control_store is not None and deps.control_store.ping()
        )
        metrics = snapshot()
        return {
            "ok": database_ok,
            "time": datetime.now(UTC).isoformat(timespec="seconds"),
            "api_version": WEB_API_VERSION,
            "database": {
                "ok": database_ok,
                "error": "" if database_ok else "unavailable",
            },
            "metrics": metrics,
        }

    @app.post("/api/admin/search/reindex")
    async def reindex_search(admin: AdminDep) -> dict:
        """重建组织成员的会话搜索索引（标题/摘要）。

        transcript 正文不在控制面，无法从这里反推；已有的正文索引会保留，
        空正文用 `control_sessions.last_message` 兜底。完整正文在用户下次
        保存轨迹时补齐。
        """
        if deps.control_store is None:
            raise HTTPException(status_code=503, detail="控制面数据库不可用")
        names = (
            [row["username"] for row in deps.accounts.list(admin.tenant)]
            if deps.accounts is not None
            else [admin.name]
        )
        count = await asyncio.to_thread(
            deps.control_store.rebuild_session_search, names
        )
        return {"ok": True, "reindexed": count}

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

    @app.get("/api/admin/workspaces")
    async def list_workspaces(
        admin: AuditorDep,
        q: str = "",
        kind: str = "",
        limit: int = Query(200, ge=1, le=500),
    ) -> dict:
        """组织内可见的工作区；auditor 只拿到元数据，不含宿主路径。"""
        store = _workspace_store()
        rows = await asyncio.to_thread(
            store.list_visible,
            admin.tenant,
            admin.name,
            admin.role,
            limit=limit,
        )
        query = q.strip().lower()
        if query:
            rows = [
                row
                for row in rows
                if query in str(row.get("owner", "")).lower()
                or query in str(row.get("display_name", "")).lower()
                or query in str(row.get("root_path", "")).lower()
                or query in str(row.get("workspace_id", "")).lower()
            ]
        if kind:
            rows = [
                row
                for row in rows
                if str(row.get("kind") or "") == kind
            ]
        return {"workspaces": rows}

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

    @app.get("/api/admin/approvals/history")
    async def approval_history(
        admin: ApprovalReadDep,
        status: str = "",
        user: str = "",
        from_day: str = Query("", alias="from"),
        to_day: str = Query("", alias="to"),
        limit: int = Query(100, ge=1, le=500),
        offset: int = Query(0, ge=0),
    ) -> dict:
        if status and status not in {
            "pending",
            "approved",
            "denied",
            "expired",
        }:
            raise HTTPException(status_code=422, detail="未知审批状态")
        if deps.approvals is None:
            return {
                "enabled": False,
                "records": [],
                "total": 0,
                "has_more": False,
            }
        data = await asyncio.to_thread(
            deps.approvals.history,
            admin.tenant,
            status=status.strip(),
            requested_by=user.strip(),
            from_ts=_epoch(from_day),
            to_ts=_epoch(to_day, end_of_day=True),
            limit=limit,
            offset=offset,
        )
        data["enabled"] = True
        return data

    @app.get("/api/admin/approvals/stream")
    async def approval_stream(
        request: Request,
        admin: ApproverDep,
    ) -> StreamingResponse:
        """审批队列 SSE：快照变化时推送，避免管理端每 5s 空轮询。

        审批可能由另一个进程写入数据库，所以服务端仍以数据库为准做对比；
        这里只降低前端延迟与无效请求，不改变审批事实源。
        """

        async def events() -> AsyncIterator[str]:
            if deps.approvals is None:
                yield 'data: {"enabled": false, "approvals": [], "count": 0}\n\n'
                return
            last = ""
            while not await request.is_disconnected():
                try:
                    rows = await asyncio.to_thread(
                        deps.approvals.pending, admin.tenant
                    )
                except Exception as exc:  # noqa: BLE001 - 用事件告知客户端
                    yield (
                        "event: error\n"
                        f"data: {json.dumps({'error': str(exc)[:200]})}\n\n"
                    )
                    await asyncio.sleep(5)
                    continue
                payload = json.dumps(
                    {
                        "enabled": True,
                        "approvals": rows,
                        "count": len(rows),
                    },
                    ensure_ascii=False,
                )
                if payload != last:
                    last = payload
                    yield f"data: {payload}\n\n"
                await asyncio.sleep(2)

        return StreamingResponse(
            with_heartbeat(events(), interval_s=15.0),
            media_type="text/event-stream",
            headers=sse_headers(),
        )

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

    @app.post("/api/admin/approvals/batch")
    async def batch_approvals(
        payload: BatchApprovalRequest,
        admin: ApproverDep,
    ) -> dict:
        if deps.approvals is None:
            raise HTTPException(status_code=503, detail="四眼审批未启用")
        results: list[dict[str, Any]] = []
        updated = 0
        for approval_id in payload.ids:
            try:
                row = await asyncio.to_thread(
                    deps.approvals.decide,
                    tenant=admin.tenant,
                    approval_id=approval_id,
                    approver=admin.name,
                    approved=payload.approved,
                )
            except SelfApprovalError as exc:
                results.append(
                    {"id": approval_id, "ok": False, "error": str(exc)}
                )
                continue
            if row is None:
                results.append(
                    {
                        "id": approval_id,
                        "ok": False,
                        "error": "审批不存在",
                    }
                )
                continue
            updated += 1
            results.append(
                {
                    "id": approval_id,
                    "ok": True,
                    "status": row.get("status", ""),
                }
            )
        if updated:
            inc(
                "agent_approvals_total",
                {
                    "decision": (
                        "approved" if payload.approved else "denied"
                    )
                },
                updated,
            )
        return {"ok": True, "updated": updated, "results": results}
