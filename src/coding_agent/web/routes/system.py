"""系统/能力路由：healthz、health、workspace、capabilities。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

from coding_agent import __version__
from coding_agent.config import Settings
from coding_agent.tenancy import WebTenant
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import UserDep
from coding_agent.web.validation import check_thread_id
from coding_agent.web.version import (
    CAPABILITIES_VERSION,
    MIN_FRONTEND_VERSION,
    WEB_API_VERSION,
)


@dataclass(frozen=True)
class SystemDeps:
    settings: Settings
    tenants: Any
    tenant: Callable[[AuthUser], WebTenant]
    tenant_store: Callable[[WebTenant], Any]
    provider_rows: Callable[[Settings, Any], list[dict[str, Any]]]
    config_for: Callable[..., Any]
    capabilities: Callable[[Any], dict[str, Any]]
    default_access: str | None
    desktop_mode: bool


def register_system_routes(app: FastAPI, deps: SystemDeps) -> None:
    @app.get("/healthz")
    async def healthz() -> dict:
        """公开存活探针：不暴露模型、Key、工作区等配置。"""
        return {
            "status": "ok",
            "version": __version__,
            "api_version": WEB_API_VERSION,
            "min_frontend_version": MIN_FRONTEND_VERSION,
            "capabilities_version": CAPABILITIES_VERSION,
            "agent_state": getattr(
                app.state, "agent_status", {"state": "unknown"}
            ).get("state", "unknown"),
            "db_state": getattr(
                app.state, "control_db_status", {"state": "disabled"}
            ).get("state", "disabled"),
        }

    @app.get("/api/health")
    async def health(user: UserDep) -> dict:
        """认证后的联通测试：不调用模型，只报配置是否齐备。"""
        from coding_agent.llm.deepseek import MissingApiKeyError, build_llm

        try:
            tenant = deps.tenants.for_user(user.name)
        except Exception as exc:  # noqa: BLE001 - 沙箱不可用也要可读报出
            return {
                "model": deps.settings.deepseek_model,
                "endpoint": deps.settings.deepseek_base_url,
                "tracing": deps.settings.langsmith_tracing,
                "workspace": None,
                "api_key": bool(deps.settings.deepseek_api_key),
                "reachable": bool(deps.settings.deepseek_api_key),
                "error": f"{type(exc).__name__}: {exc}",
                "user": user.name,
            }
        store = deps.tenant_store(tenant)
        rows = deps.provider_rows(tenant.settings, store)
        payload = {
            "model": tenant.settings.deepseek_model,
            "endpoint": tenant.settings.deepseek_base_url,
            "tracing": tenant.settings.langsmith_tracing,
            "workspace": tenant.workspace,
            "api_key": any(row["has_key"] for row in rows),
            "reachable": False,
            "error": "",
            "user": user.name,
        }
        try:
            build_llm(
                tenant.settings, streaming=False, secret_store=store
            )
            payload["reachable"] = True
        except Exception as exc:  # noqa: BLE001 - 诊断端点不能 500
            payload["error"] = (
                f"{type(exc).__name__}: {exc}"
                if not isinstance(exc, MissingApiKeyError)
                else str(exc)
            )
        return payload

    @app.get("/api/workspace")
    async def workspace(user: UserDep) -> dict:
        tenant = deps.tenant(user)
        return {"user": user.name, "workspace": tenant.workspace}

    @app.get("/api/capabilities")
    async def capabilities(
        user: UserDep,
        thread_id: str = "",
    ) -> dict:
        tenant = deps.tenant(user)
        config = None
        if thread_id:
            public_thread = check_thread_id(thread_id)
            config = deps.config_for(
                user, tenant, public_thread, create=False
            )
        return {
            "default_access_mode": deps.default_access or "legacy",
            "desktop_mode": deps.desktop_mode,
            "capabilities_version": CAPABILITIES_VERSION,
            "agent": getattr(
                app.state, "agent_status", {"state": "unknown"}
            ),
            "db": getattr(
                app.state, "control_db_status", {"state": "disabled"}
            ),
            **deps.capabilities(config),
        }
