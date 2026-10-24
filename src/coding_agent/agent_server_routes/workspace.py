"""A3 工作区路由：文件/Git/项目、快照/恢复、上传。"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile

from coding_agent.agent_protocol import AgentContext
from coding_agent.agent_server_models import DeleteUploadBody, RestoreBody
from coding_agent.config import Settings
from coding_agent.project_env import probe_project
from coding_agent.workspace.browse import (
    BrowseError,
    git_summary,
    list_directory,
    read_text_file,
)


@dataclass(frozen=True)
class WorkspaceDeps:
    context: Callable[[Request], AgentContext]
    new_runtime: Callable[[AgentContext], Any]
    close_runtime: Callable[[Any], Awaitable[None]]
    reject_active: Callable[[AgentContext, str], None]
    settings: Settings


def _uploads_dir(context: AgentContext, thread_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", thread_id)[:64] or "global"
    return Path(context.workspace) / ".agent" / "uploads" / safe


def register_workspace_routes(app: FastAPI, deps: WorkspaceDeps) -> None:
    @app.get("/v1/files")
    async def files(request: Request, path: str = "") -> dict:
        context = deps.context(request)
        try:
            return await asyncio.to_thread(
                list_directory, context.workspace, path
            )
        except BrowseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/file")
    async def file_content(request: Request, path: str) -> dict:
        context = deps.context(request)
        try:
            return await asyncio.to_thread(
                read_text_file, context.workspace, path
            )
        except BrowseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/git")
    async def git(request: Request, diff: bool = False) -> dict:
        context = deps.context(request)
        try:
            return await asyncio.to_thread(
                git_summary, context.workspace, diff=diff
            )
        except (BrowseError, OSError) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    @app.get("/v1/project")
    async def project(request: Request) -> dict:
        context = deps.context(request)
        result = await asyncio.to_thread(
            probe_project, context.workspace
        )
        result["desktop_mode"] = False
        return result

    @app.get("/v1/snapshots")
    async def snapshots(
        request: Request, thread_id: str, limit: int = 20
    ) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            entries = runtime.list_snapshots(limit=max(1, limit))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            await deps.close_runtime(runtime)
        return {"snapshots": [asdict(entry) for entry in entries]}

    @app.get("/v1/diff")
    async def diff(
        request: Request,
        thread_id: str,
        path: str = "",
        snapshot_id: str = "",
    ) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            text = runtime.diff_snapshot(
                path=path or None,
                snapshot_id=snapshot_id or None,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        finally:
            await deps.close_runtime(runtime)
        return {"diff": text}

    @app.post("/v1/restore")
    async def restore(body: RestoreBody, request: Request) -> dict:
        context = deps.context(request)
        deps.reject_active(context, body.thread_id)
        runtime = deps.new_runtime(context)
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
            await deps.close_runtime(runtime)

    @app.post("/v1/uploads/{thread_id}")
    async def uploads(
        thread_id: str,
        request: Request,
        files: Annotated[list[UploadFile], File()],
    ) -> dict:
        context = deps.context(request)
        settings = deps.settings
        if len(files) > max(1, int(settings.web_max_upload_files)):
            raise HTTPException(status_code=413, detail="单次上传文件过多")
        deps.reject_active(context, thread_id)
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
        runtime = deps.new_runtime(context)
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
            await deps.close_runtime(runtime)
        return {"uploads": uploaded}

    @app.get("/v1/uploads/{thread_id}")
    async def list_uploads(thread_id: str, request: Request) -> dict:
        context = deps.context(request)
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
        settings = deps.settings
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
        context = deps.context(request)
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
