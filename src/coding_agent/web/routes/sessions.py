"""会话目录路由：列表、创建、模型切换、改名、删除、导出。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from fastapi import FastAPI, HTTPException

from coding_agent.access import (
    ACCESS_READ_ONLY,
    ACCESS_WRITE_AUTO,
    clamp_access_mode,
    normalize_access_mode,
)
from coding_agent.memory.sessions import SessionIndex
from coding_agent.tenancy import TenantError, WebTenant
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import UserDep
from coding_agent.web.models import (
    ModelRequest,
    RenameRequest,
    SessionConfig,
    SessionImportRequest,
    SessionMetaRequest,
    SessionRequest,
)
from coding_agent.web.transcripts import (
    clean_transcript,
    load_transcript,
    save_transcript,
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
    # 管理员为账号设置的访问上限；返回 read_only/write_ask/write_auto。
    access_limit: Callable[[AuthUser], str] = lambda _user: ACCESS_WRITE_AUTO


def _row(config: Any, tenant: WebTenant, deps: SessionDeps, **extra: Any) -> dict:
    return {
        "thread_id": config.thread_id,
        "title": config.title or "（未运行）",
        "workspace": config.workspace,
        "workspace_id": config.workspace_id,
        "access_mode": deps.effective_access(config),
        "model": config.model,
        "provider": config.provider,
        "pinned": bool(config.pinned),
        "archived": bool(config.archived),
        "tags": list(config.tags),
        "unread": bool(config.unread),
        "last_message": config.last_message,
        **extra,
    }


def register_session_routes(app: FastAPI, deps: SessionDeps) -> None:
    @app.get("/api/sessions")
    async def sessions(
        user: UserDep,
        limit: int = 20,
        q: str = "",
        include_archived: bool = False,
    ) -> dict:
        await deps.sweep_expired()
        tenant = deps.tenant(user)
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()

        if deps.agent_client is not None:
            await asyncio.to_thread(deps.load_configs, user, tenant)
            data = await deps.agent_json(
                user, tenant, None, "GET", "/v1/sessions",
                params={"limit": 500, "q": q},
            )
            for item in data.get("sessions", []):
                thread = str(item.get("thread_id") or "")
                if not thread or (user.name, thread) in deps.deleted_sessions:
                    continue
                seen.add(thread)
                config = await asyncio.to_thread(
                    deps.config_for, user, tenant, thread, create=False
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
                        "pinned": bool(config.pinned) if config else False,
                        "archived": bool(config.archived) if config else False,
                        "tags": list(config.tags) if config else [],
                        "unread": bool(config.unread) if config else False,
                        "last_message": (
                            config.last_message
                            if config
                            else str(item.get("last_message") or "")
                        ),
                    }
                )
        else:
            index = SessionIndex(tenant.settings.resolved_audit_dir)
            for info in index.list(limit=0):
                public_thread = tenant.public_thread(info.thread_id)
                if (user.name, public_thread) in deps.deleted_sessions:
                    continue
                seen.add(public_thread)
                config = await asyncio.to_thread(
                    deps.config_for, user, tenant, public_thread, create=False
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
                        "pinned": bool(config.pinned) if config else False,
                        "archived": bool(config.archived) if config else False,
                        "tags": list(config.tags) if config else [],
                        "unread": bool(config.unread) if config else False,
                        "last_message": config.last_message if config else "",
                    }
                )
            await asyncio.to_thread(deps.load_configs, user, tenant)

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

        if not include_archived:
            rows = [row for row in rows if not row.get("archived")]
        query = q.strip().lower()
        if query:
            rows = [
                row
                for row in rows
                if query in str(row.get("title", "")).lower()
                or query in row["thread_id"].lower()
                or query in str(row.get("workspace", "")).lower()
                or query in str(row.get("last_message", "")).lower()
                or query in " ".join(row.get("tags") or []).lower()
            ]
        # 稳定排序：置顶优先，其余保持最近活跃顺序。
        rows.sort(key=lambda row: 0 if row.get("pinned") else 1)
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
        await asyncio.to_thread(deps.reject_deleted, user, public_thread)
        existing = await asyncio.to_thread(
            deps.config_for,
            user,
            deps.tenant(user),
            public_thread,
            create=False,
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
        workspace_kind = "sandbox"
        a2l_id = ""
        root_handle = ""
        if request.workspace_id and deps.registry_enabled:
            row = deps.bind_workspace(user, request.workspace_id)
            workspace = str(row["root_path"])
            workspace_id = str(row["workspace_id"])
            workspace_kind = str(row.get("kind") or "sandbox")
            a2l_id = str(row.get("a2l_id") or "")
            root_handle = str(row.get("root_handle") or "")
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
        access_mode = clamp_access_mode(
            normalize_access_mode(request.access_mode) or ACCESS_READ_ONLY,
            deps.access_limit(user),
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
            workspace_kind=workspace_kind,
            a2l_id=a2l_id,
            root_handle=root_handle,
            access_mode=access_mode,
            model=request.model,
            provider=request.provider,
        )
        await asyncio.to_thread(deps.set_config, user, tenant, config)
        await asyncio.to_thread(deps.persist_configs, user, tenant)
        return {
            "thread_id": public_thread,
            "workspace": workspace,
            "workspace_id": workspace_id,
            "access_mode": access_mode,
            "model": config.model,
            "provider": config.provider,
            "capabilities": deps.capabilities(config),
        }

    @app.post("/api/sessions/import")
    async def import_session(
        request: SessionImportRequest,
        user: UserDep,
    ) -> dict:
        """把导出的 JSON 恢复到新会话；有 transcript 时连工具卡片一起恢复。"""
        deps.reject_db_degraded()
        source = request.transcript or request.messages
        messages = clean_transcript(source)
        if not messages:
            raise HTTPException(status_code=400, detail="导入文件没有可用的消息")
        created = await create_session(
            SessionRequest(
                thread_id="",
                workspace_id=request.workspace_id,
                workspace=request.workspace,
                access_mode=request.access_mode,
                model=request.model,
                provider=request.provider,
            ),
            user,
        )
        public_thread = created["thread_id"]
        tenant = deps.tenant(user)
        try:
            save_transcript(tenant, public_thread, messages)
        except OSError as exc:
            with deps.registry_lock:
                deps.session_configs.pop((user.name, public_thread), None)
            await asyncio.to_thread(deps.persist_configs, user, tenant)
            raise HTTPException(
                status_code=503, detail=f"保存导入轨迹失败：{exc}"
            ) from exc

        config = await asyncio.to_thread(
            deps.config_for, user, tenant, public_thread, create=False
        )
        title = " ".join(request.title.split())[:120]
        if not title:
            title = next(
                (
                    " ".join(str(item.get("text") or "").split())[:120]
                    for item in messages
                    if item.get("role") == "user" and item.get("text")
                ),
                "",
            )
        preview = ""
        for item in reversed(messages):
            if item.get("kind") == "tool":
                continue
            text = " ".join(str(item.get("text") or "").split())
            if text:
                preview = text[:160]
                break
        if config is not None:
            if title:
                config.title = title
            config.last_message = preview
            config.unread = False
            await asyncio.to_thread(deps.set_config, user, tenant, config)
            await asyncio.to_thread(deps.persist_configs, user, tenant)
        return {
            "ok": True,
            "thread_id": public_thread,
            "title": config.title if config else title,
            "imported": len(messages),
            "capabilities": created.get("capabilities", {}),
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
        config = await asyncio.to_thread(
            deps.config_for, user, tenant, public_thread, create=False
        )
        if config is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        deps.validate_provider(tenant, user, request.provider)
        config.model = request.model
        config.provider = request.provider
        await asyncio.to_thread(deps.set_config, user, tenant, config)
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
        await asyncio.to_thread(deps.reject_deleted, user, public_thread)
        tenant = deps.tenant(user)
        config = await asyncio.to_thread(
            deps.config_for, user, tenant, public_thread, create=False
        )
        if config is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        config.title = " ".join(request.title.split())[:120]
        await asyncio.to_thread(deps.set_config, user, tenant, config)
        return {
            "ok": True,
            "thread_id": public_thread,
            "title": config.title,
        }

    @app.post("/api/sessions/{thread_id}/meta")
    async def update_session_meta(
        thread_id: str,
        request: SessionMetaRequest,
        user: UserDep,
    ) -> dict:
        """更新会话展示元数据：置顶、归档、标签、未读。"""
        deps.reject_db_degraded()
        public_thread = check_thread_id(thread_id)
        await asyncio.to_thread(deps.reject_deleted, user, public_thread)
        tenant = deps.tenant(user)
        config = await asyncio.to_thread(
            deps.config_for, user, tenant, public_thread, create=False
        )
        if config is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if request.pinned is not None:
            config.pinned = bool(request.pinned)
        if request.archived is not None:
            config.archived = bool(request.archived)
        if request.unread is not None:
            config.unread = bool(request.unread)
        if request.tags is not None:
            tags: list[str] = []
            for raw in request.tags:
                clean = " ".join(str(raw or "").split())[:24]
                if clean and clean not in tags:
                    tags.append(clean)
            config.tags = tags[:8]
        await asyncio.to_thread(deps.set_config, user, tenant, config)
        return {
            "ok": True,
            "thread_id": public_thread,
            "pinned": bool(config.pinned),
            "archived": bool(config.archived),
            "tags": list(config.tags),
            "unread": bool(config.unread),
            "last_message": config.last_message,
        }

    @app.delete("/api/sessions/{thread_id}")
    async def delete_session(thread_id: str, user: UserDep) -> dict:
        """Tombstone 删除：列表隐藏并删除会话配置，审计/checkpoint 保留。"""
        deps.reject_db_degraded()
        public_thread = check_thread_id(thread_id)
        deps.reject_active(user, public_thread)
        tenant = deps.tenant(user)
        await asyncio.to_thread(deps.load_configs, user, tenant)
        with deps.registry_lock:
            deps.deleted_sessions.add((user.name, public_thread))
            deps.session_configs.pop((user.name, public_thread), None)
        await asyncio.to_thread(deps.persist_configs, user, tenant)
        return {"ok": True, "thread_id": public_thread}

    @app.get("/api/sessions/{thread_id}/export")
    async def export_session(
        thread_id: str,
        user: UserDep,
        format: Literal["markdown", "json"] = "markdown",
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        await asyncio.to_thread(deps.reject_deleted, user, public_thread)
        tenant = deps.tenant(user)
        config = await asyncio.to_thread(
            deps.config_for, user, tenant, public_thread, create=False
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
            transcript, _ = load_transcript(tenant, public_thread)
            return {
                "format": "json",
                "version": 1,
                "thread_id": public_thread,
                "title": title,
                "workspace": config.workspace if config else tenant.workspace,
                "messages": messages,
                "transcript": transcript,
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
