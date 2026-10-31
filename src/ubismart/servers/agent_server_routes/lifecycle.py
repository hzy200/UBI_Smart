"""A3 运行生命周期路由。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

from coding_agent.agent_protocol import AgentContext
from coding_agent.servers.agent_server_models import (
    CompactBody,
    ResumeBody,
    RunBody,
    SteerBody,
)


@dataclass(frozen=True)
class LifecycleDeps:
    app: FastAPI
    context: Callable[[Request], AgentContext]
    require_token: Callable[[Request], None]
    key: Callable[[AgentContext, str], tuple[str, str]]
    event_key: Callable[[AgentContext, str], str]
    runtime_for: Callable[..., Any]
    new_runtime: Callable[[AgentContext], Any]
    start: Callable[..., Awaitable[Any]]
    buffered_sse: Callable[..., StreamingResponse]
    transport_sse: Callable[..., StreamingResponse]
    stream_guard: Callable[..., Any]
    reject_active: Callable[[AgentContext, str], None]
    states: dict
    runs: dict
    transport: Any
    # 历史读取不依赖工作区是否在注册表根内；旧会话仍可回看。
    history_context: Callable[[Request], AgentContext] | None = None
    # 优雅退出时拒绝新运行；读接口（status/stream/history）仍放行。
    drain_guard: Callable[[], None] = lambda: None


def register_lifecycle_routes(app: FastAPI, deps: LifecycleDeps) -> None:
    @app.post("/v1/runs")
    async def run(body: RunBody, request: Request) -> StreamingResponse:
        deps.drain_guard()
        context = deps.context(request)
        thread_id = body.thread_id
        deps.stream_guard(context, thread_id)
        runtime = deps.new_runtime(context)
        return deps.buffered_sse(
            await deps.start(
                context,
                thread_id,
                runtime,
                runtime.run(body.prompt, thread_id=thread_id),
            )
        )

    @app.get("/v1/stream/{thread_id}")
    async def stream_events(
        thread_id: str, request: Request, after: int = 0
    ) -> Response:
        context = deps.context(request)
        start = max(0, int(after))
        header = request.headers.get("last-event-id", "")
        try:
            start = max(start, int(header))
        except (TypeError, ValueError):
            pass
        state = deps.states.get(deps.key(context, thread_id))
        if state is not None:
            try:
                latest = deps.transport.latest_seq(
                    deps.event_key(context, thread_id)
                )
            except Exception:  # noqa: BLE001
                latest = state.seq
            if latest > state.seq:
                return deps.transport_sse(
                    deps.event_key(context, thread_id), after=start
                )
            if state.done and start >= state.seq:
                return Response(status_code=204)
            return deps.buffered_sse(state, after=start)
        try:
            latest = deps.transport.latest_seq(
                deps.event_key(context, thread_id)
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503, detail="事件传输不可用"
            ) from exc
        if latest <= start:
            return Response(status_code=204)
        return deps.transport_sse(
            deps.event_key(context, thread_id), after=start
        )

    @app.post("/v1/resume/{thread_id}")
    async def resume(
        thread_id: str, body: ResumeBody, request: Request
    ) -> StreamingResponse:
        deps.drain_guard()
        context = deps.context(request)
        deps.stream_guard(context, thread_id)
        runtime = deps.runtime_for(context, thread_id)
        assert runtime is not None
        return deps.buffered_sse(
            await deps.start(
                context,
                thread_id,
                runtime,
                runtime.resume(thread_id, body.decisions),
            )
        )

    @app.post("/v1/resume_steering/{thread_id}")
    async def resume_steering(
        thread_id: str, request: Request
    ) -> StreamingResponse:
        deps.drain_guard()
        context = deps.context(request)
        deps.stream_guard(context, thread_id)
        runtime = deps.runtime_for(context, thread_id)
        assert runtime is not None
        if not runtime.is_paused(thread_id):
            raise HTTPException(
                status_code=409, detail="该会话未处于 steering 暂停状态"
            )
        return deps.buffered_sse(
            await deps.start(
                context,
                thread_id,
                runtime,
                runtime.resume_steering(thread_id),
            )
        )

    @app.post("/v1/steer/{thread_id}")
    async def steer(
        thread_id: str, body: SteerBody, request: Request
    ) -> dict:
        context = deps.context(request)
        runtime = deps.runs.get(deps.key(context, thread_id))
        if runtime is None:
            raise HTTPException(status_code=404, detail="会话不在运行")
        runtime.steer(
            thread_id,
            body.action,
            text=body.text,
            source="web",
        )
        return {"ok": True, "thread_id": thread_id, "action": body.action}

    @app.post("/v1/cancel_all")
    async def cancel_all(request: Request) -> dict:
        """桌面关窗/升级前协作式取消；仅 A2 持内部 token 调用。"""
        deps.require_token(request)
        items = list(deps.runs.items())
        cancelled = 0
        for (_tenant, thread_id), runtime in items:
            try:
                runtime.steer(thread_id, "cancel", source="desktop")
                cancelled += 1
            except Exception:  # noqa: BLE001 - 单个运行取消失败不阻断其它
                continue
        return {"cancelled": cancelled}

    @app.post("/v1/shutdown")
    async def shutdown(request: Request) -> dict:
        """独立进程的优雅退出入口；由 Desktop supervisor 调用。"""
        deps.require_token(request)
        callback = getattr(deps.app.state, "shutdown", None)
        if callable(callback):
            asyncio.get_running_loop().call_soon(callback)
        return {"ok": True}

    @app.get("/v1/approvals/{thread_id}")
    async def approvals(thread_id: str, request: Request) -> dict:
        context = deps.context(request)
        runtime = deps.runtime_for(context, thread_id, create=False)
        temporary = None
        if runtime is None:
            runtime = deps.new_runtime(context)
            temporary = runtime
        try:
            found = await runtime.pending_approvals(thread_id)
            return {
                "approvals": [
                    item.model_dump() if hasattr(item, "model_dump") else item
                    for item in found
                ]
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            if temporary is not None:
                await temporary.aclose()

    @app.get("/v1/status/{thread_id}")
    async def status(thread_id: str, request: Request) -> dict:
        context = deps.context(request)
        state = deps.states.get(deps.key(context, thread_id))
        runtime = deps.runs.get(deps.key(context, thread_id))
        if state is None:
            return {
                "exists": runtime is not None,
                "thread_id": thread_id,
                "paused": False,
                "streaming": False,
                "last_seq": 0,
            }
        return {
            "exists": True,
            "thread_id": thread_id,
            "paused": bool(runtime and runtime.is_paused(thread_id)),
            "streaming": not state.done,
            "last_seq": state.seq,
        }

    @app.post("/v1/compact/{thread_id}")
    async def compact(
        thread_id: str, body: CompactBody, request: Request
    ) -> dict:
        context = deps.context(request)
        deps.reject_active(context, thread_id)
        runtime = deps.runtime_for(context, thread_id)
        assert runtime is not None
        try:
            target = await runtime.compact_thread(
                thread_id, keep_recent=body.keep_recent
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"thread_id": target}

    @app.get("/v1/history/{thread_id}")
    async def history(
        thread_id: str,
        request: Request,
        limit: int | None = None,
        offset: int = 0,
    ) -> dict:
        context = (deps.history_context or deps.context)(request)
        runtime = deps.runtime_for(context, thread_id, create=False)
        temporary = None
        if runtime is None:
            runtime = deps.new_runtime(context)
            temporary = runtime
        try:
            messages = await runtime.history(
                thread_id, limit=limit, offset=offset
            )
            return {
                "messages": [
                    {"role": item.role, "text": item.text}
                    for item in messages
                ]
            }
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            if temporary is not None:
                await temporary.aclose()
