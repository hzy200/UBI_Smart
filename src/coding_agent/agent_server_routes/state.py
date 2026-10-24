"""A3 状态路由：checkpoint/fork/rewind、记忆、审计、会话索引、恢复。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from coding_agent.agent_protocol import AgentContext
from coding_agent.agent_server_models import MemoryBody
from coding_agent.agent_server_support import _safe_tenant
from coding_agent.audit.logger import audit_files_by_day, read_records_many
from coding_agent.memory.sessions import SessionIndex


@dataclass(frozen=True)
class StateDeps:
    context: Callable[[Request], AgentContext]
    new_runtime: Callable[[AgentContext], Any]
    close_runtime: Callable[[Any], Awaitable[None]]
    reject_active: Callable[[AgentContext, str], None]
    audit_dir: Callable[[AgentContext], Path]
    states: dict


def _audit_paths(
    context: AgentContext, audit_dir: Callable[[AgentContext], Path]
) -> list[Path]:
    by_day = audit_files_by_day(audit_dir(context))
    return [
        path
        for day in sorted(by_day)
        for path in by_day[day]
    ]


def register_state_routes(app: FastAPI, deps: StateDeps) -> None:
    @app.get("/v1/run_started/{thread_id}")
    async def run_started(thread_id: str, request: Request) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            return {
                "prefix": runtime.last_run_started_at(thread_id)
            }
        finally:
            await deps.close_runtime(runtime)

    @app.get("/v1/checkpoints/{thread_id}")
    async def checkpoints(
        thread_id: str, request: Request, limit: int = 20
    ) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            found = await runtime.checkpoints(
                thread_id, limit=max(1, limit)
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            await deps.close_runtime(runtime)
        return {"checkpoints": found}

    @app.post("/v1/fork/{thread_id}")
    async def fork(
        thread_id: str,
        request: Request,
        checkpoint_id: str = "",
    ) -> dict:
        context = deps.context(request)
        deps.reject_active(context, thread_id)
        runtime = deps.new_runtime(context)
        try:
            target = await runtime.fork(
                thread_id, checkpoint_id=checkpoint_id
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            await deps.close_runtime(runtime)
        return {"thread_id": target}

    @app.post("/v1/rewind/{thread_id}")
    async def rewind(
        thread_id: str,
        request: Request,
        steps: int = 1,
        checkpoint_id: str = "",
    ) -> dict:
        context = deps.context(request)
        deps.reject_active(context, thread_id)
        runtime = deps.new_runtime(context)
        try:
            found = await runtime.rewind(
                thread_id,
                checkpoint_id=checkpoint_id,
                steps=max(1, steps),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            await deps.close_runtime(runtime)
        return {"checkpoint_id": found}

    @app.get("/v1/memory")
    async def memory(request: Request, scope: str = "project") -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            return {"facts": runtime.memories(scope=scope)}
        finally:
            await deps.close_runtime(runtime)

    @app.post("/v1/memory")
    async def memory_write(body: MemoryBody, request: Request) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        internal = body.thread_id
        try:
            if body.action == "remember":
                facts = runtime.remember(
                    body.text, thread_id=internal, scope=body.scope
                )
            elif body.action == "forget":
                facts = runtime.forget(
                    body.index, thread_id=internal, scope=body.scope
                )
            elif body.action == "clear":
                facts = runtime.clear_memory(
                    thread_id=internal, scope=body.scope
                )
            elif body.action == "pin":
                facts = runtime.pin_memory(
                    body.index,
                    pinned=body.pinned,
                    thread_id=internal,
                    scope=body.scope,
                )
            else:
                facts = runtime.set_memory_priority(
                    body.index,
                    body.priority,
                    thread_id=internal,
                    scope=body.scope,
                )
        except (IndexError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            await deps.close_runtime(runtime)
        return {"facts": facts}

    @app.get("/v1/audit")
    async def audit(
        request: Request, thread_id: str = "", limit: int = 100
    ) -> dict:
        context = deps.context(request)
        records = await asyncio.to_thread(
            read_records_many,
            _audit_paths(context, deps.audit_dir),
            thread_id=thread_id or None,
            limit=max(1, limit),
        )
        return {
            "records": [
                record.model_dump(mode="json") for record in records
            ]
        }

    @app.get("/v1/sessions")
    async def sessions(
        request: Request, limit: int = 50, q: str = ""
    ) -> dict:
        context = deps.context(request)
        infos = SessionIndex(deps.audit_dir(context)).list(limit=0)
        query = q.strip().lower()
        rows = [
            {
                "thread_id": info.thread_id,
                "title": info.title,
                "workspace": info.workspace,
                "last_active": info.last_seen,
                "prompts": info.prompts,
                "tool_calls": info.tool_calls,
                "runs": info.runs,
                "interrupted": info.interrupted,
            }
            for info in infos
            if not query
            or query in info.title.lower()
            or query in info.thread_id.lower()
            or query in info.workspace.lower()
        ]
        return {"sessions": rows[: max(1, limit)]}

    @app.get("/v1/recovery")
    async def recovery(request: Request) -> dict:
        context = deps.context(request)
        infos = SessionIndex(deps.audit_dir(context)).list(limit=50)
        items: list[dict] = []
        for info in infos:
            state = deps.states.get(
                (_safe_tenant(context.tenant), info.thread_id)
            )
            status = ""
            if state is not None and not state.done:
                status = "running"
            elif state is not None and state.suspended:
                status = "paused"
            elif info.interrupted:
                status = "interrupted"
            if not status:
                continue
            items.append(
                {
                    "thread_id": info.thread_id,
                    "title": info.title or info.thread_id,
                    "workspace": info.workspace,
                    "last_active": info.last_seen,
                    "status": status,
                    "approvals": 0,
                }
            )
        return {"items": items}
