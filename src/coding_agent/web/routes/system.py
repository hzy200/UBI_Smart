"""系统/能力路由：healthz、health、workspace、capabilities。"""

from __future__ import annotations

import hmac
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from coding_agent import __version__
from coding_agent.config import Settings
from coding_agent.tenancy import WebTenant
from coding_agent.web.auth import AuthUser
from coding_agent.web.client_errors import append_client_error
from coding_agent.web.dependencies import UserDep
from coding_agent.web.models import ClientErrorReport
from coding_agent.web.validation import check_thread_id
from coding_agent.web.version import (
    CAPABILITIES_VERSION,
    MIN_API_VERSION,
    MIN_FRONTEND_VERSION,
    WEB_API_VERSION,
)

_LOG = logging.getLogger(__name__)


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


def _readyz(app: FastAPI) -> Any:
    """`/readyz` 的实现：库真的能查才算就绪。"""
    db_state = str(
        getattr(app.state, "control_db_status", {"state": "disabled"}).get(
            "state", "disabled"
        )
    )
    agent_state = str(
        getattr(app.state, "agent_status", {"state": "unknown"}).get(
            "state", "unknown"
        )
    )
    checks: dict[str, str] = {"control_db": db_state, "agent": agent_state}

    store = getattr(app.state, "control_store", None)
    if store is not None:
        # 握手只在启动时做一次；探针再问一次库，才能发现"启动后挂了"。
        try:
            import sqlalchemy as sa

            with store.engine.connect() as conn:
                conn.execute(sa.text("SELECT 1"))
            checks["control_db"] = "ok"
        except Exception as exc:  # noqa: BLE001 - 连不上就是没就绪
            checks["control_db"] = f"unreachable:{type(exc).__name__}"
            _LOG.warning("控制面数据库不可达：%s", exc)

    ready = checks["control_db"] in {"ok", "disabled"} and (
        agent_state in {"ok", "in_process", "unknown"}
    )
    payload = {"status": "ready" if ready else "not_ready", "checks": checks}
    if not ready:
        return JSONResponse(status_code=503, content=payload)
    return payload


def _metrics(deps: SystemDeps, request: Request) -> PlainTextResponse:
    """`/metrics` 的实现：先认证，再刷新需要现算的 gauge。"""
    from coding_agent.observability import render, set_gauge

    token = str(deps.settings.metrics_token or "")
    if token:
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        if not hmac.compare_digest(supplied, expected):
            raise HTTPException(
                status_code=401,
                detail="metrics token 无效",
                headers={"WWW-Authenticate": "Bearer"},
            )
        tenant = str(deps.settings.web_account_tenant or "default")
    else:
        config = request.app.state.auth
        user = config.user_from_request(request)
        tenant = user.tenant
    store = getattr(request.app.state, "approval_store", None)
    if store is not None:
        try:
            pending = len(store.pending(tenant, limit=1000))
            set_gauge(
                "agent_approvals_pending",
                pending,
                {"tenant": tenant},
            )
        except Exception:  # noqa: BLE001 - 指标失败不阻断抓取
            _LOG.warning("审批待办指标刷新失败", exc_info=True)
    return PlainTextResponse(
        render(), media_type="text/plain; version=0.0.4; charset=utf-8"
    )


def register_system_routes(app: FastAPI, deps: SystemDeps) -> None:
    @app.post("/api/client-errors")
    async def client_errors(
        payload: ClientErrorReport,
        user: UserDep,
    ) -> dict:
        """接收前端错误摘要；默认由客户端设置决定是否发送。"""
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "user": user.name,
            "kind": payload.kind,
            "message": payload.message,
            "stack": payload.stack,
            "url": payload.url,
            "version": payload.version,
            "locale": payload.locale,
            "client_at": payload.at,
        }
        append_client_error(
            Path(deps.settings.resolved_audit_dir) / "client-errors.jsonl",
            record,
        )
        return {"ok": True}

    @app.get("/healthz")
    async def healthz() -> dict:
        """公开存活探针：不暴露模型、Key、工作区等配置。"""
        return {
            "status": "ok",
            "version": __version__,
            "api_version": WEB_API_VERSION,
            "min_api_version": MIN_API_VERSION,
            "min_frontend_version": MIN_FRONTEND_VERSION,
            "capabilities_version": CAPABILITIES_VERSION,
            "agent_state": getattr(
                app.state, "agent_status", {"state": "unknown"}
            ).get("state", "unknown"),
            "db_state": getattr(
                app.state, "control_db_status", {"state": "disabled"}
            ).get("state", "disabled"),
        }

    @app.get("/readyz")
    async def readyz() -> Any:
        """就绪探针：控制面库或 A3 不可用时返回 503。

        与 `/healthz` 的分工：healthz 说明"进程活着"，readyz 说明"能干活"。
        只配 healthz 的反代/编排会把依赖全挂的实例继续放进流量。
        """
        return _readyz(app)

    @app.get("/metrics")
    async def metrics(request: Request) -> PlainTextResponse:
        """Prometheus 文本导出。

        配置 `AGENT_METRICS_TOKEN` 时用独立 Bearer token（供抓取器）；
        否则复用普通 Web 认证。指标不含提示词/路径/线程 id。
        """
        return _metrics(deps, request)

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
            "oidc_enabled": bool(deps.settings.oidc_enabled),
            "mfa_enabled": bool(deps.settings.mfa_enabled),
            "four_eyes": bool(deps.settings.approval_four_eyes),
            "four_eyes_min_level": (
                int(deps.settings.approval_four_eyes_min_level or 0)
                if deps.settings.approval_four_eyes
                else 0
            ),
            **deps.capabilities(config),
        }
