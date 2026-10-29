"""Web 验证界面。

面向公网/多用户的约束：
- 所有 `/api/*`（除公开 `/healthz`）都要求 Bearer Token 或 loopback 匿名；
- runtime 按 `(user, thread)` 隔离，不能跨用户读取/恢复；
- workspace / audit / checkpoint / steering / memory 均按租户隔离；
- 单进程运行：WSL/LLM/worker 配额由进程级 governor 控制；
- 安全头、请求体上限、按用户限流在中间件/依赖层完成。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from coding_agent.access import (
    ACCESS_READ_ONLY,
    ACCESS_WRITE_AUTO,
    clamp_access_mode,
    normalize_access_mode,
)
from coding_agent.config import Settings, apply_security_profile, get_settings
from coding_agent.control.webhooks import schedule_webhooks
from coding_agent.events import (
    ApprovalRequested,
    Event,
    RunFailed,
    RunFinished,
)
from coding_agent.platform.concurrency import ConcurrencyGovernor
from coding_agent.platform.observability import note_silent_failure
from coding_agent.platform.tracing import TracingMiddleware
from coding_agent.protocol.sse_stream import (
    SSE_HEARTBEAT_MS,
    sse_headers,
    with_heartbeat,
)
from coding_agent.tenancy import TenantError, TenantManager, WebTenant
from coding_agent.web.assets import resolve_frontend_dirs
from coding_agent.web.auth import AuthConfig, AuthUser
from coding_agent.web.cors import StrictCORSMiddleware, parse_allowed_origins
from coding_agent.web.errors import (
    is_retryable_status,
    problem_response,
    status_error_code,
)
from coding_agent.web.identity import (
    build_identity_stores as _build_identity_stores,
)
from coding_agent.web.local_runtime import LocalRuntimeRegistry, RuntimeEntry
from coding_agent.web.middleware import (
    ApiVersionRewriteMiddleware,
    AuditorReadOnlyMiddleware,
    BodySizeLimitMiddleware,
    FixedWindowRateLimiter,
    FrontendVersionMiddleware,
    HttpMetricsMiddleware,
    RedisFixedWindowRateLimiter,
    RequestIdMiddleware,
    SecurityHeadersMiddleware,
)
from coding_agent.web.models import (
    SessionConfig,
)
from coding_agent.web.push import PushService, schedule_push
from coding_agent.web.routes.admin import AdminDeps, register_admin_routes
from coding_agent.web.routes.auth import AuthRouteDeps, register_auth_routes
from coding_agent.web.routes.compact import CompactDeps, register_compact_routes
from coding_agent.web.routes.desktop import (
    DesktopDeps,
    register_desktop_routes,
)
from coding_agent.web.routes.notifications import (
    NotificationDeps,
    notification_payload,
    register_notification_routes,
)
from coding_agent.web.routes.ops import OpsDeps, register_ops_routes
from coding_agent.web.routes.providers import ProviderDeps, register_provider_routes
from coding_agent.web.routes.push import PushDeps, register_push_routes
from coding_agent.web.routes.runs import RunDeps, register_run_routes
from coding_agent.web.routes.sessions import (
    SessionDeps,
    register_session_routes,
)
from coding_agent.web.routes.shares import ShareDeps, register_share_routes
from coding_agent.web.routes.snapshots import (
    SnapshotDeps,
    register_snapshot_routes,
)
from coding_agent.web.routes.system import SystemDeps, register_system_routes
from coding_agent.web.routes.tokens import TokenDeps, register_token_routes
from coding_agent.web.routes.uploads import (
    UploadDeps,
    register_upload_routes,
)
from coding_agent.web.routes.webhooks import (
    WebhookDeps,
    register_webhook_routes,
)
from coding_agent.web.routes.workspace import (
    WorkspaceDeps,
    register_workspace_routes,
)
from coding_agent.web.routes.workspaces import (
    WorkspaceRegistryDeps,
    register_workspace_registry_routes,
)
from coding_agent.web.services import (
    AgentGateway,
    ProviderService,
    SessionCatalog,
)
from coding_agent.web.sse import (
    StreamEntry,
)
from coding_agent.web.sse import (
    event_is_terminal as _event_is_terminal,
)
from coding_agent.web.sse import (
    event_key as _event_key,
)
from coding_agent.web.sse import (
    events_after as _events_after,
)
from coding_agent.web.sse import (
    is_suspended as _is_suspended,
)
from coding_agent.web.sse import (
    sse as _sse,
)
from coding_agent.web.sse import (
    sse_with_id as _sse_with_id,
)
from coding_agent.web.transcripts import load_transcript
from coding_agent.web.version import (
    MIN_API_VERSION,
    MIN_FRONTEND_VERSION,
    WEB_API_VERSION,
)

_LOG = logging.getLogger(__name__)


WEB_APPROVAL_MODE = "ask"
# 每个会话的重放缓冲上限；超出后丢最旧事件（序号仍连续）。
_STREAM_BUFFER_MAX_CHARS = 8 * 1024 * 1024
# A2 周期探测 A3；A3 重启后状态自动从 unavailable 回到 ok。
AGENT_PROBE_INTERVAL_S = 5.0


def create_app(
    settings: Settings | None = None,
    auth: AuthConfig | None = None,
    *,
    hsts: bool = False,
    allow_write: bool = False,
    multi_user: bool | None = None,
    default_access_mode: str | None = None,
    stream_buffer: bool = False,
    desktop_mode: bool = False,
    client_source: str = "web",
    event_transport: Any | None = None,
    agent_client: Any | None = None,
) -> FastAPI:
    settings = apply_security_profile(settings or get_settings())
    cors_origins = parse_allowed_origins(settings.web_cors_allowed_origins)
    if (
        desktop_mode
        and settings.workspace_registry
        and not str(settings.workspace_mount_roots or "").strip()
    ):
        # 桌面默认只允许从 Windows 盘（/mnt）导入；企业必须显式配置根。
        settings = settings.model_copy(
            update={"workspace_mount_roots": "/mnt/"}
        )
    # provider 档案是叠加在环境配置之上的；保留环境基线用于删除档案后回退。
    base_llm_providers = str(settings.llm_providers or "")
    serve_frontend, frontend_dir, static_dir = resolve_frontend_dirs(
        settings, desktop_mode
    )
    auth = auth or AuthConfig.from_settings(settings)
    openapi_enabled = bool(getattr(settings, "web_openapi", False))
    app = FastAPI(
        title="CodingAgent",
        docs_url="/docs" if openapi_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if openapi_enabled else None,
    )

    def _request_id(request: Request) -> str:
        return str(getattr(request.state, "request_id", "") or "")

    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        raw = exc.detail
        code = status_error_code(exc.status_code)
        message = ""
        details: dict[str, Any] | None = None
        if isinstance(raw, dict):
            code = str(raw.get("code") or code)
            message = str(raw.get("message") or raw.get("detail") or "")
            details = {
                key: value
                for key, value in raw.items()
                if key not in {"code", "message"}
            }
        else:
            message = str(raw or "")
        return problem_response(
            status_code=exc.status_code,
            code=code,
            detail=message,
            details=details,
            retryable=is_retryable_status(exc.status_code),
            headers=exc.headers,
            instance=request.url.path,
            request_id=_request_id(request),
        )

    async def _validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return problem_response(
            status_code=422,
            code="request.invalid",
            detail="请求参数不合法",
            details={"errors": jsonable_encoder(exc.errors())},
            instance=request.url.path,
            request_id=_request_id(request),
        )

    async def _unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> JSONResponse:
        request_id = _request_id(request)
        _LOG.exception(
            "未处理异常 request_id=%s path=%s", request_id, request.url.path
        )
        return problem_response(
            status_code=500,
            code="server.error",
            detail="服务器内部错误",
            instance=request.url.path,
            request_id=request_id,
        )

    app.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _unhandled_exception_handler)

    # 认证开启 ≠ 多用户。桌面壳有 token，但仍是单用户本地模式。
    accounts_expected = bool(
        settings.web_accounts_enabled and str(settings.control_db_url or "").strip()
    )
    is_multi_user = (
        True
        if accounts_expected
        else (auth.enabled if multi_user is None else bool(multi_user))
    )
    default_access = normalize_access_mode(default_access_mode)
    app.state.is_multi_user = is_multi_user

    # 单进程内所有租户共享一套资源池。
    governor = ConcurrencyGovernor(settings)
    tenants = TenantManager(
        settings, governor=governor, multi_user=is_multi_user
    )

    # A2 控制面：仅当配置了数据库时启用；默认仍走 gui-sessions.json。
    # 数据库不可用时进入只读降级，不能让整个控制面起不来。
    control_store = None
    control_db_url = str(settings.control_db_url or "").strip()
    control_db_status: dict[str, Any] = {"state": "disabled"}
    if control_db_url:
        from coding_agent.control.store import ControlStore

        try:
            control_store = ControlStore(
                control_db_url,
                auto_create=bool(settings.control_db_auto_create),
            )
            control_db_status = {"state": "ok"}
        except Exception as exc:  # noqa: BLE001 - 降级是契约行为
            control_db_status = {
                "state": "degraded",
                "error": f"{type(exc).__name__}: {exc}",
            }
            _LOG.warning(
                "控制面数据库不可用，进入只读降级：%s", exc
            )

    api_token_store = None
    webhook_store = None
    webhook_service = None
    share_store = None
    if control_store is not None:
        try:
            # 只清理过期预留；其他实例仍在运行的预留不能被误清零。
            control_store.purge_expired_reservations()
        except Exception:  # noqa: BLE001 - 清理失败不阻断启动
            _LOG.warning("清理配额预留失败", exc_info=True)
        from coding_agent.control.shares import ShareStore
        from coding_agent.control.webhooks import (
            WebhookService,
            WebhookStore,
        )

        webhook_store = WebhookStore(control_store.engine)
        webhook_service = WebhookService(webhook_store)
        share_store = ShareStore(control_store.engine)

    if settings.web_accounts_enabled:
        if not control_db_url:
            raise RuntimeError(
                "AGENT_WEB_ACCOUNTS_ENABLED=true 时必须配置 "
                "AGENT_CONTROL_DB_URL（生产要求 MySQL）"
            )
        if settings.web_accounts_require_mysql:
            from sqlalchemy.engine import make_url

            backend = make_url(control_db_url).get_backend_name()
            if backend != "mysql":
                raise RuntimeError(
                    "企业账号要求 MySQL（AGENT_CONTROL_DB_URL 当前为 "
                    f"{backend}）；仅本地测试可设置 "
                    "AGENT_WEB_ACCOUNTS_REQUIRE_MYSQL=false"
                )

    # 企业账号：只有显式开启且控制面数据库可用时才绑定会话后端。
    account_store = None
    session_store = None
    auth_events = None
    password_resets = None
    credential_store = None
    if control_store is not None and settings.web_accounts_enabled:
        from coding_agent.control.credentials import CredentialStore

        credential_store = CredentialStore.from_settings(
            control_store.engine, settings
        )
    if control_store is not None and settings.web_accounts_enabled:
        from coding_agent.control.accounts import (
            AuthEventStore,
            PasswordResetStore,
            SessionStore,
            UserStore,
        )

        account_store = UserStore(
            control_store.engine, tenant=settings.web_account_tenant
        )
        session_store = SessionStore(
            control_store.engine,
            ttl_s=settings.web_session_ttl_s,
            idle_ttl_s=settings.web_session_idle_ttl_s,
        )
        auth_events = AuthEventStore(
            control_store.engine, tenant=settings.web_account_tenant
        )
        password_resets = PasswordResetStore(
            control_store.engine,
            ttl_s=settings.web_password_reset_ttl_s,
        )
        auth.bind_accounts(account_store, session_store)
        if account_store is not None:
            from coding_agent.control.api_tokens import ApiTokenStore

            api_token_store = ApiTokenStore(control_store.engine)
            auth.api_tokens = api_token_store
        if settings.control_db_auto_create:
            _LOG.warning(
                "企业账号已启用且 AGENT_CONTROL_DB_AUTO_CREATE=true；"
                "生产环境应关闭自动建表并使用 Alembic 迁移"
            )
    elif settings.web_accounts_enabled:
        _LOG.warning(
            "AGENT_WEB_ACCOUNTS_ENABLED 已开启，但控制面数据库不可用；"
            "账号登录保持关闭"
        )
    app.state.account_store = account_store
    app.state.session_store = session_store

    # RFC-0004：OIDC / MFA / 四眼审批。默认关闭；开启时前置条件不满足
    # 直接失败，避免“配置了却静默不生效”。
    identity = _build_identity_stores(
        settings, control_store, account_store, session_store
    )
    oidc_client = identity.oidc_client
    oidc_states = identity.oidc_states
    mfa_store = identity.mfa_store
    mfa_challenges = identity.mfa_challenges
    approval_store = identity.approval_store
    app.state.oidc_client = oidc_client
    app.state.mfa_store = mfa_store
    app.state.approval_store = approval_store

    def _record_approval(
        user: AuthUser,
        _tenant: Any,
        public_thread: str,
        event: dict[str, Any],
    ) -> None:
        if approval_store is None:
            return
        approval_store.record_pending(
            tenant=user.tenant,
            thread_id=public_thread,
            call_id=str(event.get("request_id") or ""),
            tool=str(event.get("tool") or ""),
            command=str(event.get("command") or ""),
            level=str(event.get("level") or ""),
            level_int=int(event.get("level_int") or 0),
            requested_by=user.name,
            ttl_s=int(settings.approval_ttl_s or 3600),
        )

    # M4：运行租约与幂等键只在控制面数据库可用时装配。
    run_leases = None
    idempotency = None
    app_instance_id = uuid.uuid4().hex[:12]
    if control_store is not None:
        from coding_agent.control.idempotency import IdempotencyStore
        from coding_agent.control.leases import RunLeaseStore

        run_leases = RunLeaseStore(control_store.engine)
        idempotency = IdempotencyStore(control_store.engine)

    # RFC-0001：注册表只在启用且数据库可用时装配；否则路由返回 404/503。
    workspace_store = None
    if settings.workspace_registry:
        if control_store is None:
            _LOG.warning(
                "工作区注册表已启用，但控制面数据库不可用；注册表不可用"
            )
        else:
            from coding_agent.control.workspaces import (
                WorkspacePolicy,
                WorkspaceStore,
            )

            policy = WorkspacePolicy.from_settings(settings)
            if not policy.root:
                try:
                    policy = replace(
                        policy, root=tenants.workspaces_root
                    )
                except Exception as exc:  # noqa: BLE001 - 无 WSL 时降级
                    _LOG.warning("无法解析默认工作区根：%s", exc)
            if policy.root:
                workspace_store = WorkspaceStore(
                    control_store.engine, policy
                )
            else:
                _LOG.warning(
                    "工作区注册表已启用，但 AGENT_WORKSPACE_ROOT 为空"
                )

    def _mark_db_degraded(exc: Exception) -> None:
        app.state.control_db_status = {
            "state": "degraded",
            "error": f"{type(exc).__name__}: {exc}",
        }

    def _control_db_degraded() -> bool:
        return bool(control_db_url) and (
            control_store is None
            or app.state.control_db_status.get("state") == "degraded"
        )

    def _reject_if_db_degraded() -> None:
        if _control_db_degraded():
            raise HTTPException(
                status_code=503,
                detail=(
                    "控制面数据库不可用；当前为只读降级模式，"
                    "请恢复数据库后重启控制面"
                ),
            )

    owns_transport = event_transport is None
    transport = event_transport
    if (
        not stream_buffer
        and transport is None
        and str(settings.event_transport or "memory").strip().lower()
        == "redis"
    ):
        raise ValueError(
            "AGENT_EVENT_TRANSPORT=redis 需要 stream_buffer=True；"
            "server/control/desktop 入口已默认开启"
        )
    if stream_buffer and transport is None:
        from coding_agent.protocol.event_transport import build_event_transport

        transport = build_event_transport(settings)

    # A2 → A3：配置了内部 URL 时，运行生命周期走独立 Agent Server。
    if agent_client is None and str(settings.agent_internal_url or "").strip():
        from coding_agent.control.agent_client import HttpAgentClient

        agent_client = HttpAgentClient(
            settings.agent_internal_url,
            settings.agent_internal_token,
            hmac_key=settings.agent_internal_hmac_key,
        )

    providers = ProviderService(
        settings,
        base_llm_providers=base_llm_providers,
        is_multi_user=is_multi_user,
        credentials=credential_store,
    )
    runtimes = LocalRuntimeRegistry(
        allow_write=allow_write,
        governor=governor,
        default_access=default_access,
        approval_mode=WEB_APPROVAL_MODE,
        max_runtimes=settings.web_max_runtimes or 1,
        max_per_user=settings.web_max_runtimes_per_user or 1,
        runtime_ttl=settings.web_runtime_ttl_s or 0,
        secret_store_for=providers.tenant_store,
    )
    gateway = AgentGateway(
        agent_client,
        providers,
        effective_access=runtimes.effective_access,
        allow_write=allow_write,
    )
    streams: dict[tuple[str, str], StreamEntry] = {}
    session_configs: dict[tuple[str, str], SessionConfig] = {}
    # 删除是 tombstone：不再出现在列表里，但审计/checkpoint 保留供追责。
    deleted_sessions: set[tuple[str, str]] = set()
    sessions_loaded: set[str] = set()
    registry_lock = threading.Lock()
    session_file_lock = threading.Lock()
    def _make_limiter(per_minute: int, prefix: str):
        if str(settings.rate_limit_backend or "memory").lower() != "redis":
            return FixedWindowRateLimiter(per_minute=per_minute)
        url = str(
            settings.rate_limit_redis_url
            or settings.event_transport_url
            or ""
        ).strip()
        if not url:
            raise ValueError(
                "AGENT_RATE_LIMIT_BACKEND=redis 需要 "
                "AGENT_RATE_LIMIT_REDIS_URL（或 AGENT_EVENT_TRANSPORT_URL）"
            )
        import redis

        client = redis.Redis.from_url(url, decode_responses=True)
        try:
            client.ping()
        except Exception as exc:  # noqa: BLE001 - 启动即失败比限流失效好
            raise RuntimeError(
                f"Redis 限流后端不可用：{type(exc).__name__}: {exc}"
            ) from exc
        return RedisFixedWindowRateLimiter(
            client, per_minute=per_minute, prefix=prefix
        )

    limiter = _make_limiter(
        settings.web_rate_limit_per_min, "coding-agent:rate"
    )
    event_backend = (
        "redis"
        if transport is not None
        and type(transport).__name__ == "RedisStreamEventTransport"
        else ("memory" if transport is not None else "none")
    )
    rate_backend = (
        "redis"
        if isinstance(limiter, RedisFixedWindowRateLimiter)
        else "memory"
    )
    credential_backend = (
        "db-encrypted"
        if credential_store is not None
        and bool(getattr(credential_store, "key", b""))
        else ("keyring" if credential_store is None else "unconfigured")
    )
    app.state.collaboration = {
        "event_transport": event_backend,
        "rate_limit": rate_backend,
        "credentials": credential_backend,
        "shared_state": (
            event_backend == "redis"
            and rate_backend == "redis"
            and credential_backend == "db-encrypted"
        ),
    }
    catalog = SessionCatalog(
        tenants=tenants,
        tenant=lambda user: _tenant(user),
        default_access=default_access,
        control_store=control_store,
        control_db_url=control_db_url,
        session_configs=session_configs,
        deleted_sessions=deleted_sessions,
        sessions_loaded=sessions_loaded,
        registry_lock=registry_lock,
        session_file_lock=session_file_lock,
        db_status=lambda: app.state.control_db_status,
        mark_db_degraded=_mark_db_degraded,
    )


    def _tenant(user: AuthUser) -> WebTenant:
        try:
            tenant = tenants.for_user(user.name, org=user.tenant)
        except TenantError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - 沙箱/目录不可用
            raise HTTPException(
                status_code=503,
                detail=f"工作区不可用：{type(exc).__name__}: {exc}",
            ) from exc
        providers.apply()
        return tenant

    def _rate_limit(user: AuthUser) -> None:
        if limiter.allow(user.name):
            return
        retry = limiter.retry_after(user.name)
        raise HTTPException(
            status_code=429,
            detail="请求过于频繁",
            headers={"Retry-After": str(retry)},
        )

    login_limiter = _make_limiter(
        settings.web_login_rate_limit_per_min, "coding-agent:login-rate"
    )

    def _login_rate_limit(ip: str) -> None:
        key = ip or "unknown"
        if login_limiter.allow(key):
            return
        retry = login_limiter.retry_after(key)
        raise HTTPException(
            status_code=429,
            detail={
                "code": "auth.rate_limited",
                "message": "登录尝试过于频繁，请稍后再试",
            },
            headers={"Retry-After": str(retry)},
        )

    csp_limiter = _make_limiter(
        settings.web_csp_report_rate_limit_per_min,
        "coding-agent:csp-report",
    )

    def _csp_rate_limit(ip: str) -> None:
        key = ip or "unknown"
        if csp_limiter.allow(key):
            return
        retry = csp_limiter.retry_after(key)
        raise HTTPException(
            status_code=429,
            detail={"code": "csp.rate_limited"},
            headers={"Retry-After": str(retry)},
        )

    async def _sweep_expired() -> None:
        now = time.monotonic()
        # 事件缓冲只用于重连，保留 5 分钟；比 runtime TTL 短，避免长跑服务的
        # 内存被已完成会话无限占用。
        stream_ttl = 300.0
        with registry_lock:
            for key, stream_entry in list(streams.items()):
                if stream_entry.done and now - stream_entry.updated > stream_ttl:
                    streams.pop(key, None)
        await runtimes.sweep(now=now)

    def _access_limit(user: AuthUser) -> str:
        if account_store is None:
            return ACCESS_WRITE_AUTO
        row = (
            account_store.get(user.user_id)
            if user.user_id
            else account_store.get_by_username(user.name)
        )
        return str(
            (row or {}).get("max_access_mode") or ACCESS_WRITE_AUTO
        )

    def _config_for(
        user: AuthUser,
        tenant: WebTenant,
        public_thread: str,
        *,
        create: bool = True,
    ) -> SessionConfig | None:
        config = catalog.config_for(
            user, tenant, public_thread, create=create
        )
        if config is None:
            return None
        limit = _access_limit(user)
        # 空串是旧语义（由 allow_write + approval_mode 决定），不要改写；
        # 上限为 write_auto 时也没有需要钳制的东西。
        if not config.access_mode or limit == ACCESS_WRITE_AUTO:
            return config
        mode = clamp_access_mode(config.access_mode, limit)
        if mode != config.access_mode:
            # 只钳制本次读取；管理员提高上限后，原会话仍可恢复自己的模式。
            config = replace(config, access_mode=mode)
        return config

    def _effective_access(config: SessionConfig | None) -> str | None:
        return runtimes.effective_access(config)

    def _new_runtime(
        tenant: WebTenant, config: SessionConfig | None = None
    ) -> Any:
        return runtimes.new(tenant, config)

    def _ops_runtime(
        tenant: WebTenant, config: SessionConfig | None
    ) -> Any:
        return runtimes.ops(tenant, config)

    async def _drop_runtime(key: tuple[str, str], runtime: Any) -> None:
        await runtimes.drop(key, runtime)

    def _append_stream_event(entry: StreamEntry, event: Event) -> None:
        entry.seq += 1
        entry.events.append((entry.seq, event))
        entry.chars += len(event.model_dump_json())
        while entry.chars > _STREAM_BUFFER_MAX_CHARS and len(entry.events) > 1:
            _, old = entry.events.pop(0)
            entry.chars -= len(old.model_dump_json())
        if len(entry.events) > 5_000:
            dropped = entry.events[: len(entry.events) - 5_000]
            del entry.events[: len(entry.events) - 5_000]
            for _, old in dropped:
                entry.chars -= len(old.model_dump_json())
        entry.updated = time.monotonic()
        if transport is not None:
            try:
                transport.publish(
                    _event_key(entry.key[0], entry.public_thread),
                    entry.seq,
                    event.model_dump_json(),
                )
            except Exception:  # noqa: BLE001 - 传输故障不能中断运行
                _LOG.warning("事件传输发布失败", exc_info=True)
        for queue in list(entry.subscribers):
            try:
                queue.put_nowait((entry.seq, event))
            except asyncio.QueueFull:
                pass

    async def _record_event_notification(
        user_name: str, public_thread: str, event: Event
    ) -> None:
        """本地 runtime 路径也写站内通知；代理路径由 runs.py 负责。"""
        if control_store is None:
            return
        if not isinstance(event, (ApprovalRequested, RunFailed, RunFinished)):
            return
        notice = notification_payload(event.model_dump())
        if notice is None:
            return
        kind, title, body, dedupe = notice
        try:
            row = await asyncio.to_thread(
                control_store.add_notification,
                user_name,
                kind=kind,
                title=title,
                body=body,
                thread_id=public_thread,
                dedupe_key=dedupe,
            )
            if row:
                schedule_push(push_service, user_name, row)
                schedule_webhooks(
                    webhook_service,
                    user_name,
                    str(getattr(event, "type", "") or ""),
                    {
                        "title": title,
                        "body": body,
                        "kind": kind,
                        "thread_id": public_thread,
                        "created_at": row.get("created_at", ""),
                    },
                )
        except Exception as exc:  # noqa: BLE001 - 通知失败不打断运行
            note_silent_failure("notification")
            _LOG.warning("通知写入失败：%s", exc)

    async def _consume_stream(
        entry: StreamEntry, events: AsyncIterator[Event]
    ) -> None:
        try:
            async for event in events:
                if _is_suspended(event):
                    entry.suspended = True
                _append_stream_event(entry, event)
                await _record_event_notification(
                    entry.key[0], entry.public_thread, event
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 错误以事件形式送达订阅者
            failed = RunFailed(message=f"{type(exc).__name__}: {exc}")
            _append_stream_event(entry, failed)
        finally:
            if not entry.suspended:
                await _drop_runtime(entry.key, entry.runtime)
            entry.done = True
            entry.updated = time.monotonic()
            for queue in list(entry.subscribers):
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    def _stream_guard(
        key: tuple[str, str], *, require_suspended: bool = False
    ) -> None:
        """拒绝在 active 流上再启动 resume；跨进程 resume 没有流时放行。"""
        old = streams.get(key)
        if old is None:
            return
        if not old.done:
            raise HTTPException(
                status_code=409,
                detail="该会话已有运行中的事件流；请等待或先取消",
            )
        if require_suspended and not old.suspended:
            raise HTTPException(
                status_code=409,
                detail="该会话没有待审批的挂起",
            )

    def _start_stream(
        key: tuple[str, str],
        public_thread: str,
        runtime: Any,
        events: AsyncIterator[Event],
    ) -> StreamEntry:
        old = streams.pop(key, None)
        # 序号跨 resume/进程重启保持单调；传输层可能保留旧进程的事件。
        base_seq = old.seq if old is not None else 0
        if transport is not None:
            try:
                base_seq = max(
                    base_seq,
                    transport.latest_seq(_event_key(key[0], public_thread)),
                )
            except Exception:  # noqa: BLE001
                _LOG.warning("读取事件传输序号失败", exc_info=True)
        # 每个用户只保留最近 8 个已完成流，避免长跑服务被重放缓冲撑爆。
        with registry_lock:
            owned = [
                (other_key, other)
                for other_key, other in streams.items()
                if other_key[0] == key[0] and other.done
            ]
            if len(owned) >= 8:
                owned.sort(key=lambda item: item[1].updated)
                for other_key, _ in owned[: len(owned) - 7]:
                    streams.pop(other_key, None)
        entry = StreamEntry(
            key=key,
            public_thread=public_thread,
            runtime=runtime,
            seq=base_seq,
        )
        entry.task = asyncio.create_task(_consume_stream(entry, events))
        streams[key] = entry
        return entry

    def _buffered_sse(
        entry: StreamEntry, *, after: int = 0
    ) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            last = max(0, int(after))
            queue: asyncio.Queue = asyncio.Queue(maxsize=2_000)
            entry.subscribers.append(queue)
            try:
                while True:
                    for seq, event in _events_after(entry, last):
                        last = seq
                        yield _sse_with_id(event, seq)
                    if entry.done and last >= entry.seq:
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        # 先把 done 前最后一批事件发完，再退出。
                        for seq, event in _events_after(entry, last):
                            last = seq
                            yield _sse_with_id(event, seq)
                        break
                    seq, event = item
                    if seq <= last:
                        continue
                    last = seq
                    yield _sse_with_id(event, seq)
            finally:
                try:
                    entry.subscribers.remove(queue)
                except ValueError:
                    pass

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers=sse_headers(entry.public_thread),
        )

    def _transport_sse(
        event_key: str, public_thread: str, *, after: int = 0
    ) -> StreamingResponse:
        """没有本地 StreamEntry 时，从共享传输按 seq 重放并继续轮询。"""

        async def stream() -> AsyncIterator[str]:
            last = max(0, int(after))
            idle = 0.0
            silent = 0.0
            while transport is not None:
                try:
                    rows = await asyncio.to_thread(
                        transport.replay, event_key, last
                    )
                except Exception:  # noqa: BLE001 - 传输故障不伪造事件
                    _LOG.warning("事件传输重放失败", exc_info=True)
                    return
                for seq, payload in rows:
                    if seq <= last:
                        continue
                    last = seq
                    idle = silent = 0.0
                    yield f"id: {seq}\ndata: {payload}\n\n"
                    if _event_is_terminal(payload):
                        return
                await asyncio.sleep(0.5)
                idle += 0.5
                silent += 0.5
                if idle >= 15:
                    yield ": keepalive\n\n"
                    idle = 0.0
                if silent >= 300:
                    return

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers=sse_headers(public_thread),
        )

    def _get_entry(user: str, public_thread: str) -> RuntimeEntry | None:
        with registry_lock:
            entry = runtimes.get((user, public_thread))
        if entry is not None:
            entry.last_active = time.monotonic()
        return entry

    def _capabilities(config: SessionConfig | None) -> dict[str, Any]:
        access = _effective_access(config)
        read_only = access == ACCESS_READ_ONLY
        return {
            "access_mode": access or "legacy",
            "read_only": read_only,
            "write_enabled": not read_only and bool(access),
            "execution_enabled": not read_only,
            "can_upload": True,
            "can_restore": True,
            "can_fork": True,
            "can_steer": True,
            "multi_user": is_multi_user,
            "server_write_enabled": allow_write,
            "workspace_registry": bool(
                settings.workspace_registry
                and workspace_store is not None
            ),
            "host_mode": settings.workspace_kind_default == "host",
            "workspace_upload_max_bytes": int(
                settings.workspace_upload_max_bytes
            ),
            "desktop_mode": desktop_mode,
            "security_profile": settings.security_profile,
            "workspace": config.workspace if config is not None else "",
            "run_cost_budget_usd": float(
                settings.run_cost_budget_usd or 0.0
            ),
            "cost_alert_ratio": float(settings.cost_alert_ratio or 0.8),
            "max_upload_bytes": int(settings.web_max_upload_bytes),
            "max_upload_total_bytes": int(
                settings.web_max_upload_total_bytes
            ),
        }

    async def _runtime_for_resume(
        user: AuthUser, public_thread: str
    ) -> tuple[tuple[str, str], Any]:
        key = (user.name, public_thread)
        entry = _get_entry(*key)
        if entry is not None:
            return key, entry.runtime
        # 进程重启/TTL 清理后，从 per-user checkpoint 重建。
        tenant = _tenant(user)
        # 进程重启后首次恢复可能触发控制面读取；不要在事件循环线程里做 DB I/O。
        config = await asyncio.to_thread(
            _config_for, user, tenant, public_thread
        )
        runtime = _new_runtime(tenant, config)
        with registry_lock:
            runtimes.put(key, runtime)
        return key, runtime

    def _sse_response(
        key: tuple[str, str],
        public_thread: str,
        runtime: Any,
        events: AsyncIterator[Event],
    ) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            suspended = False
            try:
                async for event in events:
                    if _is_suspended(event):
                        suspended = True
                    yield _sse(event)
                    await _record_event_notification(
                        key[0], public_thread, event
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 错误以事件形式送到前端
                yield _sse(RunFailed(message=f"{type(exc).__name__}: {exc}"))
            finally:
                if suspended:
                    entry = _get_entry(*key)
                    if entry is not None:
                        entry.last_active = time.monotonic()
                else:
                    await _drop_runtime(key, runtime)

        return StreamingResponse(
            with_heartbeat(stream()),
            media_type="text/event-stream",
            headers=sse_headers(public_thread),
        )

    # 最内层：把 /api/v1/* 重写为 /api/*，路由只注册一份。
    app.add_middleware(
        ApiVersionRewriteMiddleware,
        current=WEB_API_VERSION,
        deprecated=MIN_API_VERSION,
    )
    app.add_middleware(
        BodySizeLimitMiddleware,
        max_bytes=settings.web_max_body_bytes,
        # 会话上传是多文件 multipart；请求体上限按累计配额 + 头部余量，
        # 单文件上限仍由路由逐文件校验。
        upload_max_bytes=(
            max(
                int(settings.web_max_upload_total_bytes),
                int(settings.web_max_upload_bytes),
            )
            + 1_048_576
        ),
        workspace_upload_max_bytes=settings.workspace_upload_max_bytes,
    )
    app.add_middleware(
        FrontendVersionMiddleware, minimum=MIN_FRONTEND_VERSION
    )
    app.add_middleware(SecurityHeadersMiddleware, hsts=hsts)
    if cors_origins:
        # 企业账号用 Cookie 时，独立源站需要 credentials；静态 Bearer 不需要。
        app.add_middleware(
            StrictCORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=bool(settings.web_accounts_enabled),
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "Last-Event-ID",
                "X-Request-ID",
                "X-Frontend-Version",
                "X-CSRF-Token",
            ],
            expose_headers=[
                "X-Request-ID",
                "X-Thread-Id",
                "Retry-After",
                "WWW-Authenticate",
                "Deprecation",
                "Sunset",
            ],
            max_age=600,
        )
    from coding_agent.platform.observability import configure_tracing

    otel_tracer = configure_tracing(settings)
    if otel_tracer is not None:
        app.add_middleware(TracingMiddleware, tracer=otel_tracer)
    # 放在 HttpMetrics 内层：只读拒绝也要计入指标。
    app.add_middleware(AuditorReadOnlyMiddleware)
    app.add_middleware(HttpMetricsMiddleware)
    # 最后添加 = 最外层：保证 CORS/BodySize/Security 提前返回的错误也带 request id。
    app.add_middleware(RequestIdMiddleware)
    app.state.auth = auth
    app.state.tenants = tenants
    app.state.governor = governor
    app.state.desktop_mode = desktop_mode
    app.state.control_store = control_store
    app.state.control_db_status = control_db_status
    app.state.event_transport = transport
    app.state.agent_client = agent_client
    # 独立进程模式下由 control_server 在启动时用握手结果覆盖。
    app.state.agent_status = {
        "state": "in_process" if agent_client is None else "unknown"
    }
    if agent_client is not None:
        from coding_agent.agent_protocol import check_compatibility

        async def _probe_agent_forever() -> None:
            while True:
                try:
                    meta = await agent_client.probe()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - A3 重启是预期状态
                    app.state.agent_status = {
                        "state": "unavailable",
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                else:
                    compat = check_compatibility(meta)
                    app.state.agent_status = {
                        "state": "ok" if compat.ok else "incompatible",
                        "meta": meta.to_dict(),
                        "reason": compat.reason,
                        "warning": compat.warning,
                    }
                await asyncio.sleep(AGENT_PROBE_INTERVAL_S)

        async def _start_probe() -> None:
            app.state.agent_probe_task = asyncio.create_task(
                _probe_agent_forever(), name="agent-probe"
            )

        async def _stop_probe() -> None:
            task = getattr(app.state, "agent_probe_task", None)
            if task is None:
                return
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - 关闭阶段不再抛错
                pass

        app.router.on_startup.append(_start_probe)
        app.router.on_shutdown.append(_stop_probe)
    if control_store is not None:
        # FastAPI 新版本移除了 add_event_handler；router 的 on_shutdown 仍可用。
        app.router.on_shutdown.append(control_store.close)
    if owns_transport and transport is not None:
        app.router.on_shutdown.append(transport.close)
    if agent_client is not None:
        app.router.on_shutdown.append(agent_client.aclose)

    @app.get("/config.json", include_in_schema=False)
    async def runtime_config() -> JSONResponse:
        """前端运行时配置；公开但只含非敏感传输参数。

        独立部署时由静态站自己提供 config.json；这里的默认值用于桌面
        与同域反代。
        """
        return JSONResponse(
            {
                "schemaVersion": 1,
                "apiBaseUrl": "",
                "apiVersion": WEB_API_VERSION,
                "authMode": (
                    "desktop-token"
                    if desktop_mode
                    else ("local-session" if account_store is not None else "bearer")
                ),
                "auth": {
                    "csrfHeader": "X-CSRF-Token",
                },
                "cost": currency.as_dict(),
                "sse": {
                    "heartbeatMs": SSE_HEARTBEAT_MS,
                    "idleTimeoutMs": SSE_HEARTBEAT_MS * 3,
                    "reconnectBaseMs": 500,
                    "reconnectMaxMs": 30000,
                    "reconnectMaxAttempts": 3,
                },
                "requestIdHeader": "X-Request-ID",
            },
            headers={"Cache-Control": "no-cache"},
        )

    app.state.serve_frontend = serve_frontend
    app.state.frontend_dir = str(frontend_dir)
    app.state.static_dir = str(static_dir)

    if serve_frontend:

        @app.get("/", response_class=HTMLResponse)
        async def index() -> str:
            return (frontend_dir / "index.html").read_text(encoding="utf-8")

        @app.get("/admin", response_class=HTMLResponse)
        @app.get("/admin/", response_class=HTMLResponse)
        async def admin_index() -> str:
            """管理端是独立静态入口；数据仍全部走 /api/admin/*。"""
            page = frontend_dir / "admin" / "index.html"
            if not page.is_file():
                raise HTTPException(status_code=404, detail="管理端未构建")
            return page.read_text(encoding="utf-8")

        @app.get("/sw.js", include_in_schema=False)
        async def service_worker() -> FileResponse:
            """Service Worker 必须从根路径提供，才能控制 `/` 与应用外壳。"""
            return FileResponse(
                frontend_dir / "sw.js",
                media_type="application/javascript",
                headers={
                    "Service-Worker-Allowed": "/",
                    "Cache-Control": "no-cache",
                },
            )

        @app.get("/manifest.webmanifest", include_in_schema=False)
        async def manifest() -> FileResponse:
            return FileResponse(
                frontend_dir / "manifest.webmanifest",
                media_type="application/manifest+json",
            )

        app.mount(
            "/static", StaticFiles(directory=static_dir), name="static"
        )

    def _reject_if_active(user: AuthUser, public_thread: str) -> None:
        if _get_entry(user.name, public_thread) is not None:
            raise HTTPException(
                status_code=409,
                detail="该会话正在运行或已挂起；请先结束/恢复再执行此操作",
            )

    def _config_and_tenant(
        user: AuthUser, public_thread: str
    ) -> tuple[WebTenant, SessionConfig | None]:
        # create=False 只读内存：会话目录首次 load 已由路由层用 to_thread
        # 完成。不要再在这里触发同步控制面 DB 调用。
        tenant = _tenant(user)
        config = _config_for(user, tenant, public_thread, create=False)
        if config is None:
            workspace = catalog.audit_workspace(user, tenant, public_thread)
            if workspace:
                config = SessionConfig(
                    thread_id=public_thread,
                    workspace=workspace,
                    access_mode=default_access,
                )
        return tenant, config

    register_system_routes(
        app,
        SystemDeps(
            settings=settings,
            tenants=tenants,
            tenant=_tenant,
            tenant_store=providers.tenant_store,
            provider_rows=providers.rows,
            config_for=_config_for,
            capabilities=_capabilities,
            default_access=default_access,
            desktop_mode=desktop_mode,
            csp_rate_limit=_csp_rate_limit,
        ),
    )

    from coding_agent.costs import CurrencyConfig

    currency = CurrencyConfig.from_settings(settings)
    register_auth_routes(
        app,
        AuthRouteDeps(
            settings=settings,
            auth=auth,
            accounts=account_store,
            sessions=session_store,
            control_store=control_store,
            rate_limit=_rate_limit,
            login_rate_limit=_login_rate_limit,
            auth_events=auth_events,
            currency=currency,
            oidc_client=oidc_client,
            oidc_states=oidc_states,
            oidc_enabled=bool(settings.oidc_enabled),
            mfa_store=mfa_store,
            mfa_challenges=mfa_challenges,
            mfa_enabled=bool(settings.mfa_enabled),
            mfa_issuer=str(settings.mfa_issuer or "CodingAgent"),
            mfa_window_steps=int(settings.mfa_window_steps or 1),
            mfa_max_attempts=int(settings.mfa_max_attempts or 5),
            mfa_challenge_ttl_s=int(settings.mfa_challenge_ttl_s or 300),
            password_resets=password_resets,
        ),
    )

    register_admin_routes(
        app,
        AdminDeps(
            settings=settings,
            accounts=account_store,
            sessions=session_store,
            control_store=control_store,
            provider_rows=providers.rows,
            user_store=providers.user_store,
            auth_events=auth_events,
            currency=currency,
            approvals=approval_store,
            mfa=mfa_store,
            workspace_store=workspace_store,
        ),
    )

    push_service = PushService(settings, control_store)
    register_push_routes(
        app,
        PushDeps(push=push_service, rate_limit=_rate_limit),
    )
    register_token_routes(
        app,
        TokenDeps(tokens=api_token_store, rate_limit=_rate_limit),
    )
    register_webhook_routes(
        app,
        WebhookDeps(service=webhook_service, rate_limit=_rate_limit),
    )
    register_notification_routes(
        app,
        NotificationDeps(
            control_store=control_store,
            rate_limit=_rate_limit,
        ),
    )

    register_provider_routes(
        app,
        ProviderDeps(
            settings=settings,
            allow_write=allow_write,
            is_multi_user=is_multi_user,
            base_llm_providers=base_llm_providers,
            profiles_path=providers.profiles_path,
            apply_profiles=providers.apply,
            resolve_provider=providers.resolve,
            user_store=providers.user_store,
            rate_limit=_rate_limit,
            provider_rows=providers.rows,
        ),
    )

    def _local_audit_records(tenant: WebTenant) -> list[dict[str, Any]]:
        """本地运行没有 A3 HTTP，直接从租户审计目录取最近记录。"""
        from coding_agent.audit import audit_files_by_day, read_records_many

        groups = audit_files_by_day(tenant.settings.resolved_audit_dir)
        paths: list[Any] = []
        for day in sorted(groups)[-2:]:
            paths.extend(groups[day])
        return [
            record.model_dump(exclude_none=True)
            for record in read_records_many(paths, limit=500)
        ]

    async def _flush_audit(
        user: AuthUser, tenant: WebTenant, config: Any
    ) -> None:
        """运行终态后把审计幂等写入控制面索引与用量表。"""
        if control_store is None:
            return
        try:
            if agent_client is not None:
                data = await agent_client.json_request(
                    gateway.context(
                        user, tenant, config, include_keys=False
                    ),
                    "GET",
                    "/v1/audit",
                    params={"limit": 500},
                )
                records = list(data.get("records", []))
            else:
                records = await asyncio.to_thread(
                    _local_audit_records, tenant
                )
            await asyncio.to_thread(
                control_store.index_audit,
                user.name,
                records,
                usage_tenant=user.tenant,
                usage_user=user.name,
            )
        except Exception:  # noqa: BLE001 - 索引失败不影响运行
            _LOG.warning("终态审计索引回写失败", exc_info=True)

    def _bind_workspace(
        user: AuthUser, workspace_id: str
    ) -> dict[str, Any]:
        if not settings.workspace_registry:
            raise HTTPException(
                status_code=400, detail="工作区注册表未启用"
            )
        if workspace_store is None:
            raise HTTPException(
                status_code=503,
                detail="工作区注册表需要可用的控制面数据库",
            )
        row = workspace_store.get(workspace_id)
        if (
            row is None
            or row.get("tenant") != user.name
            or row.get("owner") != user.name
        ):
            raise HTTPException(status_code=404, detail="工作区不存在")
        if row.get("status") != "active":
            raise HTTPException(
                status_code=409,
                detail=f"工作区尚未就绪：{row.get('status')}",
            )
        return row

    consent_token = str(settings.consent_bridge_token or "").strip()
    if desktop_mode and consent_token:
        from coding_agent.web.consent_bridge import register_consent_bridge

        register_consent_bridge(app, token=consent_token)

    register_workspace_registry_routes(
        app,
        WorkspaceRegistryDeps(
            rate_limit=_rate_limit,
            store=workspace_store,
            enabled=bool(settings.workspace_registry),
            settings=settings,
            agent_client=agent_client,
            idempotency=idempotency,
        ),
    )

    register_session_routes(
        app,
        SessionDeps(
            tenant=_tenant,
            sweep_expired=_sweep_expired,
            load_configs=catalog.load,
            config_for=_config_for,
            set_config=catalog.set_config,
            persist_configs=catalog.persist,
            effective_access=_effective_access,
            capabilities=_capabilities,
            reject_deleted=catalog.reject_deleted,
            reject_active=_reject_if_active,
            reject_db_degraded=_reject_if_db_degraded,
            rate_limit=_rate_limit,
            resolve_workspace=lambda name, ws: tenants.resolve_workspace(
                name, ws
            ),
            bind_workspace=_bind_workspace,
            registry_enabled=bool(settings.workspace_registry),
            validate_provider=lambda tenant, user, provider: (
                providers.validate(
                    tenant.settings, providers.user_store(user), provider
                )
            ),
            agent_json=gateway.json,
            ops_runtime=_ops_runtime,
            agent_client=agent_client,
            allow_write=allow_write,
            registry_lock=registry_lock,
            session_configs=session_configs,
            deleted_sessions=deleted_sessions,
            runtimes=runtimes,
            access_limit=_access_limit,
            control_store=control_store,
        ),
    )

    register_workspace_routes(
        app,
        WorkspaceDeps(
            tenant=_tenant,
            rate_limit=_rate_limit,
            resolve_workspace=lambda name, ws: tenants.resolve_workspace(
                name, ws
            ),
            reject_deleted=catalog.reject_deleted,
            config_for=_config_for,
            agent_json=gateway.json,
            agent_failure=gateway.failure,
            agent_client=agent_client,
            default_access=default_access,
            allow_write=allow_write,
            desktop_mode=desktop_mode,
        ),
    )

    register_upload_routes(
        app,
        UploadDeps(
            settings=settings,
            tenant=_tenant,
            rate_limit=_rate_limit,
            config_for=_config_for,
            set_config=catalog.set_config,
            reject_active=_reject_if_active,
            reject_deleted=catalog.reject_deleted,
            agent_client=agent_client,
            agent_upload=gateway.upload,
            agent_json=gateway.json,
            ops_runtime=_ops_runtime,
            workspace_uploaded_bytes=catalog.workspace_uploaded_bytes,
            idempotency=idempotency,
        ),
    )

    register_ops_routes(
        app,
        OpsDeps(
            tenant=_tenant,
            sweep_expired=_sweep_expired,
            load_configs=catalog.load,
            config_for=_config_for,
            deleted_sessions=deleted_sessions,
            agent_client=agent_client,
            agent_json=gateway.json,
            ops_runtime=_ops_runtime,
            rate_limit=_rate_limit,
            get_entry=_get_entry,
            control_store=control_store,
            run_leases=run_leases,
            idempotency=idempotency,
        ),
    )

    register_snapshot_routes(
        app,
        SnapshotDeps(
            config_and_tenant=_config_and_tenant,
            reject_active=_reject_if_active,
            agent_client=agent_client,
            agent_json=gateway.json,
            ops_runtime=_ops_runtime,
            set_config=catalog.set_config,
            effective_access=_effective_access,
        ),
    )
    register_share_routes(
        app,
        ShareDeps(
            shares=share_store,
            config_and_tenant=_config_and_tenant,
            load_transcript=load_transcript,
            rate_limit=_rate_limit,
        ),
    )

    register_run_routes(
        app,
        RunDeps(
            settings=settings,
            client_source=client_source,
            stream_buffer=stream_buffer,
            transport=transport,
            streams=streams,
            registry_lock=registry_lock,
            runtimes=runtimes,
            rate_limit=_rate_limit,
            sweep_expired=_sweep_expired,
            tenant=_tenant,
            config_for=_config_for,
            set_config=catalog.set_config,
            reject_deleted=catalog.reject_deleted,
            agent_client=agent_client,
            agent_context=gateway.context,
            agent_failure=gateway.failure,
            proxy_agent=gateway.proxy,
            stream_guard=_stream_guard,
            start_stream=_start_stream,
            buffered_sse=_buffered_sse,
            sse_response=_sse_response,
            get_entry=_get_entry,
            runtime_for_resume=_runtime_for_resume,
            new_runtime=_new_runtime,
            event_key=_event_key,
            transport_sse=_transport_sse,
            ops_runtime=_ops_runtime,
            run_leases=run_leases,
            lease_owner=app_instance_id,
            lease_ttl_s=int(settings.run_lease_ttl_s or 900),
            audit_flush=_flush_audit,
            idempotency=idempotency,
            control_store=control_store,
            quota_reserve_tokens=int(settings.web_quota_reserve_tokens or 0),
            usd_to_cny=float(settings.usd_to_cny or 1),
            quota_owner=app_instance_id,
            quota_ttl_s=int(settings.quota_reservation_ttl_s or 3600),
            approvals=approval_store,
            four_eyes_min_level=(
                int(settings.approval_four_eyes_min_level or 0)
                if settings.approval_four_eyes
                else 0
            ),
            record_approval=(
                _record_approval if approval_store is not None else None
            ),
            push=push_service,
            webhooks=webhook_service,
        ),
    )

    register_desktop_routes(
        app,
        DesktopDeps(
            app=app,
            desktop_mode=desktop_mode,
            registry_lock=registry_lock,
            runtimes=runtimes,
            tenants=tenants,
        ),
    )

    register_compact_routes(
        app,
        CompactDeps(
            config_and_tenant=_config_and_tenant,
            reject_active=_reject_if_active,
            agent_client=agent_client,
            agent_json=gateway.json,
            set_config=catalog.set_config,
            effective_access=_effective_access,
            ops_runtime=_ops_runtime,
        ),
    )

    return app
