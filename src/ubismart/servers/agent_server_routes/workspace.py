"""A3 工作区路由：文件/Git/项目、快照/恢复、上传。"""

from __future__ import annotations

import asyncio
import glob
import re
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, HTTPException, Request, Response, UploadFile

from ubismart.agent_protocol import AgentContext
from ubismart.config import Settings
from ubismart.execution.file_access import LocalFileAccess
from ubismart.execution.host_file_access import HostFileAccess
from ubismart.identity.project_env import probe_project
from ubismart.servers.agent_server_models import DeleteUploadBody, RestoreBody
from ubismart.tools.documents import DocumentLimits
from ubismart.workspace.browse import (
    MAX_READ_BYTES,
    BrowseError,
    git_summary,
    list_directory,
    read_text_file,
    search_workspace,
)
from ubismart.workspace.file_preview import (
    INLINE_MIME_ALLOWLIST,
    RAW_MAX_BYTES,
    PreviewError,
    raw_response_parts,
    read_document,
    read_raw,
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


def _host_access(app: FastAPI, context: AgentContext) -> HostFileAccess:
    acceptor = getattr(app.state, "host_broker_acceptor", None)
    if acceptor is None:
        raise HTTPException(status_code=503, detail="A2L 未启用")
    caller = acceptor.caller_for(context.a2l_id)
    if caller is None:
        raise HTTPException(status_code=503, detail="A2L 未连接")
    return HostFileAccess(
        caller,
        workspace_id=context.workspace_id,
        root_handle=context.root_handle,
    )


def _host_error(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=502,
        detail=f"本机工作区读取失败：{type(exc).__name__}: {exc}",
    )


def register_workspace_routes(app: FastAPI, deps: WorkspaceDeps) -> None:
    @app.get("/v1/files")
    async def files(request: Request, path: str = "") -> dict:
        context = deps.context(request)
        if context.workspace_kind == "host":
            access = _host_access(app, context)
            try:
                rows = await asyncio.to_thread(
                    access.list_dir, path, limit=1000
                )
            except Exception as exc:  # noqa: BLE001 - 边界转 HTTP
                raise _host_error(exc) from exc
            entries = []
            for kind, item_path in rows:
                name = item_path.rsplit("/", 1)[-1] or item_path
                entries.append(
                    {
                        "name": name,
                        "path": item_path,
                        "is_dir": kind == "d",
                        "size": 0,
                    }
                )
            entries.sort(
                key=lambda item: (
                    not item["is_dir"], item["name"].lower()
                )
            )
            return {
                "path": path.strip("/"),
                "entries": entries[:200],
                "truncated": len(entries) > 200,
            }
        try:
            return await asyncio.to_thread(
                list_directory, context.workspace, path
            )
        except BrowseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/file")
    async def file_content(request: Request, path: str) -> dict:
        context = deps.context(request)
        if context.workspace_kind == "host":
            access = _host_access(app, context)
            try:
                info = await asyncio.to_thread(access.stat, path)
                if not info.get("is_file"):
                    raise HTTPException(status_code=422, detail="不是文件")
                text = await asyncio.to_thread(
                    access.read_text, path, max_bytes=MAX_READ_BYTES
                )
            except HTTPException:
                raise
            except Exception as exc:  # noqa: BLE001 - 边界转 HTTP
                raise _host_error(exc) from exc
            return {
                "path": path.strip("/"),
                "size": int(info.get("size") or 0),
                "truncated": False,
                "binary": False,
                "content": text,
            }
        try:
            return await asyncio.to_thread(
                read_text_file, context.workspace, path
            )
        except BrowseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    def _access(app: FastAPI, context: AgentContext):
        if context.workspace_kind == "host":
            return _host_access(app, context)
        return LocalFileAccess(context.workspace)

    @app.get("/v1/file/raw")
    async def file_raw(request: Request, path: str) -> Response:
        context = deps.context(request)
        access = _access(app, context)
        try:
            raw = await asyncio.to_thread(
                read_raw,
                access,
                path,
                max_bytes=min(
                    RAW_MAX_BYTES,
                    int(getattr(deps.settings, "document_max_bytes", RAW_MAX_BYTES)),
                ),
                allow_mime=INLINE_MIME_ALLOWLIST,
            )
        except PreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - host/A2L 错误转 HTTP
            raise _host_error(exc) from exc
        status, headers, body = raw_response_parts(
            raw,
            request.headers.get("range", ""),
            request.headers.get("if-none-match", ""),
        )
        return Response(
            content=body,
            status_code=status,
            media_type=raw.mime,
            headers=headers,
        )

    @app.get("/v1/file/document")
    async def file_document(
        request: Request, path: str, include_images: bool = False
    ) -> dict:
        context = deps.context(request)
        access = _access(app, context)
        limits = DocumentLimits(
            max_bytes=int(
                getattr(deps.settings, "document_max_bytes", 20 * 1024 * 1024)
            ),
            max_pages=int(getattr(deps.settings, "document_max_pages", 200)),
            max_cells=int(getattr(deps.settings, "document_max_cells", 20_000)),
            max_chars=int(
                getattr(deps.settings, "document_max_chars", 2_000_000)
            ),
        )
        try:
            model = await asyncio.to_thread(
                read_document,
                access,
                path,
                limits=limits,
                include_images=include_images,
            )
        except PreviewError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - host/A2L 错误转 HTTP
            raise _host_error(exc) from exc
        return model.model_dump()

    @app.get("/v1/search")
    async def search(
        request: Request, q: str = "", limit: int = 50
    ) -> dict:
        context = deps.context(request)
        text = q.strip()
        cap = max(1, min(int(limit), 100))
        if not text:
            return {"query": "", "results": [], "truncated": False}
        if context.workspace_kind == "host":
            access = _host_access(app, context)
            pattern = f"*{glob.escape(text)}*"
            try:
                files = await asyncio.to_thread(
                    access.find_files, pattern, limit=cap
                )
            except Exception as exc:  # noqa: BLE001 - 边界转 HTTP
                raise _host_error(exc) from exc
            return {
                "query": text,
                "results": [
                    {"path": path, "line": 0, "preview": "", "kind": "path"}
                    for path in files[:cap]
                ],
                "truncated": len(files) >= cap,
                "content_search": False,
            }
        try:
            result = await asyncio.to_thread(
                search_workspace, context.workspace, text, limit=cap
            )
        except BrowseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        result["content_search"] = True
        return result

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
