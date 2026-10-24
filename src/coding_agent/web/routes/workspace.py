"""工作区只读路由：项目探测、文件树、文件内容、Git 面板。"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException

from coding_agent.access import ACCESS_READ_ONLY
from coding_agent.agent_protocol import AgentContext
from coding_agent.project_env import probe_project
from coding_agent.tenancy import TenantError, WebTenant
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import UserDep
from coding_agent.web.validation import check_thread_id
from coding_agent.workspace.browse import (
    BrowseError,
    git_summary,
    list_directory,
    read_text_file,
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
