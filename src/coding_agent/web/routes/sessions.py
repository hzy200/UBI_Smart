"""会话目录路由：列表、创建、模型切换、改名、删除、导出。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI, HTTPException

from coding_agent.access import ACCESS_READ_ONLY, normalize_access_mode
from coding_agent.memory.sessions import SessionIndex
from coding_agent.tenancy import TenantError, WebTenant
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import UserDep
from coding_agent.web.models import (
    ModelRequest,
    RenameRequest,
    SessionConfig,
    SessionRequest,
)
from coding_agent.web.validation import check_thread_id


@dataclass(frozen=True)
class SessionDeps:
    tenant: Callable[[AuthUser], WebTenant]
    sweep_expired: Callable[[], Awaitable[None]]
    load_configs: Callable[[AuthUser, WebTenant], None]
    config_for: Callable[..., Any]
    set_config: Callable[[AuthUser, WebTenant, Any], None]
    persist_configs: Callable[[AuthUser, WebTenant], None]
    effective_access: Callable[[Any], str | None]
    capabilities: Callable[[Any], dict[str, Any]]
    reject_deleted: Callable[[AuthUser, str], None]
    reject_active: Callable[[AuthUser, str], None]
    reject_db_degraded: Callable[[], None]
    rate_limit: Callable[[AuthUser], None]
    resolve_workspace: Callable[[str, str], str]
    # RFC-0001：注册表开启时按 workspace_id 绑定；关闭时保留裸路径语义。
    bind_workspace: Callable[[AuthUser, str], dict[str, Any]]
    registry_enabled: bool
    validate_provider: Callable[[WebTenant, AuthUser, str], None]
    agent_json: Callable[..., Awaitable[dict[str, Any]]]
    ops_runtime: Callable[[WebTenant, Any], Any]
    agent_client: Any
    allow_write: bool
    registry_lock: Any
    session_configs: dict[tuple[str, str], Any]
    deleted_sessions: set[tuple[str, str]]
    runtimes: Any


def _row(config: Any, tenant: WebTenant, deps: SessionDeps, **extra: Any) -> dict:
    return {
        "thread_id": config.thread_id,
        "title": config.title or "（未运行）",
        "workspace": config.workspace,
        "workspace_id": config.workspace_id,
        "access_mode": deps.effective_access(config),
        "model": config.model,
        "provider": config.provider,
        **extra,
    }


def register_session_routes(app: FastAPI, deps: SessionDeps) -> None:
    @app.get("/api/sessions")
    async def sessions(
        user: UserDep,
        limit: int = 20,
        q: str = "",
    ) -> dict:
        await deps.sweep_expired()
        tenant = deps.tenant(user)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()

        if deps.agent_client is not None:
            deps.load_configs(user, tenant)
            data = await deps.agent_json(
                user, tenant, None, "GET", "/v1/sessions",
                params={"limit": 500, "q": q},
            )
            for item in data.get("sessions", []):
                thread = str(item.get("thread_id") or "")
                if not thread or (user.name, thread) in deps.deleted_sessions:
                    continue
                seen.add(thread)
                config = deps.config_for(
                    user, tenant, thread, create=False
                )
                rows.append(
                    {
                        "thread_id": thread,
                        "last_active": item.get("last_active", ""),
                        "prompts": item.get("prompts", 0),
                        "tool_calls": item.get("tool_calls", 0),
                        "title": (
                            (config.title if config else "")
                            or item.get("title", "")
                        ),
                        "workspace": (
                            config.workspace
                            if config
                            else item.get("workspace", "")
                        ),
                        "workspace_id": (
                            config.workspace_id if config else ""
                        ),
                        "access_mode": deps.effective_access(config),
                        "model": config.model if config else "",
                        "provider": config.provider if config else "",
                        "run_count": item.get("runs", 0),
                        "interrupted": bool(item.get("interrupted")),
                    }
                )
        else:
            index = SessionIndex(tenant.settings.resolved_audit_dir)
            for info in index.list(limit=0):
                public_thread = tenant.public_thread(info.thread_id)
                if (user.name, public_thread) in deps.deleted_sessions:
                    continue
                seen.add(public_thread)
                config = deps.config_for(
                    user, tenant, public_thread, create=False
                )
                rows.append(
                    {
                        "thread_id": public_thread,
                        "last_active": info.last_seen,
                        "prompts": info.prompts,
                        "tool_calls": info.tool_calls,
                        "title": (config.title if config else "") or info.title,
                        "workspace": (
                            config.workspace if config else info.workspace
                        ),
                        "workspace_id": (
                            config.workspace_id if config else ""
                        ),
                        "access_mode": deps.effective_access(config),
                        "model": config.model if config else "",
                        "provider": config.provider if config else "",
                        "run_count": info.runs,
                        "interrupted": bool(info.interrupted),
                    }
                )
            deps.load_configs(user, tenant)

        with deps.registry_lock:
            pending = [
                config
                for (owner, _), config in deps.session_configs.items()
                if owner == user.name
                and config.thread_id not in seen
                and (owner, config.thread_id) not in deps.deleted_sessions
            ]
        for config in pending:
            rows.append(
                _row(
                    config,
                    tenant,
                    deps,
                    last_active="",
                    prompts=0,
                    tool_calls=0,
                    run_count=0,
                    interrupted=False,
                )
            )

        query = q.strip().lower()
        if query:
            rows = [
                row
                for row in rows
                if query in str(row.get("title", "")).lower()
                or query in row["thread_id"].lower()
                or query in str(row.get("workspace", "")).lower()
            ]
        return {"sessions": rows[: max(1, limit)]}

    @app.post("/api/sessions")
    async def create_session(
        request: SessionRequest,
        user: UserDep,
    ) -> dict:
        """绑定会话的 workspace / 权限 / 模型；这些字段不能由单次 run 改写。"""
        deps.rate_limit(user)
        deps.reject_db_degraded()
        public_thread = check_thread_id(request.thread_id)
        key = (user.name, public_thread)
        with deps.registry_lock:
            if deps.runtimes.contains(key):
                raise HTTPException(
                    status_code=409,
                    detail="该会话正在运行或已挂起；请先结束/恢复",
                )
        deps.reject_deleted(user, public_thread)
        existing = deps.config_for(
            user, deps.tenant(user), public_thread, create=False
        )
        if existing is not None:
            if (
                deps.registry_enabled
                and request.workspace_id
                and existing.workspace_id
                and request.workspace_id != existing.workspace_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail="会话已绑定其他工作区，不能改绑",
                )
            return {
                "thread_id": public_thread,
                "workspace": existing.workspace,
                "workspace_id": existing.workspace_id,
                "access_mode": deps.effective_access(existing),
                "model": existing.model,
                "provider": existing.provider,
                "capabilities": deps.capabilities(existing),
            }

        tenant = deps.tenant(user)
        workspace_id = ""
        if request.workspace_id and deps.registry_enabled:
            row = deps.bind_workspace(user, request.workspace_id)
            workspace = str(row["root_path"])
            workspace_id = str(row["workspace_id"])
        elif deps.registry_enabled:
            raise HTTPException(
                status_code=400,
                detail="已启用工作区注册表，请使用 workspace_id 创建会话",
            )
        else:
            try:
                workspace = deps.resolve_workspace(
                    user.name, request.workspace
                )
            except (TenantError, ValueError, OSError) as exc:
                raise HTTPException(
                    status_code=400, detail=str(exc)
                ) from exc
        access_mode = (
            normalize_access_mode(request.access_mode) or ACCESS_READ_ONLY
        )
        if access_mode != ACCESS_READ_ONLY and not deps.allow_write:
            raise HTTPException(
                status_code=403,
                detail="服务端未开启写能力；请以 --write 启动或选择只读模式",
            )
        deps.validate_provider(tenant, user, request.provider)
        config = SessionConfig(
            thread_id=public_thread,
            workspace=workspace,
            workspace_id=workspace_id,
            access_mode=access_mode,
            model=request.model,
            provider=request.provider,
        )
        deps.set_config(user, tenant, config)
        deps.persist_configs(user, tenant)
        return {
            "thread_id": public_thread,
            "workspace": workspace,
            "workspace_id": workspace_id,
            "access_mode": access_mode,
            "model": config.model,
            "provider": config.provider,
            "capabilities": deps.capabilities(config),
        }

    @app.post("/api/sessions/{thread_id}/model")
    async def switch_model(
        thread_id: str,
        request: ModelRequest,
        user: UserDep,
    ) -> dict:
        """空闲时切换模型；thread/checkpoint 保留，下一次 run 重建 runtime。"""
        deps.reject_db_degraded()
        public_thread = check_thread_id(thread_id)
        deps.reject_active(user, public_thread)
        tenant = deps.tenant(user)
        config = deps.config_for(
            user, tenant, public_thread, create=False
        )
        if config is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        deps.validate_provider(tenant, user, request.provider)
        config.model = request.model
        config.provider = request.provider
        deps.set_config(user, tenant, config)
        return {
            "ok": True,
            "thread_id": public_thread,
            "model": config.model,
            "provider": config.provider,
        }

    @app.post("/api/sessions/{thread_id}/rename")
    async def rename_session(
        thread_id: str,
        request: RenameRequest,
        user: UserDep,
    ) -> dict:
        deps.reject_db_degraded()
        public_thread = check_thread_id(thread_id)
        deps.reject_deleted(user, public_thread)
        tenant = deps.tenant(user)
        config = deps.config_for(
            user, tenant, public_thread, create=False
        )
        if config is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        config.title = " ".join(request.title.split())[:120]
        deps.set_config(user, tenant, config)
        return {
            "ok": True,
            "thread_id": public_thread,
            "title": config.title,
        }

    @app.delete("/api/sessions/{thread_id}")
    async def delete_session(thread_id: str, user: UserDep) -> dict:
        """Tombstone 删除：列表隐藏并删除会话配置，审计/checkpoint 保留。"""
        deps.reject_db_degraded()
        public_thread = check_thread_id(thread_id)
        deps.reject_active(user, public_thread)
        tenant = deps.tenant(user)
        deps.load_configs(user, tenant)
        with deps.registry_lock:
            deps.deleted_sessions.add((user.name, public_thread))
            deps.session_configs.pop((user.name, public_thread), None)
        deps.persist_configs(user, tenant)
        return {"ok": True, "thread_id": public_thread}

    @app.get("/api/sessions/{thread_id}/export")
    async def export_session(
        thread_id: str,
        user: UserDep,
        format: Literal["markdown", "json"] = "markdown",
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        deps.reject_deleted(user, public_thread)
        tenant = deps.tenant(user)
        config = deps.config_for(
            user, tenant, public_thread, create=False
        )
        if deps.agent_client is not None:
            data = await deps.agent_json(
                user, tenant, config, "GET",
                f"/v1/history/{public_thread}",
            )
            messages = [
                {
                    "role": str(item.get("role") or ""),
                    "text": str(item.get("text") or ""),
                }
                for item in data.get("messages", [])
            ]
        else:
            runtime = deps.ops_runtime(tenant, config)
            try:
                history = await runtime.history(
                    tenant.thread_id(public_thread), limit=None
                )
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail=f"{type(exc).__name__}: {exc}",
                ) from exc
            finally:
                await runtime.aclose()
            messages = [
                {"role": message.role, "text": message.text}
                for message in history
            ]
        title = (
            (config.title if config else "")
            or next(
                (
                    item["text"]
                    for item in messages
                    if item["role"] == "user"
                ),
                public_thread,
            )
        ).strip()
        if format == "json":
            return {
                "format": "json",
                "thread_id": public_thread,
                "title": title,
                "workspace": config.workspace if config else tenant.workspace,
                "messages": messages,
            }
        lines = [
            f"# {title}",
            "",
            f"- thread: {public_thread}",
            f"- workspace: {config.workspace if config else tenant.workspace}",
            "",
        ]
        for message in messages:
            role = "用户" if message["role"] == "user" else "助手"
            lines.extend([f"## {role}", "", message["text"], ""])
        return {
            "format": "markdown",
            "thread_id": public_thread,
            "title": title,
            "content": "\n".join(lines).rstrip() + "\n",
        }
