"""A3 资料库路由：异步上传/重建任务、状态、取消与检索。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from ubismart.agent_protocol import AgentContext
from ubismart.library.models import (
    LibraryError,
    LibraryNotFound,
    LibraryQuotaExceeded,
    LibraryTooLarge,
    SupportedFormatError,
)

_LOG = logging.getLogger(__name__)
_TASKS: set[asyncio.Task] = set()


@dataclass(frozen=True)
class LibraryDeps:
    context: Callable[[Request], AgentContext]
    new_runtime: Callable[[AgentContext], Any]
    close_runtime: Callable[[Any], Awaitable[None]]
    max_concurrent_jobs: int = 2


class LibraryTagsBody(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=20)


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LibraryNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (LibraryTooLarge, LibraryQuotaExceeded)):
        return HTTPException(status_code=413, detail=str(exc))
    if isinstance(exc, (SupportedFormatError, LibraryError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(
        status_code=500,
        detail=f"{type(exc).__name__}: {exc}",
    )


def register_library_routes(app: FastAPI, deps: LibraryDeps) -> None:
    semaphore = asyncio.Semaphore(max(1, int(deps.max_concurrent_jobs)))

    async def _run_job(runtime: Any, job_id: str) -> None:
        async with semaphore:
            try:
                await asyncio.to_thread(runtime.library_process_job, job_id)
            except Exception as exc:  # noqa: BLE001 - 单个任务失败不能拖垮事件循环
                _LOG.exception("library job %s failed", job_id)
                try:
                    await asyncio.to_thread(
                        runtime.library_fail_job,
                        job_id,
                        f"{type(exc).__name__}: {exc}",
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception("library job %s could not be failed", job_id)
            finally:
                await deps.close_runtime(runtime)

    def _schedule(runtime: Any, job_id: str) -> None:
        task = asyncio.create_task(_run_job(runtime, job_id))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)

    @app.post("/v1/library/items", status_code=202)
    async def upload(
        request: Request,
        file: Annotated[UploadFile, File()],
    ) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            started = await asyncio.to_thread(
                runtime.library_start_ingest,
                file.filename or "upload.bin",
                await file.read(),
                content_type=file.content_type or "",
            )
        except Exception as exc:  # noqa: BLE001 - 统一映射成 HTTP 错误
            await deps.close_runtime(runtime)
            raise _http_error(exc) from exc
        job = started.get("job")
        if isinstance(job, dict):
            _schedule(runtime, str(job["job_id"]))
        else:
            await deps.close_runtime(runtime)
        return {
            "items": [started.get("item")],
            "jobs": [job] if isinstance(job, dict) else [],
        }

    @app.get("/v1/library/search")
    async def search(
        request: Request,
        q: str,
        top_k: int = 8,
        mode: str = "",
        tags: str = "",
    ) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            hits = await asyncio.to_thread(
                runtime.library_search,
                q,
                top_k=top_k,
                mode=mode,
                tags=[item for item in tags.split(",") if item],
            )
            return {"query": q, "hits": hits}
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        finally:
            await deps.close_runtime(runtime)

    @app.get("/v1/library/items")
    async def list_items(
        request: Request,
        limit: int = 50,
        offset: int = 0,
        status: str = "",
        tag: str = "",
        q: str = "",
    ) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            return await asyncio.to_thread(
                runtime.library_list,
                limit=limit,
                offset=offset,
                status=status,
                tag=tag,
                q=q,
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        finally:
            await deps.close_runtime(runtime)

    @app.get("/v1/library/items/{item_id}")
    async def get_item(item_id: str, request: Request) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            return await asyncio.to_thread(runtime.library_get, item_id)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        finally:
            await deps.close_runtime(runtime)

    @app.get("/v1/library/items/{item_id}/detail")
    async def get_detail(
        item_id: str, request: Request, chunk_limit: int = 20
    ) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            return await asyncio.to_thread(
                runtime.library_detail, item_id, chunk_limit=chunk_limit
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        finally:
            await deps.close_runtime(runtime)

    @app.patch("/v1/library/items/{item_id}")
    async def set_tags(
        item_id: str, body: LibraryTagsBody, request: Request
    ) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            return await asyncio.to_thread(
                runtime.library_set_tags, item_id, body.tags
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        finally:
            await deps.close_runtime(runtime)

    @app.delete("/v1/library/items/{item_id}")
    async def delete_item(
        item_id: str, request: Request, purge: bool = False
    ) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            return await asyncio.to_thread(
                runtime.library_delete, item_id, purge=purge
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        finally:
            await deps.close_runtime(runtime)

    @app.post("/v1/library/items/{item_id}/reindex", status_code=202)
    async def reindex_item(item_id: str, request: Request) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            started = await asyncio.to_thread(
                runtime.library_start_reindex, item_id
            )
        except Exception as exc:  # noqa: BLE001
            await deps.close_runtime(runtime)
            raise _http_error(exc) from exc
        job = started.get("job")
        if isinstance(job, dict):
            _schedule(runtime, str(job["job_id"]))
        else:
            await deps.close_runtime(runtime)
        return started

    @app.post("/v1/library/reindex-all", status_code=202)
    async def reindex_all(request: Request) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            started = await asyncio.to_thread(
                runtime.library_start_reindex_all
            )
        except Exception as exc:  # noqa: BLE001
            await deps.close_runtime(runtime)
            raise _http_error(exc) from exc
        job = started.get("job")
        if isinstance(job, dict):
            _schedule(runtime, str(job["job_id"]))
        else:
            await deps.close_runtime(runtime)
        return started

    @app.get("/v1/library/jobs")
    async def list_jobs(request: Request) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            return await asyncio.to_thread(runtime.library_list_jobs)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        finally:
            await deps.close_runtime(runtime)

    @app.get("/v1/library/jobs/{job_id}")
    async def get_job(job_id: str, request: Request) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            return await asyncio.to_thread(runtime.library_get_job, job_id)
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        finally:
            await deps.close_runtime(runtime)

    @app.post("/v1/library/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, request: Request) -> dict:
        context = deps.context(request)
        runtime = deps.new_runtime(context)
        try:
            return await asyncio.to_thread(
                runtime.library_cancel_job, job_id
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
        finally:
            await deps.close_runtime(runtime)
