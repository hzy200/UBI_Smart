"""恢复中心、记忆与审计查询路由。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from coding_agent.audit import read_records_many
from coding_agent.identity.tenancy import WebTenant
from coding_agent.memory.sessions import SessionIndex
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import UserDep
from coding_agent.web.validation import check_thread_id

_LOG = logging.getLogger(__name__)


class MemoryRequest(BaseModel):
    action: Literal["remember", "forget", "clear", "pin", "priority"]
    thread_id: str = ""
    text: str = Field(default="", max_length=4_000)
    index: int = -1
    scope: str = "project"
    pinned: bool = True
    priority: int = 0


@dataclass(frozen=True)
class OpsDeps:
    tenant: Callable[[AuthUser], WebTenant]
    sweep_expired: Callable[[], Awaitable[None]]
    load_configs: Callable[[AuthUser, WebTenant], None]
    config_for: Callable[..., Any]
    deleted_sessions: set[tuple[str, str]]
    agent_client: Any
    agent_json: Callable[..., Awaitable[dict[str, Any]]]
    ops_runtime: Callable[[WebTenant, Any], Any]
    rate_limit: Callable[[AuthUser], None]
    get_entry: Callable[[str, str], Any]
    control_store: Any
    run_leases: Any | None = None
    idempotency: Any | None = None


def register_ops_routes(app: FastAPI, deps: OpsDeps) -> None:
    @app.get("/api/recovery")
    async def recovery(user: UserDep) -> dict:
        """列出需要用户处理的会话：待审批、暂停、异常中断或仍在运行。"""
        await deps.sweep_expired()
        tenant = deps.tenant(user)
        await asyncio.to_thread(deps.load_configs, user, tenant)
        if deps.agent_client is not None:
            data = await deps.agent_json(
                user, tenant, None, "GET", "/v1/recovery"
            )
            rows = []
            for item in data.get("items", []):
                thread = str(item.get("thread_id") or "")
                if not thread:
                    continue
                if (user.name, thread) in deps.deleted_sessions:
                    continue
                config = deps.config_for(
                    user, tenant, thread, create=False
                )
                rows.append(
                    {
                        **item,
                        "title": (
                            (config.title if config else "")
                            or item.get("title", "")
                        ),
                        "workspace": (
                            config.workspace
                            if config
                            else item.get("workspace", "")
                        ),
                    }
                )
            return {"items": rows}
        found = SessionIndex(
            tenant.settings.resolved_audit_dir
        ).list(limit=50)
        rows: list[dict[str, Any]] = []
        checked = 0
        for info in found:
            public_thread = tenant.public_thread(info.thread_id)
            if (user.name, public_thread) in deps.deleted_sessions:
                continue
            config = deps.config_for(
                user, tenant, public_thread, create=False
            )
            entry = deps.get_entry(user.name, public_thread)
            internal = tenant.thread_id(public_thread)
            status = ""
            approvals = 0
            if entry is not None:
                if getattr(entry.runtime, "_stream_active", False):
                    status = "running"
                elif entry.runtime.is_paused(internal):
                    status = "paused"
                else:
                    try:
                        approvals = len(
                            await entry.runtime.pending_approvals(internal)
                        )
                    except Exception:  # noqa: BLE001
                        approvals = 0
                    if approvals:
                        status = "approval"
            elif info.interrupted and checked < 10:
                checked += 1
                runtime = deps.ops_runtime(tenant, config)
                try:
                    if runtime.is_paused(internal):
                        status = "paused"
                    else:
                        approvals = len(
                            await runtime.pending_approvals(internal)
                        )
                        if approvals:
                            status = "approval"
                        else:
                            status = "interrupted"
                except Exception:  # noqa: BLE001 - 探测失败仍按中断展示
                    status = "interrupted"
                finally:
                    await runtime.aclose()
            if not status:
                continue
            rows.append(
                {
                    "thread_id": public_thread,
                    "title": (
                        (config.title if config else "")
                        or info.title
                        or public_thread
                    ),
                    "workspace": (
                        config.workspace if config else info.workspace
                    ),
                    "last_active": info.last_seen,
                    "status": status,
                    "approvals": approvals,
                }
            )
        return {"items": rows}

    @app.get("/api/memory")
    async def memory(
        user: UserDep,
        scope: str = "project",
    ) -> dict:
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            return await deps.agent_json(
                user, tenant, None, "GET", "/v1/memory",
                params={"scope": scope},
            )
        runtime = deps.ops_runtime(tenant, None)
        try:
            facts = runtime.memories(scope=scope)
        finally:
            await runtime.aclose()
        return {"facts": facts}

    @app.post("/api/memory")
    async def memory_write(
        request: MemoryRequest,
        user: UserDep,
    ) -> dict:
        deps.rate_limit(user)
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            return await deps.agent_json(
                user, tenant, None, "POST", "/v1/memory",
                json_body=request.model_dump(),
            )
        runtime = deps.ops_runtime(tenant, None)
        internal = (
            tenant.thread_id(request.thread_id)
            if request.thread_id
            else ""
        )
        try:
            if request.action == "remember":
                facts = runtime.remember(
                    request.text,
                    thread_id=internal,
                    scope=request.scope,
                )
            elif request.action == "forget":
                facts = runtime.forget(
                    request.index,
                    thread_id=internal,
                    scope=request.scope,
                )
            elif request.action == "clear":
                facts = runtime.clear_memory(
                    thread_id=internal, scope=request.scope
                )
            elif request.action == "pin":
                facts = runtime.pin_memory(
                    request.index,
                    pinned=request.pinned,
                    thread_id=internal,
                    scope=request.scope,
                )
            else:
                facts = runtime.set_memory_priority(
                    request.index,
                    request.priority,
                    thread_id=internal,
                    scope=request.scope,
                )
        except (IndexError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            await runtime.aclose()
        return {"facts": facts}

    @app.post("/api/ops/leases/{thread_id}/release")
    async def release_lease(thread_id: str, user: UserDep) -> dict:
        """强制释放当前用户某个会话的运行租约（崩溃恢复用）。"""
        deps.rate_limit(user)
        if deps.run_leases is None:
            raise HTTPException(
                status_code=404, detail="运行租约未启用"
            )
        public_thread = check_thread_id(thread_id)
        released = deps.run_leases.force_release(
            f"{user.name}:{public_thread}"
        )
        return {"released": bool(released)}

    @app.post("/api/ops/maintenance/purge")
    async def purge_maintenance(user: UserDep) -> dict:
        """清理过期租约与幂等键。"""
        deps.rate_limit(user)
        leases = (
            deps.run_leases.purge_expired()
            if deps.run_leases is not None
            else 0
        )
        keys = (
            deps.idempotency.purge_expired()
            if deps.idempotency is not None
            else 0
        )
        return {"leases": int(leases), "idempotency": int(keys)}

    @app.get("/api/audit")
    async def audit(
        user: UserDep,
        thread_id: str = "",
        limit: int = 100,
    ) -> dict:
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            data = await deps.agent_json(
                user, tenant, None, "GET", "/v1/audit",
                params={
                    "thread_id": (
                        check_thread_id(thread_id) if thread_id else ""
                    ),
                    "limit": max(1, limit),
                },
            )
            if deps.control_store is not None:
                try:
                    await asyncio.to_thread(
                        deps.control_store.index_audit,
                        user.name,
                        list(data.get("records", [])),
                    )
                except Exception:  # noqa: BLE001 - 索引失败不影响查询
                    _LOG.warning("审计索引写入失败", exc_info=True)
            return data
        runtime = deps.ops_runtime(tenant, None)
        try:
            internal = (
                tenant.thread_id(check_thread_id(thread_id))
                if thread_id
                else None
            )
            records = read_records_many(
                runtime.audit_files,
                thread_id=internal,
                limit=max(1, limit),
            )
        finally:
            await runtime.aclose()
        return {
            "records": [
                record.model_dump(mode="json") for record in records
            ]
        }
