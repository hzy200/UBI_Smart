"""A2 资料库路由：认证、限额、异步任务代理与本地执行。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from ubismart.config import Settings
from ubismart.identity.tenancy import WebTenant
from ubismart.library.models import (
    LibraryError,
    LibraryNotFound,
    LibraryQuotaExceeded,
    LibraryTooLarge,
    SupportedFormatError,
)
from ubismart.web.auth import AuthUser
from ubismart.web.dependencies import UserDep

_LOG = logging.getLogger(__name__)
_TASKS: set[asyncio.Task] = set()

# 请求体中间件按显式路径放行上传；/api/v1 未重写前也要匹配。
LIBRARY_UPLOAD_PATHS = ("/api/library/items", "/api/v1/library/items")


@dataclass(frozen=True)
class LibraryDeps:
    settings: Settings
    tenant: Callable[[AuthUser], WebTenant]
    rate_limit: Callable[[AuthUser], None]
    agent_client: Any
    agent_library_upload: Callable[..., Awaitable[dict]]
    agent_library_json: Callable[..., Awaitable[dict]]
    ops_runtime: Callable[[WebTenant, Any], Any]


class LibraryTagsBody(BaseModel):
    tags: list[str] = Field(default_factory=list, max_length=20)


def _ensure_enabled(deps: LibraryDeps) -> None:
    if not deps.settings.library_enabled:
        raise HTTPException(status_code=404, detail="资料库未启用")


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, LibraryNotFound):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (LibraryTooLarge, LibraryQuotaExceeded)):
        return HTTPException(status_code=413, detail=str(exc))
    if isinstance(exc, (SupportedFormatError, LibraryError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(
        status_code=503,
        detail=f"{type(exc).__name__}: {exc}",
    )


async def _local(
    deps: LibraryDeps,
    tenant: WebTenant,
    call: Callable[[Any], Any],
) -> dict:
    runtime = deps.ops_runtime(tenant, None)
    try:
        return await asyncio.to_thread(call, runtime)
    finally:
        await runtime.aclose()


def register_library_routes(app: FastAPI, deps: LibraryDeps) -> None:
    semaphore = asyncio.Semaphore(
        max(1, int(deps.settings.library_max_concurrent_jobs))
    )

    async def _run_local_job(runtime: Any, job_id: str) -> None:
        async with semaphore:
            try:
                await asyncio.to_thread(runtime.library_process_job, job_id)
            except Exception as exc:  # noqa: BLE001
                _LOG.exception("local library job %s failed", job_id)
                try:
                    await asyncio.to_thread(
                        runtime.library_fail_job,
                        job_id,
                        f"{type(exc).__name__}: {exc}",
                    )
                except Exception:  # noqa: BLE001
                    _LOG.exception(
                        "local library job %s could not be failed", job_id
                    )
            finally:
                await runtime.aclose()

    def _schedule(runtime: Any, job_id: str) -> None:
        task = asyncio.create_task(_run_local_job(runtime, job_id))
        _TASKS.add(task)
        task.add_done_callback(_TASKS.discard)

    @app.post("/api/library/items", status_code=202)
    async def upload_items(
        user: UserDep,
        files: Annotated[list[UploadFile], File()],
    ) -> dict:
        deps.rate_limit(user)
        _ensure_enabled(deps)
        settings = deps.settings
        limit_files = max(1, int(settings.web_max_upload_files))
        if len(files) > limit_files:
            raise HTTPException(
                status_code=413,
                detail=f"单次最多上传 {limit_files} 个文件",
            )
        tenant = deps.tenant(user)
        items: list[dict] = []
        jobs: list[dict] = []

        if deps.agent_client is not None:
            for item in files:
                data = await item.read()
                if len(data) > settings.web_max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=f"{item.filename} 超过单文件上限",
                    )
                result = await deps.agent_library_upload(
                    user,
                    tenant,
                    item.filename or "upload.bin",
                    data,
                    item.content_type or "application/octet-stream",
                )
                items.extend(result.get("items") or [])
                jobs.extend(result.get("jobs") or [])
            return {"items": items, "jobs": jobs}

        for item in files:
            data = await item.read()
            if len(data) > settings.web_max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"{item.filename} 超过单文件上限",
                )
            runtime = deps.ops_runtime(tenant, None)
            try:
                started = await asyncio.to_thread(
                    runtime.library_start_ingest,
                    item.filename or "upload.bin",
                    data,
                    content_type=item.content_type or "",
                )
            except HTTPException:
                await runtime.aclose()
                raise
            except Exception as exc:  # noqa: BLE001
                await runtime.aclose()
                raise _http_error(exc) from exc
            item_row = started.get("item")
            if isinstance(item_row, dict):
                items.append(item_row)
            job = started.get("job")
            if isinstance(job, dict):
                jobs.append(job)
                _schedule(runtime, str(job["job_id"]))
            else:
                await runtime.aclose()
        return {"items": items, "jobs": jobs}

    @app.get("/api/library/search")
    async def search_library(
        user: UserDep,
        q: str,
        top_k: int = 8,
        mode: str = "",
        tags: str = "",
    ) -> dict:
        deps.rate_limit(user)
        _ensure_enabled(deps)
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            return await deps.agent_library_json(
                user,
                tenant,
                "GET",
                "/v1/library/search",
                params={
                    "q": q,
                    "top_k": str(top_k),
                    "mode": mode,
                    "tags": tags,
                },
            )
        try:
            return await _local(
                deps,
                tenant,
                lambda rt: {
                    "query": q,
                    "hits": rt.library_search(
                        q,
                        top_k=top_k,
                        mode=mode,
                        tags=[item for item in tags.split(",") if item],
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.get("/api/library/items")
    async def list_items(
        user: UserDep,
        limit: int = 50,
        offset: int = 0,
        status: str = "",
        tag: str = "",
        q: str = "",
    ) -> dict:
        deps.rate_limit(user)
        _ensure_enabled(deps)
        tenant = deps.tenant(user)
        params = {
            "limit": str(limit),
            "offset": str(offset),
            "status": status,
            "tag": tag,
            "q": q,
        }
        if deps.agent_client is not None:
            return await deps.agent_library_json(
                user, tenant, "GET", "/v1/library/items", params=params
            )
        try:
            return await _local(
                deps,
                tenant,
                lambda rt: rt.library_list(
                    limit=limit,
                    offset=offset,
                    status=status,
                    tag=tag,
                    q=q,
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.get("/api/library/items/{item_id}")
    async def get_item(item_id: str, user: UserDep) -> dict:
        deps.rate_limit(user)
        _ensure_enabled(deps)
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            return await deps.agent_library_json(
                user, tenant, "GET", f"/v1/library/items/{item_id}"
            )
        try:
            return await _local(
                deps, tenant, lambda rt: rt.library_get(item_id)
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.get("/api/library/items/{item_id}/detail")
    async def get_detail(
        item_id: str, user: UserDep, chunk_limit: int = 20
    ) -> dict:
        deps.rate_limit(user)
        _ensure_enabled(deps)
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            return await deps.agent_library_json(
                user,
                tenant,
                "GET",
                f"/v1/library/items/{item_id}/detail",
                params={"chunk_limit": str(chunk_limit)},
            )
        try:
            return await _local(
                deps,
                tenant,
                lambda rt: rt.library_detail(
                    item_id, chunk_limit=chunk_limit
                ),
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.patch("/api/library/items/{item_id}")
    async def set_tags(
        item_id: str, body: LibraryTagsBody, user: UserDep
    ) -> dict:
        deps.rate_limit(user)
        _ensure_enabled(deps)
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            return await deps.agent_library_json(
                user,
                tenant,
                "PATCH",
                f"/v1/library/items/{item_id}",
                json_body={"tags": body.tags},
            )
        try:
            return await _local(
                deps,
                tenant,
                lambda rt: rt.library_set_tags(item_id, body.tags),
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.delete("/api/library/items/{item_id}")
    async def delete_item(
        item_id: str, user: UserDep, purge: bool = False
    ) -> dict:
        deps.rate_limit(user)
        _ensure_enabled(deps)
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            return await deps.agent_library_json(
                user,
                tenant,
                "DELETE",
                f"/v1/library/items/{item_id}",
                params={"purge": str(bool(purge)).lower()},
            )
        try:
            return await _local(
                deps,
                tenant,
                lambda rt: rt.library_delete(item_id, purge=purge),
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.post("/api/library/items/{item_id}/reindex", status_code=202)
    async def reindex_item(item_id: str, user: UserDep) -> dict:
        deps.rate_limit(user)
        _ensure_enabled(deps)
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            return await deps.agent_library_json(
                user,
                tenant,
                "POST",
                f"/v1/library/items/{item_id}/reindex",
            )
        runtime = deps.ops_runtime(tenant, None)
        try:
            started = await asyncio.to_thread(
                runtime.library_start_reindex, item_id
            )
        except Exception as exc:  # noqa: BLE001
            await runtime.aclose()
            raise _http_error(exc) from exc
        job = started.get("job")
        if isinstance(job, dict):
            _schedule(runtime, str(job["job_id"]))
        else:
            await runtime.aclose()
        return started

    @app.post("/api/library/reindex-all", status_code=202)
    async def reindex_all(user: UserDep) -> dict:
        deps.rate_limit(user)
        _ensure_enabled(deps)
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            return await deps.agent_library_json(
                user, tenant, "POST", "/v1/library/reindex-all"
            )
        runtime = deps.ops_runtime(tenant, None)
        try:
            started = await asyncio.to_thread(
                runtime.library_start_reindex_all
            )
        except Exception as exc:  # noqa: BLE001
            await runtime.aclose()
            raise _http_error(exc) from exc
        job = started.get("job")
        if isinstance(job, dict):
            _schedule(runtime, str(job["job_id"]))
        else:
            await runtime.aclose()
        return started

    @app.get("/api/library/jobs")
    async def list_jobs(user: UserDep) -> dict:
        deps.rate_limit(user)
        _ensure_enabled(deps)
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            return await deps.agent_library_json(
                user, tenant, "GET", "/v1/library/jobs"
            )
        try:
            return await _local(
                deps, tenant, lambda rt: rt.library_list_jobs()
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.get("/api/library/jobs/{job_id}")
    async def get_job(job_id: str, user: UserDep) -> dict:
        deps.rate_limit(user)
        _ensure_enabled(deps)
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            return await deps.agent_library_json(
                user, tenant, "GET", f"/v1/library/jobs/{job_id}"
            )
        try:
            return await _local(
                deps, tenant, lambda rt: rt.library_get_job(job_id)
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc

    @app.post("/api/library/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str, user: UserDep) -> dict:
        deps.rate_limit(user)
        _ensure_enabled(deps)
        tenant = deps.tenant(user)
        if deps.agent_client is not None:
            return await deps.agent_library_json(
                user,
                tenant,
                "POST",
                f"/v1/library/jobs/{job_id}/cancel",
            )
        try:
            return await _local(
                deps, tenant, lambda rt: rt.library_cancel_job(job_id)
            )
        except Exception as exc:  # noqa: BLE001
            raise _http_error(exc) from exc
