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
from coding_agent.session.run_compare import build_runs, compare_runs


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

    @app.get("/v1/sessions/{thread_id}/patch")
    async def session_patch(thread_id: str, request: Request) -> dict:
        """本轮改动的合并补丁。返回文本而非文件：A2 决定怎么呈现（下载/预览）。"""
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            return {"patch": runtime.session_patch(thread_id)}
        finally:
            await deps.close_runtime(runtime)

    @app.get("/v1/sessions/{thread_id}/runs")
    async def session_runs(
        thread_id: str, request: Request, limit: int = 20
    ) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            records = read_records_many(
                runtime.audit_files, thread_id=thread_id, limit=5000
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            await deps.close_runtime(runtime)
        return {
            "runs": build_runs(
                [record.model_dump(mode="json") for record in records],
                limit=max(1, min(50, int(limit))),
            )
        }

    @app.get("/v1/sessions/{thread_id}/runs/compare")
    async def compare_session_runs(
        thread_id: str,
        request: Request,
        left: int,
        right: int,
        max_files: int = 30,
    ) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        size = max(1, min(50, int(max_files)))
        try:
            records = read_records_many(
                runtime.audit_files, thread_id=thread_id, limit=5000
            )
            try:
                data = compare_runs(
                    build_runs(
                        [
                            record.model_dump(mode="json")
                            for record in records
                        ],
                        limit=1000,
                    ),
                    left,
                    right,
                )
            except KeyError as exc:
                raise HTTPException(
                    status_code=404, detail=str(exc)
                ) from exc
            files = data["files"]
            truncated = max(0, len(files) - size)
            for item in files[:size]:
                for side in ("left", "right"):
                    info = item.get(side)
                    if not info or not info.get("snapshot_id"):
                        continue
                    try:
                        info["diff"] = runtime.diff_snapshot(
                            path=item["path"],
                            snapshot_id=info["snapshot_id"],
                        )[:200_000]
                    except Exception:  # noqa: BLE001
                        info["diff"] = ""
            data["files"] = files[:size]
            data["truncated_files"] = truncated
            return data
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
        approval_checks = 0
        for info in infos:
            state = deps.states.get(
                (_safe_tenant(context.tenant), info.thread_id)
            )
            status = ""
            if state is not None and state.suspended:
                status = "paused"
            elif state is not None and not state.done:
                status = "running"
            elif info.interrupted:
                status = "interrupted"
            if not status:
                continue
            approvals = 0
            if (
                status in {"paused", "interrupted"}
                and approval_checks < 10
            ):
                approval_checks += 1
                temporary = None
                try:
                    temporary = deps.new_runtime(context)
                    found = await temporary.pending_approvals(
                        info.thread_id
                    )
                    approvals = len(found)
                except Exception:  # noqa: BLE001 - 单个会话失败不影响列表
                    approvals = 0
                finally:
                    if temporary is not None:
                        try:
                            await deps.close_runtime(temporary)
                        except Exception:  # noqa: BLE001
                            pass
            items.append(
                {
                    "thread_id": info.thread_id,
                    "title": info.title or info.thread_id,
                    "workspace": info.workspace,
                    "last_active": info.last_seen,
                    "status": status,
                    "recoverable": status in {"paused", "interrupted"},
                    "approvals": approvals,
                }
            )
        return {"items": items}
