"""上下文压缩并分叉路由。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException

from coding_agent.identity.tenancy import WebTenant
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import UserDep
from coding_agent.web.models import CompactRequest, SessionConfig
from coding_agent.web.validation import check_thread_id


@dataclass(frozen=True)
class CompactDeps:
    config_and_tenant: Callable[[AuthUser, str], tuple[WebTenant, Any]]
    reject_active: Callable[[AuthUser, str], None]
    agent_client: Any
    agent_json: Callable[..., Awaitable[dict[str, Any]]]
    set_config: Callable[[AuthUser, WebTenant, Any], None]
    effective_access: Callable[[Any], str | None]
    ops_runtime: Callable[[WebTenant, Any], Any]


def register_compact_routes(app: FastAPI, deps: CompactDeps) -> None:
    @app.post("/api/context/{thread_id}/compact")
    async def compact_context(
        thread_id: str,
        request: CompactRequest,
        user: UserDep,
    ) -> dict:
        """压缩并分叉；原 thread 保留，不原地删消息。"""
        public_thread = check_thread_id(thread_id)
        deps.reject_active(user, public_thread)
        tenant, config = deps.config_and_tenant(user, public_thread)
        if deps.agent_client is not None:
            result = await deps.agent_json(
                user, tenant, config, "POST",
                f"/v1/compact/{public_thread}",
                json_body={"keep_recent": request.keep_recent},
            )
            target_public = str(result.get("thread_id") or "")
            if target_public:
                deps.set_config(
                    user,
                    tenant,
                    SessionConfig(
                        thread_id=target_public,
                        workspace=(
                            config.workspace
                            if config
                            else tenant.workspace
                        ),
                        workspace_id=config.workspace_id if config else "",
                        access_mode=deps.effective_access(config),
                        model=config.model if config else "",
                        provider=config.provider if config else "",
                    ),
                )
            return {
                "thread_id": target_public,
                "source_thread_id": public_thread,
            }
        runtime = deps.ops_runtime(tenant, config)
        try:
            target_internal = await runtime.compact_thread(
                tenant.thread_id(public_thread),
                keep_recent=request.keep_recent,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=409,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        target_public = tenant.public_thread(target_internal)
        deps.set_config(
            user,
            tenant,
            SessionConfig(
                thread_id=target_public,
                workspace=config.workspace if config else tenant.workspace,
                workspace_id=config.workspace_id if config else "",
                access_mode=deps.effective_access(config),
                model=config.model if config else "",
                provider=config.provider if config else "",
            ),
        )
        return {"thread_id": target_public, "source_thread_id": public_thread}
