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
import re
import sys
import time
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import uvicorn
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse

from coding_agent import AGENT_API_VERSION, __version__
from coding_agent.agent_context import AgentRuntimeFactory
from coding_agent.agent_protocol import AGENT_CAPABILITIES, AgentContext
from coding_agent.agent_server_models import (
    DeleteUploadBody,
    MemoryBody,
    RestoreBody,
)
from coding_agent.agent_server_routes.lifecycle import (
    LifecycleDeps,
    register_lifecycle_routes,
)
from coding_agent.agent_server_support import (
    RunState,
    _is_suspended,
    _safe_tenant,
    _sse_with_id,
    _terminal_payload,
)
from coding_agent.audit.logger import audit_files_by_day, read_records_many
from coding_agent.config import Settings, get_settings
from coding_agent.event_transport import build_event_transport
from coding_agent.events import (
    Event,
    RunFailed,
)
from coding_agent.memory.sessions import SessionIndex
from coding_agent.project_env import probe_project
from coding_agent.runtime import AgentRuntime
from coding_agent.workspace.browse import (
    BrowseError,
    git_summary,
    list_directory,
    read_text_file,
)

_STREAM_BUFFER_MAX_CHARS = 8 * 1024 * 1024
_STREAM_BUFFER_MAX_EVENTS = 5_000


def create_agent_app(
    settings: Settings | None = None,
    *,
    token: str = "",
    runtime_factory: Any = AgentRuntime,
) -> FastAPI:
    settings = settings or get_settings()
    agent_factory = AgentRuntimeFactory(settings, runtime_factory)
    expected = str(token or os.environ.get("AGENT_INTERNAL_TOKEN", "")).strip()
    if not expected:
        raise ValueError("AGENT_INTERNAL_TOKEN 未配置，拒绝启动无鉴权的 Agent Server")

    app = FastAPI(title="CodingAgent Agent Server", docs_url=None, redoc_url=None)
    runs: dict[tuple[str, str], AgentRuntime] = {}
    states: dict[tuple[str, str], RunState] = {}
    transport = build_event_transport(settings)
    app.state.transport = transport

    def _require_token(request: Request) -> None:
        header = request.headers.get("authorization", "")
        provided = (
            header[7:].strip() if header.lower().startswith("bearer ") else ""
        )
        if not hmac.compare_digest(provided, expected):
            raise HTTPException(status_code=401, detail="内部 token 无效")

    def _context(request: Request) -> AgentContext:
        _require_token(request)
        raw = request.headers.get("x-agent-context", "")
        try:
            context = AgentContext.from_header(raw)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail=f"Agent context 无效：{exc}"
            ) from exc
        if not context.workspace.startswith("/"):
            raise HTTPException(
                status_code=400, detail="workspace 必须是绝对路径"
            )
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
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
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
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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

    @app.get("/v1/files")
    async def files(
        request: Request, path: str = ""
    ) -> dict:
        context = _context(request)
        try:
            return await asyncio.to_thread(
                list_directory, context.workspace, path
            )
        except BrowseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/file")
    async def file_content(request: Request, path: str) -> dict:
        context = _context(request)
        try:
            return await asyncio.to_thread(
                read_text_file, context.workspace, path
            )
        except BrowseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/git")
    async def git(request: Request, diff: bool = False) -> dict:
        context = _context(request)
        try:
            return await asyncio.to_thread(
                git_summary, context.workspace, diff=diff
            )
        except (BrowseError, OSError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/project")
    async def project(request: Request) -> dict:
        context = _context(request)
        result = await asyncio.to_thread(
            probe_project, context.workspace
        )
        result["desktop_mode"] = False
        return result

    # ------------------------------------------------------------------
    # 快照 / checkpoint / 记忆
    # ------------------------------------------------------------------

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

    @app.get("/v1/snapshots")
    async def snapshots(
        request: Request, thread_id: str, limit: int = 20
    ) -> dict:
        context = _context(request)
        runtime = agent_factory.new(context)
        try:
            entries = runtime.list_snapshots(limit=max(1, limit))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            await _close_runtime(runtime)
        return {"snapshots": [asdict(entry) for entry in entries]}

    @app.get("/v1/diff")
    async def diff(
        request: Request,
        thread_id: str,
        path: str = "",
        snapshot_id: str = "",
    ) -> dict:
        context = _context(request)
        runtime = agent_factory.new(context)
        try:
            text = runtime.diff_snapshot(
                path=path or None,
                snapshot_id=snapshot_id or None,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            await _close_runtime(runtime)
        return {"diff": text}

    @app.post("/v1/restore")
    async def restore(body: RestoreBody, request: Request) -> dict:
        context = _context(request)
        _reject_active(context, body.thread_id)
        runtime = agent_factory.new(context)
        try:
            if body.prefix:
                results = runtime.restore_since(
                    body.prefix, dry_run=body.dry_run
                )
                return {"results": [asdict(item) for item in results]}
            result = runtime.restore(
                path=body.path or None,
                snapshot_id=body.snapshot_id or None,
            )
            return asdict(result)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            await _close_runtime(runtime)

    @app.get("/v1/run_started/{thread_id}")
    async def run_started(thread_id: str, request: Request) -> dict:
        context = _context(request)
        runtime = agent_factory.new(context)
        try:
            return {
                "prefix": runtime.last_run_started_at(thread_id)
            }
        finally:
            await _close_runtime(runtime)

    @app.get("/v1/checkpoints/{thread_id}")
    async def checkpoints(
        thread_id: str, request: Request, limit: int = 20
    ) -> dict:
        context = _context(request)
        runtime = agent_factory.new(context)
        try:
            found = await runtime.checkpoints(
                thread_id, limit=max(1, limit)
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            await _close_runtime(runtime)
        return {"checkpoints": found}

    @app.post("/v1/fork/{thread_id}")
    async def fork(
        thread_id: str,
        request: Request,
        checkpoint_id: str = "",
    ) -> dict:
        context = _context(request)
        _reject_active(context, thread_id)
        runtime = agent_factory.new(context)
        try:
            target = await runtime.fork(
                thread_id, checkpoint_id=checkpoint_id
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            await _close_runtime(runtime)
        return {"thread_id": target}

    @app.post("/v1/rewind/{thread_id}")
    async def rewind(
        thread_id: str,
        request: Request,
        steps: int = 1,
        checkpoint_id: str = "",
    ) -> dict:
        context = _context(request)
        _reject_active(context, thread_id)
        runtime = agent_factory.new(context)
        try:
            found = await runtime.rewind(
                thread_id,
                checkpoint_id=checkpoint_id,
                steps=max(1, steps),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        finally:
            await _close_runtime(runtime)
        return {"checkpoint_id": found}

    @app.get("/v1/memory")
    async def memory(request: Request, scope: str = "project") -> dict:
        context = _context(request)
        runtime = agent_factory.new(context)
        try:
            return {"facts": runtime.memories(scope=scope)}
        finally:
            await _close_runtime(runtime)

    @app.post("/v1/memory")
    async def memory_write(body: MemoryBody, request: Request) -> dict:
        context = _context(request)
        runtime = agent_factory.new(context)
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
            await _close_runtime(runtime)
        return {"facts": facts}

    # ------------------------------------------------------------------
    # 审计 / 会话索引 / 恢复 / 上传
    # ------------------------------------------------------------------

    def _audit_dir(context: AgentContext) -> Path:
        return Path(agent_factory.tenant_settings(context).resolved_audit_dir)

    def _audit_paths(context: AgentContext) -> list[Path]:
        by_day = audit_files_by_day(_audit_dir(context))
        return [
            path
            for day in sorted(by_day)
            for path in by_day[day]
        ]

    @app.get("/v1/audit")
    async def audit(
        request: Request, thread_id: str = "", limit: int = 100
    ) -> dict:
        context = _context(request)
        records = await asyncio.to_thread(
            read_records_many,
            _audit_paths(context),
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
        context = _context(request)
        infos = SessionIndex(_audit_dir(context)).list(limit=0)
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
        context = _context(request)
        infos = SessionIndex(_audit_dir(context)).list(limit=50)
        items: list[dict] = []
        for info in infos:
            state = states.get((_safe_tenant(context.tenant), info.thread_id))
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

    def _uploads_dir(context: AgentContext, thread_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_-]", "", thread_id)[:64] or "global"
        return Path(context.workspace) / ".agent" / "uploads" / safe

    @app.post("/v1/uploads/{thread_id}")
    async def uploads(
        thread_id: str,
        request: Request,
        files: Annotated[list[UploadFile], File()],
    ) -> dict:
        context = _context(request)
        if len(files) > max(1, int(settings.web_max_upload_files)):
            raise HTTPException(status_code=413, detail="单次上传文件过多")
        _reject_active(context, thread_id)
        blobs: list[tuple[str, bytes, str]] = []
        for item in files:
            data = await item.read()
            if len(data) > settings.web_max_upload_bytes:
                raise HTTPException(
                    status_code=413, detail="单个文件超过上传上限"
                )
            blobs.append(
                (
                    item.filename or "upload.bin",
                    data,
                    item.content_type or "",
                )
            )
        used = 0
        base = _uploads_dir(context, thread_id)
        if base.is_dir():
            used = sum(
                path.stat().st_size
                for path in base.rglob("*")
                if path.is_file()
            )
        if used + sum(len(data) for _, data, _ in blobs) > (
            settings.web_max_upload_total_bytes
        ):
            raise HTTPException(
                status_code=413, detail="工作区累计上传超过上限"
            )
        runtime = agent_factory.new(context)
        uploaded = []
        try:
            for name, data, content_type in blobs:
                saved = runtime.store_upload(
                    name,
                    data,
                    thread_id=thread_id,
                    max_bytes=settings.web_max_upload_bytes,
                )
                uploaded.append(
                    {
                        **saved,
                        "name": name,
                        "content_type": content_type,
                    }
                )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        finally:
            await _close_runtime(runtime)
        return {"uploads": uploaded}

    @app.get("/v1/uploads/{thread_id}")
    async def list_uploads(thread_id: str, request: Request) -> dict:
        context = _context(request)
        base = _uploads_dir(context, thread_id)
        rows: list[dict] = []
        used = 0
        if base.is_dir():
            for path in sorted(base.rglob("*")):
                if not path.is_file():
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    continue
                used += size
                rows.append(
                    {
                        "relative_path": path.relative_to(
                            Path(context.workspace)
                        ).as_posix(),
                        "bytes": size,
                    }
                )
        return {
            "uploads": rows,
            "used_bytes": used,
            "max_file_bytes": settings.web_max_upload_bytes,
            "max_files_per_request": settings.web_max_upload_files,
            "max_total_bytes": settings.web_max_upload_total_bytes,
        }

    @app.delete("/v1/uploads/{thread_id}")
    async def delete_upload(
        thread_id: str, body: DeleteUploadBody, request: Request
    ) -> dict:
        context = _context(request)
        base = _uploads_dir(context, thread_id)
        rel = body.path.strip().replace("\\", "/")
        root = Path(context.workspace).resolve()
        try:
            target = (root / rel).resolve()
            target.relative_to(base.resolve())
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="路径越界") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        size = target.stat().st_size
        target.unlink()
        return {"ok": True, "bytes": size}

    register_lifecycle_routes(
        app,
        LifecycleDeps(
            app=app,
            context=_context,
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
