"""独立 Agent Server（A3 + A4，Phase B4）。

A2 只通过内部 token + `X-Agent-Context` 调用本进程；这里持有 AgentRuntime、
checkpoint、工作区与 SSE。默认只绑 loopback，不暴露给浏览器。

启动：

    python -m coding_agent.agent_server --port 8790
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
from fastapi.responses import StreamingResponse

from coding_agent import AGENT_API_VERSION, __version__
from coding_agent.agent_context import AgentRuntimeFactory
from coding_agent.agent_protocol import (
    AGENT_CAPABILITIES,
    AgentContext,
    verify_context,
)
from coding_agent.agent_server_routes.imports import (
    ImportDeps,
    register_import_routes,
)
from coding_agent.agent_server_routes.lifecycle import (
    LifecycleDeps,
    register_lifecycle_routes,
)
from coding_agent.agent_server_routes.state import (
    StateDeps,
    register_state_routes,
)
from coding_agent.agent_server_routes.workspace import (
    WorkspaceDeps,
    register_workspace_routes,
)
from coding_agent.agent_server_support import (
    RunState,
    _is_suspended,
    _safe_tenant,
    _sse_with_id,
    _terminal_payload,
)
from coding_agent.config import Settings, get_settings
from coding_agent.event_transport import build_event_transport
from coding_agent.events import (
    Event,
    RunFailed,
)
from coding_agent.host_broker.acceptor import HostBrokerAcceptor
from coding_agent.host_broker.devices import DeviceRegistry
from coding_agent.host_broker.transport import (
    ServerTlsConfig,
    certificate_fingerprint,
)
from coding_agent.paths import PathGuardError, ensure_inside
from coding_agent.runtime import AgentRuntime
from coding_agent.sse_stream import sse_headers

_STREAM_BUFFER_MAX_CHARS = 8 * 1024 * 1024
_STREAM_BUFFER_MAX_EVENTS = 5_000


def create_agent_app(
    settings: Settings | None = None,
    *,
    token: str = "",
    runtime_factory: Any = AgentRuntime,
) -> FastAPI:
    settings = settings or get_settings()
    expected = str(token or os.environ.get("AGENT_INTERNAL_TOKEN", "")).strip()
    if not expected:
        raise ValueError("AGENT_INTERNAL_TOKEN 未配置，拒绝启动无鉴权的 Agent Server")

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
    agent_factory = AgentRuntimeFactory(
        settings,
        runtime_factory,
        host_broker_acceptor=acceptor,
    )

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        if acceptor is not None:
            acceptor.start()
        try:
            yield
        finally:
            if acceptor is not None:
                acceptor.stop()

    app = FastAPI(
        title="CodingAgent Agent Server",
        docs_url=None,
        redoc_url=None,
        lifespan=_lifespan,
    )
    app.state.transport = transport
    app.state.host_broker_acceptor = acceptor
    runs: dict[tuple[str, str], AgentRuntime] = {}
    states: dict[tuple[str, str], RunState] = {}

    def _require_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        provided = (
            header[7:].strip() if header.lower().startswith("bearer ") else ""
        )
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="内部 token 无效")

    def _context(
        request: Request,
        *,
        require_host_handle: bool = True,
        require_workspace_root: bool = True,
    ) -> AgentContext:
        _require_token(request)
        raw = request.headers.get("x-agent-context", "")
        signature = request.headers.get("x-agent-signature", "")
        if not verify_context(
            raw, signature, settings.agent_internal_hmac_key
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
            if not context.a2l_id:
                raise HTTPException(
                    status_code=400, detail="host 工作区需要 a2l_id"
                )
            if require_host_handle and (
                not context.workspace_id or not context.root_handle
            ):
                raise HTTPException(
                    status_code=400,
                    detail="host 工作区需要 workspace_id/root_handle",
                )
        else:
            if not context.workspace.startswith("/"):
                raise HTTPException(
                    status_code=400, detail="workspace 必须是绝对路径"
                )
        if (
            require_workspace_root
            and settings.workspace_registry
            and context.workspace_kind != "host"
        ):
            allowed = str(settings.workspace_root or "").strip()
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

    def _key(context: AgentContext, thread_id: str) -> tuple[str, str]:
        return (_safe_tenant(context.tenant), thread_id)

    def _event_key(context: AgentContext, thread_id: str) -> str:
        return f"{_safe_tenant(context.tenant)}:{thread_id}"

    def _runtime_for(
        context: AgentContext, thread_id: str, *, create: bool = True
    ) -> AgentRuntime | None:
        key = _key(context, thread_id)
        runtime = runs.get(key)
        if runtime is None and create:
            runtime = agent_factory.new(context)
            runs[key] = runtime
        return runtime

    async def _drop_runtime(
        key: tuple[str, str], runtime: AgentRuntime
    ) -> None:
        if runs.get(key) is runtime:
            runs.pop(key, None)
        try:
            await runtime.aclose()
        except Exception:  # noqa: BLE001
            pass

    def _append(state: RunState, event: Event) -> None:
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
            transport.publish(
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

    async def _consume(
        state: RunState, events: AsyncIterator[Event]
    ) -> None:
        try:
            async for event in events:
                if _is_suspended(event):
                    state.suspended = True
                _append(state, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _append(
                state,
                RunFailed(message=f"{type(exc).__name__}: {exc}"),
            )
        finally:
            if not state.suspended:
                await _drop_runtime(state.key, state.runtime)
            state.done = True
            state.updated = time.monotonic()
            for queue in list(state.subscribers):
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    async def _start(
        context: AgentContext,
        thread_id: str,
        runtime: AgentRuntime,
        events: AsyncIterator[Event],
    ) -> RunState:
        key = _key(context, thread_id)
        old = states.pop(key, None)
        base_seq = old.seq if old is not None else 0
        try:
            base_seq = max(
                base_seq,
                transport.latest_seq(_event_key(context, thread_id)),
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
        state.task = asyncio.create_task(_consume(state, events))
        states[key] = state
        runs[key] = runtime
        return state

    def _buffered_sse(state: RunState, *, after: int = 0) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            last = max(0, int(after))
            queue: asyncio.Queue = asyncio.Queue(maxsize=2_000)
            state.subscribers.append(queue)
            try:
                while True:
                    for seq, event in list(state.events):
                        if seq > last:
                            last = seq
                            yield _sse_with_id(event, seq)
                    if state.done and last >= state.seq:
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15)
                    except TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if item is None:
                        for seq, event in list(state.events):
                            if seq > last:
                                last = seq
                                yield _sse_with_id(event, seq)
                        break
                    seq, event = item
                    if seq > last:
                        last = seq
                        yield _sse_with_id(event, seq)
            finally:
                try:
                    state.subscribers.remove(queue)
                except ValueError:
                    pass

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers=sse_headers(),
        )

    def _transport_sse(event_key: str, *, after: int = 0) -> StreamingResponse:
        async def stream() -> AsyncIterator[str]:
            last = max(0, int(after))
            idle = silent = 0.0
            while True:
                try:
                    rows = await asyncio.to_thread(
                        transport.replay, event_key, last
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

    def _stream_guard(context: AgentContext, thread_id: str) -> RunState | None:
        state = states.get(_key(context, thread_id))
        if state is not None and not state.done:
            raise HTTPException(
                status_code=409, detail="该会话已有运行中的事件流"
            )
        return state

    @app.get("/healthz")
    async def healthz() -> dict:
        """公开存活探针；只暴露版本，不返回配置或工作区。"""
        return {
            "status": "ok",
            "service": "agent-server",
            "version": __version__,
            "api_version": AGENT_API_VERSION,
        }

    @app.get("/v1/meta")
    async def meta(request: Request) -> dict:
        """A2 握手：需要内部 token，不要求 X-Agent-Context。"""
        _require_token(request)
        return {
            "service": "agent-server",
            "version": __version__,
            "api_version": AGENT_API_VERSION,
            "execution_backend": str(settings.execution_backend or ""),
            "capabilities": {
                group: list(items)
                for group, items in AGENT_CAPABILITIES.items()
            },
        }

    async def _close_runtime(runtime: AgentRuntime) -> None:
        try:
            await runtime.aclose()
        except Exception:  # noqa: BLE001
            pass

    def _reject_active(context: AgentContext, thread_id: str) -> None:
        state = states.get(_key(context, thread_id))
        if state is not None and not state.done:
            raise HTTPException(
                status_code=409,
                detail="该会话正在运行或挂起；请先处理",
            )

    def _audit_dir(context: AgentContext) -> Path:
        return Path(agent_factory.tenant_settings(context).resolved_audit_dir)

    @app.post("/v1/host-workspaces/register")
    async def register_host_workspace(request: Request) -> dict[str, Any]:
        context = _context(request, require_host_handle=False)
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

    register_lifecycle_routes(
        app,
        LifecycleDeps(
            app=app,
            context=_context,
            history_context=lambda request: _context(
                request, require_workspace_root=False
            ),
            require_token=_require_token,
            key=_key,
            event_key=_event_key,
            runtime_for=_runtime_for,
            new_runtime=agent_factory.new,
            start=_start,
            buffered_sse=_buffered_sse,
            transport_sse=_transport_sse,
            stream_guard=_stream_guard,
            reject_active=_reject_active,
            states=states,
            runs=runs,
            transport=transport,
        ),
    )

    register_workspace_routes(
        app,
        WorkspaceDeps(
            context=_context,
            new_runtime=agent_factory.new,
            close_runtime=_close_runtime,
            reject_active=_reject_active,
            settings=settings,
        ),
    )

    register_import_routes(
        app,
        ImportDeps(
            context=_context,
            new_runtime=agent_factory.new,
            close_runtime=_close_runtime,
            settings=settings,
        ),
    )

    register_state_routes(
        app,
        StateDeps(
            context=_context,
            new_runtime=agent_factory.new,
            close_runtime=_close_runtime,
            reject_active=_reject_active,
            audit_dir=_audit_dir,
            states=states,
        ),
    )

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="CodingAgent Agent Server")
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
    app = create_agent_app(get_settings(), token=token)
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
    config = uvicorn.Config(
        app,
        host=args.host,
        port=args.port,
        log_level=args.log_level,
        access_log=False,
    )
    server = uvicorn.Server(config)
    app.state.shutdown = lambda: setattr(server, "should_exit", True)
    server.run()


if __name__ == "__main__":
    main()
