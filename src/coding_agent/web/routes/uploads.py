"""会话上传路由：配额检查、落盘与元数据。"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.tenancy import WebTenant
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import UserDep
from coding_agent.web.validation import check_thread_id


class UploadDeleteRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)


@dataclass(frozen=True)
class UploadDeps:
    settings: Settings
    tenant: Callable[[AuthUser], WebTenant]
    rate_limit: Callable[[AuthUser], None]
    config_for: Callable[..., Any]
    set_config: Callable[[AuthUser, WebTenant, Any], None]
    reject_active: Callable[[AuthUser, str], None]
    reject_deleted: Callable[[AuthUser, str], None]
    agent_client: Any
    agent_upload: Callable[..., Awaitable[dict[str, Any]]]
    agent_json: Callable[..., Awaitable[dict[str, Any]]]
    ops_runtime: Callable[[WebTenant, Any], Any]
    workspace_uploaded_bytes: Callable[[AuthUser, str], int]


def _upload_rows(
    workspace: str, internal: str
) -> tuple[list[dict[str, Any]], int]:
    base = Path(workspace) / ".agent" / "uploads" / internal
    rows: list[dict[str, Any]] = []
    total = 0
    if not base.is_dir():
        return rows, total
    for path in sorted(base.rglob("*"))[:500]:
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        total += size
        rows.append(
            {
                "relative_path": path.relative_to(workspace).as_posix(),
                "bytes": size,
            }
        )
    return rows, total


def register_upload_routes(app: FastAPI, deps: UploadDeps) -> None:
    @app.post("/api/sessions/{thread_id}/uploads")
    async def upload_files(
        thread_id: str,
        user: UserDep,
        files: Annotated[list[UploadFile], File()],
    ) -> dict:
        """上传写入 `.agent/uploads`，不授予模型修改源码的能力。"""
        deps.rate_limit(user)
        public_thread = check_thread_id(thread_id)
        deps.reject_active(user, public_thread)
        settings = deps.settings
        if len(files) > max(1, int(settings.web_max_upload_files)):
            raise HTTPException(
                status_code=413,
                detail=f"单次最多上传 {settings.web_max_upload_files} 个文件",
            )
        tenant = deps.tenant(user)
        config = deps.config_for(
            user, tenant, public_thread, create=False
        )
        if config is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if deps.agent_client is not None:
            payload: list[tuple[str, bytes, str]] = []
            total = 0
            for item in files:
                data = await item.read()
                if len(data) > settings.web_max_upload_bytes:
                    raise HTTPException(
                        status_code=413, detail="单个文件超过上传上限"
                    )
                total += len(data)
                payload.append(
                    (
                        item.filename or "upload.bin",
                        data,
                        item.content_type or "application/octet-stream",
                    )
                )
            current = deps.workspace_uploaded_bytes(
                user, config.workspace
            )
            if current + total > settings.web_max_upload_total_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        "工作区累计上传超过上限 "
                        f"{settings.web_max_upload_total_bytes} 字节"
                    ),
                )
            result = await deps.agent_upload(
                user, tenant, config, public_thread, payload
            )
            added = sum(
                int(item.get("bytes", 0))
                for item in result.get("uploads", [])
            )
            config.uploaded_bytes += added
            deps.set_config(user, tenant, config)
            return result

        runtime = deps.ops_runtime(tenant, config)
        internal = tenant.thread_id(public_thread)
        uploaded = []
        before_bytes = config.uploaded_bytes
        total_limit = settings.web_max_upload_total_bytes
        known_sizes: list[int] | None = []
        for item in files:
            size = getattr(item, "size", None)
            if size is None:
                known_sizes = None
                break
            if size > settings.web_max_upload_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=(
                        f"{item.filename} 超过上限 "
                        f"{settings.web_max_upload_bytes} 字节"
                    ),
                )
            known_sizes.append(int(size))
        if known_sizes is not None:
            current = deps.workspace_uploaded_bytes(
                user, config.workspace
            )
            if current + sum(known_sizes) > total_limit:
                raise HTTPException(
                    status_code=413,
                    detail=f"工作区累计上传超过上限 {total_limit} 字节",
                )
        try:
            for item in files:
                data = await item.read()
                if len(data) > settings.web_max_upload_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"{item.filename} 超过上限 "
                            f"{settings.web_max_upload_bytes} 字节"
                        ),
                    )
                if (
                    deps.workspace_uploaded_bytes(
                        user, config.workspace
                    )
                    + len(data)
                    > total_limit
                ):
                    raise HTTPException(
                        status_code=413,
                        detail=f"工作区累计上传超过上限 {total_limit} 字节",
                    )
                try:
                    saved = runtime.store_upload(
                        item.filename or "upload.bin",
                        data,
                        thread_id=internal,
                        max_bytes=settings.web_max_upload_bytes,
                    )
                except ValueError as exc:
                    raise HTTPException(
                        status_code=422, detail=str(exc)
                    ) from exc
                except Exception as exc:  # noqa: BLE001 - 沙箱/路径错误要可读报出
                    raise HTTPException(
                        status_code=503,
                        detail=f"{type(exc).__name__}: {exc}",
                    ) from exc
                config.uploaded_bytes += len(data)
                uploaded.append(
                    {
                        **saved,
                        "name": item.filename or "upload.bin",
                        "content_type": item.content_type or "",
                    }
                )
        finally:
            if config.uploaded_bytes != before_bytes:
                deps.set_config(user, tenant, config)
            await runtime.aclose()
        return {"uploads": uploaded}

    @app.get("/api/sessions/{thread_id}/uploads")
    async def list_uploads(thread_id: str, user: UserDep) -> dict:
        public_thread = check_thread_id(thread_id)
        deps.reject_deleted(user, public_thread)
        tenant = deps.tenant(user)
        config = deps.config_for(
            user, tenant, public_thread, create=False
        )
        if deps.agent_client is not None:
            data = await deps.agent_json(
                user, tenant, config, "GET",
                f"/v1/uploads/{public_thread}",
            )
            data["accounted_bytes"] = (
                config.uploaded_bytes if config else 0
            )
            data["workspace_bytes"] = deps.workspace_uploaded_bytes(
                user,
                config.workspace if config else tenant.workspace,
            )
            return data
        workspace = config.workspace if config else tenant.workspace
        internal = tenant.thread_id(public_thread)
        rows, actual = await asyncio.to_thread(
            _upload_rows, workspace, internal
        )
        settings = deps.settings
        return {
            "uploads": rows,
            "used_bytes": actual,
            "accounted_bytes": config.uploaded_bytes if config else 0,
            "max_file_bytes": settings.web_max_upload_bytes,
            "max_files_per_request": settings.web_max_upload_files,
            "max_total_bytes": settings.web_max_upload_total_bytes,
            "workspace_bytes": deps.workspace_uploaded_bytes(
                user, workspace
            ),
        }

    @app.delete("/api/sessions/{thread_id}/uploads")
    async def delete_upload(
        thread_id: str,
        request: UploadDeleteRequest,
        user: UserDep,
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        deps.reject_active(user, public_thread)
        tenant = deps.tenant(user)
        config = deps.config_for(
            user, tenant, public_thread, create=False
        )
        if config is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        if deps.agent_client is not None:
            result = await deps.agent_json(
                user, tenant, config, "DELETE",
                f"/v1/uploads/{public_thread}",
                json_body={"path": request.path},
            )
            config.uploaded_bytes = max(
                0,
                config.uploaded_bytes - int(result.get("bytes", 0)),
            )
            deps.set_config(user, tenant, config)
            return result
        internal = tenant.thread_id(public_thread)
        prefix = f".agent/uploads/{internal}/"
        rel = request.path.strip().replace("\\", "/")
        if not rel.startswith(prefix):
            raise HTTPException(status_code=422, detail="只能删除本会话的上传")
        root = Path(config.workspace).resolve()
        try:
            target = (root / rel).resolve()
            target.relative_to(root)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="路径越界") from exc
        if not target.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        size = target.stat().st_size
        target.unlink()
        config.uploaded_bytes = max(0, config.uploaded_bytes - size)
        deps.set_config(user, tenant, config)
        return {"ok": True, "bytes": size}
