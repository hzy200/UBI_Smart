"""工作区只读路由：项目探测、文件树、文件内容、Git 面板。"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response

from ubismart.agent_protocol import AgentContext
from ubismart.execution.file_access import LocalFileAccess
from ubismart.identity.access import ACCESS_READ_ONLY
from ubismart.identity.project_env import probe_project
from ubismart.identity.tenancy import TenantError, WebTenant
from ubismart.web.auth import AuthUser
from ubismart.web.dependencies import UserDep
from ubismart.web.validation import check_thread_id
from ubismart.workspace.browse import (
    BrowseError,
    git_summary,
    list_directory,
    read_text_file,
    search_workspace,
)
from ubismart.workspace.file_preview import (
    INLINE_MIME_ALLOWLIST,
    RAW_MAX_BYTES,
    PreviewError,
    document_limits,
    raw_response_parts,
    read_document,
    read_raw,
)


@dataclass(frozen=True)
class WorkspaceDeps:
    tenant: Callable[[AuthUser], WebTenant]
    rate_limit: Callable[[AuthUser], None]
    resolve_workspace: Callable[[str, str], str]
    reject_deleted: Callable[[AuthUser, str], None]
    config_for: Callable[..., Any]
    agent_json: Callable[..., Awaitable[dict[str, Any]]]
    agent_failure: Callable[[Exception], HTTPException]
    agent_client: Any
    default_access: str | None
    allow_write: bool
    desktop_mode: bool
    settings: Any
    agent_raw: Any


def register_workspace_routes(app: FastAPI, deps: WorkspaceDeps) -> None:
    @app.get("/api/project")
    async def project(user: UserDep, workspace: str = "") -> dict:
        """只读探测项目解释器/包管理器；不创建环境、不执行项目代码。"""
        deps.rate_limit(user)
        tenant = deps.tenant(user)
        try:
            target = deps.resolve_workspace(
                user.name, workspace or tenant.workspace
            )
        except (TenantError, ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if deps.agent_client is not None:
            context = AgentContext(
                tenant=tenant.user,
                user=user.name,
                workspace=target,
                access_mode=deps.default_access or ACCESS_READ_ONLY,
                allow_write=deps.allow_write,
            )
            try:
                return await deps.agent_client.json_request(
                    context, "GET", "/v1/project"
                )
            except Exception as exc:  # noqa: BLE001
                raise deps.agent_failure(exc) from exc
        result = await asyncio.to_thread(probe_project, target)
        result["desktop_mode"] = deps.desktop_mode
        return result

    @app.get("/api/files")
    async def files(
        user: UserDep,
        thread_id: str,
        path: str = "",
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        deps.reject_deleted(user, public_thread)
        tenant = deps.tenant(user)
        config = deps.config_for(
            user, tenant, public_thread, create=False
        )
        if deps.agent_client is not None:
            return await deps.agent_json(
                user, tenant, config, "GET", "/v1/files",
                params={"path": path},
            )
        workspace = config.workspace if config else tenant.workspace
        try:
            return await asyncio.to_thread(
                list_directory, workspace, path
            )
        except BrowseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/file")
    async def file_content(
        user: UserDep,
        thread_id: str,
        path: str,
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        deps.reject_deleted(user, public_thread)
        tenant = deps.tenant(user)
        config = deps.config_for(
            user, tenant, public_thread, create=False
        )
        if deps.agent_client is not None:
            return await deps.agent_json(
                user, tenant, config, "GET", "/v1/file",
                params={"path": path},
            )
        workspace = config.workspace if config else tenant.workspace
        try:
            return await asyncio.to_thread(
                read_text_file, workspace, path
            )
        except BrowseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/api/file/raw")
    async def file_raw(
        user: UserDep,
        thread_id: str,
        path: str,
        request: Request,
    ) -> Response:
        public_thread = check_thread_id(thread_id)
        deps.reject_deleted(user, public_thread)
        tenant = deps.tenant(user)
        config = deps.config_for(
            user, tenant, public_thread, create=False
        )
        range_header = request.headers.get("range", "")
        if deps.agent_client is not None and deps.agent_raw is not None:
            status, headers, body = await deps.agent_raw(
                user,
                tenant,
                config,
                "/v1/file/raw",
                params={"path": path},
                range_header=range_header,
                if_none_match=request.headers.get("if-none-match", ""),
            )
            allowed = {
                "content-range",
                "content-disposition",
                "accept-ranges",
                "cache-control",
                "content-length",
                "x-content-type-options",
            }
            outgoing = {
                key: value
                for key, value in headers.items()
                if key.lower() in allowed
            }
            return Response(
                content=body,
                status_code=status,
                media_type=headers.get("content-type", "application/octet-stream"),
                headers=outgoing,
            )
        workspace = config.workspace if config else tenant.workspace
        access = LocalFileAccess(workspace)
        try:
            raw = await asyncio.to_thread(
                read_raw,
                access,
                path,
                max_bytes=min(
                    RAW_MAX_BYTES,
                    int(getattr(deps.settings, "document_max_bytes", RAW_MAX_BYTES)),
                ),
                allow_mime=INLINE_MIME_ALLOWLIST,
            )
        except PreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        status, headers, body = raw_response_parts(
            raw, range_header, request.headers.get("if-none-match", "")
        )
        return Response(
            content=body,
            status_code=status,
            media_type=raw.mime,
            headers=headers,
        )

    @app.get("/api/file/document")
    async def file_document(
        user: UserDep,
        thread_id: str,
        path: str,
        include_images: bool = False,
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        deps.reject_deleted(user, public_thread)
        tenant = deps.tenant(user)
        config = deps.config_for(
            user, tenant, public_thread, create=False
        )
        if deps.agent_client is not None:
            return await deps.agent_json(
                user, tenant, config, "GET", "/v1/file/document",
                params={
                    "path": path,
                    "include_images": "true" if include_images else "false",
                },
            )
        workspace = config.workspace if config else tenant.workspace
        limits = document_limits(deps.settings)
        try:
            model = await asyncio.to_thread(
                read_document,
                LocalFileAccess(workspace),
                path,
                limits=limits,
                include_images=include_images,
            )
        except PreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return model.model_dump()

    @app.get("/api/search")
    async def search(
        user: UserDep,
        thread_id: str,
        q: str = "",
        limit: int = 50,
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        deps.reject_deleted(user, public_thread)
        tenant = deps.tenant(user)
        config = deps.config_for(
            user, tenant, public_thread, create=False
        )
        if deps.agent_client is not None:
            return await deps.agent_json(
                user, tenant, config, "GET", "/v1/search",
                params={"q": q, "limit": str(limit)},
            )
        if not q.strip():
            return {"query": "", "results": [], "truncated": False}
        workspace = config.workspace if config else tenant.workspace
        try:
            result = await asyncio.to_thread(
                search_workspace, workspace, q, limit=limit
            )
        except BrowseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        result["content_search"] = True
        return result

    @app.get("/api/git")
    async def git_panel(
        user: UserDep,
        thread_id: str,
        diff: bool = False,
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        deps.reject_deleted(user, public_thread)
        tenant = deps.tenant(user)
        config = deps.config_for(
            user, tenant, public_thread, create=False
        )
        if deps.agent_client is not None:
            return await deps.agent_json(
                user, tenant, config, "GET", "/v1/git",
                params={"diff": "true" if diff else "false"},
            )
        workspace = config.workspace if config else tenant.workspace
        try:
            return await asyncio.to_thread(
                git_summary, workspace, diff=diff
            )
        except (BrowseError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
