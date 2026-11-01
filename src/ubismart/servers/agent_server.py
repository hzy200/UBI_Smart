"""独立 Agent Server（A3 + A4，Phase B4）。

A2 只通过内部 token + `X-Agent-Context` 调用本进程；这里持有 AgentRuntime、
checkpoint、工作区与 SSE。默认只绑 loopback，不暴露给浏览器。

启动：

    python -m ubismart.servers.agent_server --port 8790
    AGENT_INTERNAL_TOKEN=... AGENT_INTERNAL_URL=...（A2 侧）

当前迁移范围是运行生命周期：runs / stream / resume / resume_steering /
steer / approvals / status / history。文件、快照、上传等 A2 路由仍走原实现，
后续切片继续迁移。
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
import sys
import time
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from ubismart import AGENT_API_VERSION, __version__
from ubismart.agent_protocol import (
    AGENT_CAPABILITIES,
    AgentContext,
    verify_context,
)
from ubismart.config import Settings, get_settings
from ubismart.events import (
    Event,
    RunFailed,
)
from ubismart.host_broker.acceptor import HostBrokerAcceptor
from ubismart.host_broker.devices import DeviceRegistry
from ubismart.host_broker.transport import (
    ServerTlsConfig,
    certificate_fingerprint,
)
from ubismart.platform.paths import (
    PathGuardError,
    ensure_inside,
    looks_like_windows_path,
)
from ubismart.protocol.event_transport import build_event_transport
from ubismart.protocol.sse_stream import sse_headers
from ubismart.runtime import AgentRuntime
from ubismart.servers.agent_context import AgentRuntimeFactory
from ubismart.servers.agent_server_routes.lifecycle import (
    LifecycleDeps,
    register_lifecycle_routes,
)
from ubismart.servers.agent_server_routes.state import (
    StateDeps,
    register_state_routes,
)
from ubismart.servers.agent_server_routes.workspace import (
    WorkspaceDeps,
    register_workspace_routes,
)
from ubismart.servers.agent_server_routes.workspace_import import (
    ImportDeps,
    register_import_routes,
)
from ubismart.servers.agent_server_support import (
    RunState,
    _is_suspended,
    _safe_tenant,
    _sse_with_id,
    _terminal_payload,
)

_STREAM_BUFFER_MAX_CHARS = 8 * 1024 * 1024
_STREAM_BUFFER_MAX_EVENTS = 5_000


def ensure_host_workspace(path: str) -> None:
    """host 工作区的 A3 侧词法兜底（不访问文件系统）。

    host 工作区的绝对路径只登记在 A2L，A2 只下发 `workspace_id +
    root_handle`，A3 拿不到它背后真实的盘符目录——所以 A3 这里做的是
    **兜底而非主防线**：挡住 UNC/设备路径（会落到别的机器或内核对象）
    与相对路径，真正的越界防御在 A2L 的 `root_handle` + `resolve_child`。

    空字符串表示“还没选目录”（`/v1/host-workspaces/register`），直接放行。
    """
    text = str(path or "").strip()
    if not text:
        return
    if "\x00" in text:
        raise PathGuardError("workspace 含空字节")
    # `\\host\share`、`\\.\PhysicalDrive0`、`\\?\C:\` 都要拦；
    # POSIX 下 `//` 前缀同样是网络/实现定义语义。
    if text.startswith(("\\\\", "//")):
        raise PathGuardError(f"不允许 UNC/设备路径：{text}")
    if not os.path.isabs(text) and not looks_like_windows_path(text):
        raise PathGuardError(f"workspace 必须是绝对路径：{text}")


def validate_host_context(
    context: AgentContext, *, require_handle: bool
) -> None:
    """host 工作区上下文校验：handle 必填 + 路径兜底。"""
    if not context.a2l_id:
        raise HTTPException(status_code=400, detail="host 工作区需要 a2l_id")
    if require_handle and (
        not context.workspace_id or not context.root_handle
    ):
        raise HTTPException(
            status_code=400,
            detail="host 工作区需要 workspace_id/root_handle",
        )
    try:
        ensure_host_workspace(context.workspace)
    except PathGuardError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"host workspace 路径不合法：{exc}",
        ) from exc


_KEEPALIVE = object()


def _events_after(
    state: RunState, last: int
) -> AsyncIterator[tuple[int, Event]]:
    for seq, event in list(state.events):
        if seq > last:
            yield seq, event


async def _next_event(queue: asyncio.Queue) -> Any:
    try:
        return await asyncio.wait_for(queue.get(), timeout=15)
    except TimeoutError:
        return _KEEPALIVE


def _discard_subscriber(state: RunState, queue: asyncio.Queue) -> None:
    try:
        state.subscribers.remove(queue)
    except ValueError:
        pass


async def _buffered_stream(
    state: RunState, after: int
) -> AsyncIterator[str]:
    last = max(0, int(after))
    queue: asyncio.Queue = asyncio.Queue(maxsize=2_000)
    state.subscribers.append(queue)
    try:
        while True:
            for seq, event in _events_after(state, last):
                last = seq
                yield _sse_with_id(event, seq)
            if state.done and last >= state.seq:
                return
            item = await _next_event(queue)
            if item is None:
                for seq, event in _events_after(state, last):
                    last = seq
                    yield _sse_with_id(event, seq)
                return
            if item is _KEEPALIVE:
                yield ": keepalive\n\n"
                continue
            seq, event = item
            if seq > last:
                last = seq
                yield _sse_with_id(event, seq)
    finally:
        _discard_subscriber(state, queue)


class _AgentAppHandlers:
    """把 create_agent_app 的闭包收敛为方法，避免注册函数圈复杂度膨胀。"""

    def __init__(
        self,
        app: FastAPI,
        settings: Settings,
        expected: str,
        context_hmac_required: bool,
        agent_factory: AgentRuntimeFactory,
        transport: Any,
    ) -> None:
        self.app = app
        self.settings = settings
        self.expected = expected
        self.context_hmac_required = context_hmac_required
        self.agent_factory = agent_factory
        self.transport = transport
        self.runs: dict[tuple[str, str], AgentRuntime] = {}
        self.states: dict[tuple[str, str], RunState] = {}
        self._probe_sandbox: dict[str, Any] = {
            "instance": None,
            "built": False,
        }

    def drain_guard(self) -> None:
        if self.app.state.draining:
            raise HTTPException(
                status_code=503,
                detail={
                    "code": "agent.draining",
                    "message": "Agent Server 正在优雅退出，暂不接受新运行",
                },
            )

    async def drain(self, timeout_s: float) -> dict[str, Any]:
        """停止接单并等待进行中的运行；超时仍存活的任务取消。"""
        app = self.app
        states = self.states
        app.state.draining = True
        deadline = time.monotonic() + max(0.0, float(timeout_s))
        while time.monotonic() < deadline:
            if not any(not state.done for state in states.values()):
                break
            await asyncio.sleep(0.1)
        pending = [state for state in states.values() if not state.done]
        tasks = [
            state.task
            for state in pending
            if getattr(state, "task", None) is not None
            and not state.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            # 取消是最后手段：等 finally 落盘 checkpoint 并关闭 runtime。
            await asyncio.gather(*tasks, return_exceptions=True)
        return {
            "pending": len(pending),
            "cancelled": len(tasks),
            "completed": len(states) - len(pending),
        }

    def require_token(self, request: Request) -> None:
        header = request.headers.get("authorization", "")
        provided = (
            header[7:].strip() if header.lower().startswith("bearer ") else ""
        )
        if not hmac.compare_digest(provided, self.expected):
            raise HTTPException(status_code=401, detail="内部 token 无效")

    def context(
        self,
        request: Request,
        *,
        require_host_handle: bool = True,
        require_workspace_root: bool = True,
    ) -> AgentContext:
        self.require_token(request)
        raw = request.headers.get("x-agent-context", "")
        signature = request.headers.get("x-agent-signature", "")
        if not verify_context(
            raw,
            signature,
            self.settings.agent_internal_hmac_key,
            required=self.context_hmac_required,
        ):
            raise HTTPException(
                status_code=401, detail="Agent context 签名无效"
            )
        try:
            context = AgentContext.from_header(raw)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail=f"Agent context 无效：{exc}"
            ) from exc
        if context.workspace_kind == "host":
            validate_host_context(
                context, require_handle=require_host_handle
            )
        else:
            if not context.workspace.startswith("/"):
                raise HTTPException(
                    status_code=400, detail="workspace 必须是绝对路径"
                )
        if require_workspace_root and self.settings.workspace_registry:
            allowed = str(self.settings.workspace_root or "").strip()
            if not allowed:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "已启用工作区注册表，但 A3 未配置 "
                        "AGENT_WORKSPACE_ROOT"
                    ),
                )
            try:
                ensure_inside(context.workspace, allowed)
            except PathGuardError as exc:
                raise HTTPException(
                    status_code=403,
                    detail=f"workspace 不在允许的工作区根内：{exc}",
                ) from exc
        return context

    def key(self, context: AgentContext, thread_id: str) -> tuple[str, str]:
        return (_safe_tenant(context.tenant), thread_id)

    def event_key(self, context: AgentContext, thread_id: str) -> str:
        return f"{_safe_tenant(context.tenant)}:{thread_id}"

    def runtime_for(
        self, context: AgentContext, thread_id: str, *, create: bool = True
    ) -> AgentRuntime | None:
        key = self.key(context, thread_id)
        runtime = self.runs.get(key)
        if runtime is None and create:
            runtime = self.agent_factory.new(context)
            self.runs[key] = runtime
        return runtime

    async def drop_runtime(
        self, key: tuple[str, str], runtime: AgentRuntime
    ) -> None:
        if self.runs.get(key) is runtime:
            self.runs.pop(key, None)
        try:
            await runtime.aclose()
        except Exception:  # noqa: BLE001
            pass

    def append(self, state: RunState, event: Event) -> None:
        state.seq += 1
        state.events.append((state.seq, event))
        state.chars += len(event.model_dump_json())
        while (
            state.chars > _STREAM_BUFFER_MAX_CHARS
            and len(state.events) > 1
        ):
            _, old = state.events.popleft()
            state.chars -= len(old.model_dump_json())
        while len(state.events) > _STREAM_BUFFER_MAX_EVENTS:
            _, old = state.events.popleft()
            state.chars -= len(old.model_dump_json())
        state.updated = time.monotonic()
        try:
            self.transport.publish(
                f"{state.key[0]}:{state.thread_id}",
                state.seq,
                event.model_dump_json(),
            )
        except Exception:  # noqa: BLE001
            pass
        for queue in list(state.subscribers):
            try:
                queue.put_nowait((state.seq, event))
            except asyncio.QueueFull:
                pass

    async def consume(
        self, state: RunState, events: AsyncIterator[Event]
    ) -> None:
        try:
            async for event in events:
                if _is_suspended(event):
                    state.suspended = True
                self.append(state, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            self.append(
                state,
                RunFailed(message=f"{type(exc).__name__}: {exc}"),
            )
        finally:
            if not state.suspended:
                await self.drop_runtime(state.key, state.runtime)
            state.done = True
            state.updated = time.monotonic()
            for queue in list(state.subscribers):
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    async def start(
        self,
        context: AgentContext,
        thread_id: str,
        runtime: AgentRuntime,
        events: AsyncIterator[Event],
    ) -> RunState:
        key = self.key(context, thread_id)
        old = self.states.pop(key, None)
        base_seq = old.seq if old is not None else 0
        try:
            base_seq = max(
                base_seq,
                self.transport.latest_seq(
                    self.event_key(context, thread_id)
                ),
            )
        except Exception:  # noqa: BLE001
            pass
        state = RunState(
            key=key,
            thread_id=thread_id,
            runtime=runtime,
            events=deque(),
            seq=base_seq,
        )
        state.task = asyncio.create_task(self.consume(state, events))
        self.states[key] = state
        self.runs[key] = runtime
        return state

    def buffered_sse(
        self, state: RunState, *, after: int = 0
    ) -> StreamingResponse:
        return StreamingResponse(
            _buffered_stream(state, after),
            media_type="text/event-stream",
            headers=sse_headers(),
        )

    def transport_sse(
        self, event_key: str, *, after: int = 0
    ) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            last = max(0, int(after))
            idle = silent = 0.0
            while True:
                try:
                    rows = await asyncio.to_thread(
                        self.transport.replay, event_key, last
                    )
                except Exception:  # noqa: BLE001
                    return
                for seq, payload in rows:
                    if seq <= last:
                        continue
                    last = seq
                    idle = silent = 0.0
                    yield f"id: {seq}\ndata: {payload}\n\n"
                    if _terminal_payload(payload):
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
            headers=sse_headers(),
        )

    def stream_guard(
        self, context: AgentContext, thread_id: str
    ) -> RunState | None:
        state = self.states.get(self.key(context, thread_id))
        if state is not None and not state.done:
            raise HTTPException(
                status_code=409, detail="该会话已有运行中的事件流"
            )
        return state

    def sandbox_ready(self) -> tuple[bool, str]:
        """执行后端是否可用；结果按沙箱自带的 TTL 缓存，不会每次探测都起进程。"""
        if not self._probe_sandbox["built"]:
            self._probe_sandbox["built"] = True
            try:
                from ubismart.sandbox.backend import build_sandbox

                self._probe_sandbox["instance"] = build_sandbox(
                    self.settings
                )
            except Exception as exc:  # noqa: BLE001 - 构建失败即未就绪
                self._probe_sandbox["error"] = (
                    f"{type(exc).__name__}: {exc}"
                )
                return False, "build_failed"
        sandbox = self._probe_sandbox["instance"]
        if sandbox is None:
            return False, "build_failed"
        try:
            return (
                bool(sandbox.health()),
                "ok" if sandbox.health() else "unhealthy",
            )
        except Exception as exc:  # noqa: BLE001 - 探测异常即未就绪
            self._probe_sandbox["error"] = f"{type(exc).__name__}: {exc}"
            return False, "probe_error"

    async def close_runtime(self, runtime: AgentRuntime) -> None:
        try:
            await runtime.aclose()
        except Exception:  # noqa: BLE001
            pass

    def reject_active(self, context: AgentContext, thread_id: str) -> None:
        state = self.states.get(self.key(context, thread_id))
        if state is not None and not state.done:
            raise HTTPException(
                status_code=409,
                detail="该会话正在运行或挂起；请先处理",
            )

    def audit_dir(self, context: AgentContext) -> Path:
        return Path(
            self.agent_factory.tenant_settings(
                context
            ).resolved_audit_dir
        )


def _make_lifespan(acceptor: Any):
    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # 信号处理器在独立线程里调用 handle_exit，必须拿到跑在事件循环里的
        # loop 才能把 drain 协程排进去。
        _app.state.loop = asyncio.get_running_loop()
        if acceptor is not None:
            acceptor.start()
        try:
            yield
        finally:
            if acceptor is not None:
                acceptor.stop()

    return _lifespan


def _build_server_components(settings: Settings) -> tuple[Any, Any]:
    transport = build_event_transport(settings)
    tls = None
    server_fingerprint = ""
    if settings.host_broker_tls_cert and settings.host_broker_tls_key:
        tls = ServerTlsConfig(
            cert=settings.host_broker_tls_cert,
            key=settings.host_broker_tls_key,
            ca=settings.host_broker_tls_ca,
            require_client_cert=True,
        )
        server_fingerprint = certificate_fingerprint(
            settings.host_broker_tls_cert
        )
    elif settings.host_broker_require_tls:
        raise ValueError("AGENT_HOST_BROKER_REQUIRE_TLS 需要证书与私钥")
    device_registry = None
    policy = str(settings.host_broker_device_policy or "off").lower()
    if settings.host_broker_enabled and policy == "enforce":
        device_registry = DeviceRegistry(
            settings.host_broker_device_registry or None,
            enforce=True,
        )
        if device_registry.load_error:
            raise ValueError(
                f"设备台账不可用：{device_registry.load_error}"
            )
    acceptor = (
        HostBrokerAcceptor(
            host=settings.host_broker_listen_host,
            port=settings.host_broker_listen_port,
            client_id="a3",
            heartbeat_s=settings.host_broker_heartbeat_s,
            tls=tls,
            require_encrypted=settings.host_broker_require_tls or bool(tls),
            a2l_id=settings.host_a2l_id,
            server_fingerprint=server_fingerprint,
            device_registry=device_registry,
            device_reload_s=settings.host_broker_device_reload_s,
        )
        if settings.host_broker_enabled
        else None
    )
    return transport, acceptor


def _register_healthz(app: FastAPI) -> None:
    @app.get("/healthz")
    async def healthz() -> dict:
        """公开存活探针；只暴露版本，不返回配置或工作区。

        **存活 ≠ 就绪**：这个端点只说明进程在。依赖（执行后端、checkpoint）
        挂没挂看 `/readyz` —— 否则编排/反代会把"进程活着但沙箱已死"的实例
        继续放进流量。
        """
        return {
            "status": "ok",
            "service": "agent-server",
            "version": __version__,
            "api_version": AGENT_API_VERSION,
        }


def _register_readyz(app: FastAPI, handlers: _AgentAppHandlers) -> None:
    @app.get("/readyz")
    async def readyz(request: Request) -> Any:
        """就绪探针：依赖不健康就返回 503，让编排把实例摘出流量。"""
        handlers.require_token(request)
        settings = handlers.settings
        checkpoint_persistent = str(
            settings.checkpoint_path or ""
        ).strip() != ":memory:"
        sandbox_ok, sandbox_state = await asyncio.to_thread(
            handlers.sandbox_ready
        )
        checks = {
            "sandbox": sandbox_state,
            "checkpoint": (
                "persistent" if checkpoint_persistent else "in_memory"
            ),
            "draining": "true" if app.state.draining else "false",
        }
        # draining 时即使依赖健康也不再判定就绪：让编排把流量摘走后再退出。
        ready = sandbox_ok and not app.state.draining
        payload = {
            "status": "ready" if ready else "not_ready",
            "checks": checks,
        }
        if not ready:
            return JSONResponse(status_code=503, content=payload)
        return payload


def _register_metrics(app: FastAPI, handlers: _AgentAppHandlers) -> None:
    @app.get("/metrics")
    async def metrics(request: Request) -> PlainTextResponse:
        """Prometheus 文本导出；需要内部 token（指标含运行与失败计数）。"""
        handlers.require_token(request)
        from ubismart.platform.observability import render

        return PlainTextResponse(
            render(), media_type="text/plain; version=0.0.4; charset=utf-8"
        )


def _register_meta(app: FastAPI, handlers: _AgentAppHandlers) -> None:
    @app.get("/v1/meta")
    async def meta(request: Request) -> dict:
        """A2 握手：需要内部 token，不要求 X-Agent-Context。"""
        handlers.require_token(request)
        return {
            "service": "agent-server",
            "version": __version__,
            "api_version": AGENT_API_VERSION,
            "execution_backend": str(
                handlers.settings.execution_backend or ""
            ),
            "capabilities": {
                group: list(items)
                for group, items in AGENT_CAPABILITIES.items()
            },
        }


def _register_host_workspace(
    app: FastAPI, handlers: _AgentAppHandlers
) -> None:
    @app.post("/v1/host-workspaces/register")
    async def register_host_workspace(request: Request) -> dict[str, Any]:
        # 注册请求还没有目录：workspace 为空，路径校验发生在后续真正带
        # workspace 的请求上（那里同样会走 ensure_inside）。
        context = handlers.context(
            request,
            require_host_handle=False,
            require_workspace_root=False,
        )
        acceptor = app.state.host_broker_acceptor
        if acceptor is None:
            raise HTTPException(status_code=503, detail="A2L 未启用")
        caller = acceptor.caller_for(context.a2l_id)
        if caller is None:
            raise HTTPException(
                status_code=503, detail="A2L 未连接"
            )
        try:
            data = await asyncio.to_thread(
                caller.call,
                "host.workspace.register",
                workspace={},
                payload={},
            )
        except Exception as exc:  # noqa: BLE001 - 边界转 HTTP
            raise HTTPException(
                status_code=502,
                detail=f"本机工作区注册失败：{type(exc).__name__}: {exc}",
            ) from exc
        return {"a2l_id": context.a2l_id, **dict(data or {})}


def create_agent_app(
    settings: Settings | None = None,
    *,
    token: str = "",
    runtime_factory: Any = AgentRuntime,
    hmac_required: bool | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    expected = str(token or os.environ.get("AGENT_INTERNAL_TOKEN", "")).strip()
    if not expected:
        raise ValueError("AGENT_INTERNAL_TOKEN 未配置，拒绝启动无鉴权的 Agent Server")
    # `hmac_required` 由入口（main）按绑定地址推导；直接构造 app 时回退配置。
    context_hmac_required = (
        bool(settings.agent_internal_hmac_required)
        if hmac_required is None
        else bool(hmac_required)
    )
    if context_hmac_required and not str(
        settings.agent_internal_hmac_key or ""
    ).strip():
        raise ValueError(
            "已要求 X-Agent-Context 验签，但 AGENT_INTERNAL_HMAC_KEY 为空"
        )

    transport, acceptor = _build_server_components(settings)
    agent_factory = AgentRuntimeFactory(
        settings,
        runtime_factory,
        host_broker_acceptor=acceptor,
    )

    app = FastAPI(
        title="UBISmart Agent Server",
        docs_url=None,
        redoc_url=None,
        lifespan=_make_lifespan(acceptor),
    )
    from ubismart.platform.observability import configure_tracing
    from ubismart.platform.tracing import TracingMiddleware

    otel_tracer = configure_tracing(settings)
    if otel_tracer is not None:
        app.add_middleware(TracingMiddleware, tracer=otel_tracer)

    app.state.transport = transport
    app.state.host_broker_acceptor = acceptor
    # 收到第一次 SIGTERM 后置真：不再接新运行，/readyz 报未就绪。
    app.state.draining = False
    handlers = _AgentAppHandlers(
        app,
        settings,
        expected,
        context_hmac_required,
        agent_factory,
        transport,
    )
    app.state.runs = handlers.runs
    app.state.states = handlers.states
    app.state.drain = handlers.drain

    _register_healthz(app)
    _register_readyz(app, handlers)
    _register_metrics(app, handlers)
    _register_meta(app, handlers)
    _register_host_workspace(app, handlers)

    register_lifecycle_routes(
        app,
        LifecycleDeps(
            app=app,
            context=handlers.context,
            history_context=lambda request: handlers.context(
                request, require_workspace_root=False
            ),
            require_token=handlers.require_token,
            drain_guard=handlers.drain_guard,
            key=handlers.key,
            event_key=handlers.event_key,
            runtime_for=handlers.runtime_for,
            new_runtime=agent_factory.new,
            start=handlers.start,
            buffered_sse=handlers.buffered_sse,
            transport_sse=handlers.transport_sse,
            stream_guard=handlers.stream_guard,
            reject_active=handlers.reject_active,
            states=handlers.states,
            runs=handlers.runs,
            transport=transport,
        ),
    )

    register_workspace_routes(
        app,
        WorkspaceDeps(
            context=handlers.context,
            new_runtime=agent_factory.new,
            close_runtime=handlers.close_runtime,
            reject_active=handlers.reject_active,
            settings=settings,
        ),
    )

    register_import_routes(
        app,
        ImportDeps(
            context=handlers.context,
            new_runtime=agent_factory.new,
            close_runtime=handlers.close_runtime,
            settings=settings,
        ),
    )

    register_state_routes(
        app,
        StateDeps(
            context=handlers.context,
            new_runtime=agent_factory.new,
            close_runtime=handlers.close_runtime,
            reject_active=handlers.reject_active,
            audit_dir=handlers.audit_dir,
            states=handlers.states,
        ),
    )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="UBISmart Agent Server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--token", default="")
    parser.add_argument(
        "--token-stdin",
        action="store_true",
        help="从 stdin 读取内部 token（避免出现在进程列表）",
    )
    parser.add_argument("--log-level", default="warning")
    args = parser.parse_args()
    token = args.token or os.environ.get("AGENT_INTERNAL_TOKEN", "")
    if not token and args.token_stdin:
        token = sys.stdin.readline().strip()
    if not token.strip():
        print("必须设置 AGENT_INTERNAL_TOKEN 或传 --token", file=sys.stderr)
        raise SystemExit(2)
    settings = get_settings()
    from ubismart.platform.observability import configure_logging

    configure_logging(
        level=args.log_level,
        fmt=str(getattr(settings, "log_format", "text") or "text"),
    )
    loopback = args.host in {"127.0.0.1", "::1", "localhost"}
    # 非 loopback = A3 会被别的机器直接访问：只靠 Bearer token 不够 —— 拿到
    # token 的人可以伪造 X-Agent-Context 里的 tenant / workspace / a2l_id。
    # 这种情况必须有 HMAC 验签，否则拒绝启动。
    hmac_required = bool(settings.agent_internal_hmac_required) or not loopback
    if hmac_required and not str(
        settings.agent_internal_hmac_key or ""
    ).strip():
        print(
            "非 loopback 绑定（或 AGENT_INTERNAL_HMAC_REQUIRED=true）必须配置 "
            "AGENT_INTERNAL_HMAC_KEY：否则内部 token 持有者可伪造 Agent context",
            file=sys.stderr,
        )
        raise SystemExit(2)
    app = create_agent_app(
        settings, token=token, hmac_required=hmac_required
    )
    print(
        json.dumps(
            {
                "event": "coding_agent_agent_server",
                "host": args.host,
                "port": args.port,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    drain_timeout = int(getattr(settings, "drain_timeout_s", 60) or 0)
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
        # 先由下面的 handle_exit 等运行自然结束；耗尽后再给 uvicorn
        # 一点时间把在途 HTTP 响应收尾。
        timeout_graceful_shutdown=drain_timeout or None,
    )
    server = uvicorn.Server(config)
    original_handle_exit = server.handle_exit

    def _request_drain() -> None:
        """第一次退出请求：停止接单，等待运行结束；由信号与 /v1/shutdown 共用。"""
        if app.state.draining:
            return
        app.state.draining = True
        loop = getattr(app.state, "loop", None)
        if loop is None:
            # 尚未进入事件循环（例如启动失败）：退回直接退出。
            server.should_exit = True
            return

        async def _finish() -> None:
            result = await app.state.drain(drain_timeout)
            print(
                json.dumps(
                    {"event": "agent_server_drained", **result},
                    ensure_ascii=False,
                ),
                flush=True,
            )
            server.should_exit = True

        # 信号处理器可能在非 loop 线程触发；统一回到事件循环里建任务。
        loop.call_soon_threadsafe(lambda: loop.create_task(_finish()))

    def handle_exit(sig: Any, frame: Any) -> None:
        if app.state.draining:
            # 第二次信号：不再等待，交给 uvicorn 原有逻辑强制退出。
            original_handle_exit(sig, frame)
            return
        _request_drain()

    server.handle_exit = handle_exit
    # Desktop supervisor 调用的 /v1/shutdown 也走同一条 drain 路径。
    app.state.shutdown = _request_drain
    server.run()


if __name__ == "__main__":
    main()
