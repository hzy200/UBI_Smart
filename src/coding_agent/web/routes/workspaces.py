"""工作区注册表路由（RFC-0001）。

边界：

- tenant / owner 永远取自认证用户，不接受请求体覆盖；
- `root_path` 只用于导入类来源，仍由 `WorkspaceStore` 做白名单校验；
- 注册表未启用时返回 404，不暴露内部状态；
- 本路由只写元数据，不碰工作区文件。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from fastapi import (
    FastAPI,
    File,
    Header,
    HTTPException,
    UploadFile,
)
from pydantic import BaseModel, Field

from coding_agent.agent_protocol import AgentContext
from coding_agent.config import Settings
from coding_agent.control.idempotency import IdempotencyInFlight
from coding_agent.control.workspaces import (
    WorkspaceError,
    WorkspaceNotFound,
)
from coding_agent.paths import PathGuardError, is_within, normalize_root
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import UserDep


class WorkspaceCreateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=120)
    source: Literal[
        "upload", "template", "local_import", "local_mount"
    ] = "upload"
    # 仅 local_import / local_mount 允许显式传入；托管来源由服务端生成。
    root_path: str = Field(default="", max_length=4096)
    access_mode: Literal[
        "read_only", "write_ask", "write_auto"
    ] = "read_only"
    quota_bytes: int = Field(default=0, ge=0)


class WorkspaceUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    access_mode: Literal[
        "read_only", "write_ask", "write_auto"
    ] | None = None
    quota_bytes: int | None = Field(default=None, ge=0)


class WorkspaceImportRequest(BaseModel):
    source_path: str = Field(min_length=1, max_length=4096)
    mode: Literal["copy"] = "copy"
    max_files: int = Field(default=200_000, ge=1)
    max_bytes: int = Field(default=0, ge=0)


class WorkspaceRollbackRequest(BaseModel):
    revision_id: str = Field(min_length=1, max_length=36)


class WorkspacePinRequest(BaseModel):
    pinned: bool = True


@dataclass(frozen=True)
class WorkspaceRegistryDeps:
    rate_limit: Callable[[AuthUser], None]
    store: Any | None
    enabled: bool
    settings: Settings
    agent_client: Any | None = None
    idempotency: Any | None = None


def _require_store(deps: WorkspaceRegistryDeps) -> Any:
    if not deps.enabled:
        raise HTTPException(status_code=404, detail="工作区注册表未启用")
    if deps.store is None:
        raise HTTPException(
            status_code=503,
            detail="工作区注册表需要可用的控制面数据库",
        )
    return deps.store


def _owned(
    deps: WorkspaceRegistryDeps, user: AuthUser, workspace_id: str
) -> dict[str, Any]:
    store = _require_store(deps)
    try:
        row = store.get(workspace_id)
    except WorkspaceNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if (
        row is None
        or row.get("tenant") != user.name
        or row.get("owner") != user.name
    ):
        # 不区分“不存在”和“别人的”，避免枚举。
        raise HTTPException(status_code=404, detail="工作区不存在")
    return row


def _import_roots(settings: Settings) -> tuple[str, ...]:
    found: list[str] = []
    for item in str(settings.workspace_mount_roots or "").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            found.append(normalize_root(text))
        except PathGuardError:
            continue
    return tuple(found)


def _validate_import_source(
    settings: Settings, source: str, *, target: str
) -> str:
    try:
        normalized = normalize_root(str(source or "").strip())
    except PathGuardError as exc:
        raise HTTPException(
            status_code=400, detail=f"导入源路径非法：{exc}"
        ) from exc
    roots = _import_roots(settings)
    if not roots:
        raise HTTPException(
            status_code=400,
            detail="未配置 AGENT_WORKSPACE_MOUNT_ROOTS，禁止导入本地目录",
        )
    if not any(is_within(normalized, root) for root in roots):
        raise HTTPException(
            status_code=403,
            detail=f"导入源不在允许的根内：{normalized}",
        )
    if normalized == target or is_within(target, normalized):
        raise HTTPException(
            status_code=400,
            detail="导入源不能是目标目录自身或其祖先",
        )
    return normalized


def _idem_begin(
    deps: WorkspaceRegistryDeps, key: str, scope: str
) -> dict[str, Any] | None:
    if deps.idempotency is None or not str(key or "").strip():
        return None
    try:
        return deps.idempotency.begin(
            key,
            scope,
            ttl_s=int(deps.settings.idempotency_ttl_s or 600),
        )
    except IdempotencyInFlight as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _idem_finish(
    deps: WorkspaceRegistryDeps, key: str, response: dict[str, Any]
) -> None:
    if deps.idempotency is not None and str(key or "").strip():
        deps.idempotency.finish(
            key,
            response,
            ttl_s=int(deps.settings.idempotency_ttl_s or 600),
        )


def _idem_abort(deps: WorkspaceRegistryDeps, key: str) -> None:
    if deps.idempotency is not None and str(key or "").strip():
        deps.idempotency.abort(key)


def _workspace_context(
    user: AuthUser, row: dict[str, Any]
) -> AgentContext:
    return AgentContext(
        tenant=str(row.get("tenant") or user.name),
        user=user.name,
        workspace=str(row.get("root_path") or ""),
        workspace_id=str(row.get("workspace_id") or ""),
        access_mode=str(row.get("access_mode") or "read_only"),
        allow_write=True,
    )


async def _prune_workspace(
    deps: WorkspaceRegistryDeps,
    user: AuthUser,
    row: dict[str, Any],
) -> list[str]:
    """删除超出保留数的归档；失败只跳过，不影响主流程。"""
    store = deps.store
    client = deps.agent_client
    if store is None or client is None:
        return []
    try:
        candidates = store.revisions_to_prune(row["workspace_id"])
    except WorkspaceError:
        return []
    context = _workspace_context(user, row)
    pruned: list[str] = []
    for item in candidates:
        archive_path = str(item.get("archive_path") or "")
        if archive_path:
            try:
                await client.workspace_archive_delete(
                    context, archive_path
                )
            except Exception:  # noqa: BLE001 - 清理失败保留索引
                continue
        try:
            store.mark_revision_pruned(item["revision_id"])
        except WorkspaceError:
            continue
        pruned.append(item["revision_id"])
    return pruned


def register_workspace_registry_routes(
    app: FastAPI, deps: WorkspaceRegistryDeps
) -> None:
    @app.get("/api/workspaces")
    async def list_workspaces(
        user: UserDep, include_deleted: bool = False
    ) -> dict:
        deps.rate_limit(user)
        store = _require_store(deps)
        rows = store.list(
            user.name,
            owner=user.name,
            include_deleted=include_deleted,
        )
        return {"workspaces": rows}

    @app.post("/api/workspaces")
    async def create_workspace(
        request: WorkspaceCreateRequest,
        user: UserDep,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key")
        ] = "",
    ) -> dict:
        deps.rate_limit(user)
        store = _require_store(deps)
        cached = _idem_begin(
            deps, idempotency_key, "workspace.create"
        )
        if cached is not None:
            return cached
        if request.source not in {"local_import", "local_mount"}:
            if request.root_path:
                raise HTTPException(
                    status_code=400,
                    detail="托管来源的 root_path 由服务端生成，不能手动指定",
                )
        # 本地导入/挂载意味着目录已经存在；托管来源等待后续导入。
        status = (
            "active"
            if request.root_path and request.source in {
                "local_import", "local_mount"
            }
            else "provisioning"
        )
        try:
            row = store.create(
                user.name,
                user.name,
                request.display_name,
                source=request.source,
                root_path=request.root_path or None,
                access_mode=request.access_mode,
                quota_bytes=request.quota_bytes,
                status=status,
            )
        except WorkspaceError as exc:
            _idem_abort(deps, idempotency_key)
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _idem_finish(deps, idempotency_key, row)
        return row

    @app.get("/api/workspaces/{workspace_id}")
    async def get_workspace(workspace_id: str, user: UserDep) -> dict:
        deps.rate_limit(user)
        return _owned(deps, user, workspace_id)

    @app.patch("/api/workspaces/{workspace_id}")
    async def update_workspace(
        workspace_id: str,
        request: WorkspaceUpdateRequest,
        user: UserDep,
    ) -> dict:
        deps.rate_limit(user)
        store = _require_store(deps)
        _owned(deps, user, workspace_id)
        try:
            return store.update(
                workspace_id,
                display_name=request.display_name,
                access_mode=request.access_mode,
                quota_bytes=request.quota_bytes,
            )
        except WorkspaceError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/api/workspaces/{workspace_id}")
    async def delete_workspace(workspace_id: str, user: UserDep) -> dict:
        deps.rate_limit(user)
        store = _require_store(deps)
        _owned(deps, user, workspace_id)
        try:
            return store.delete(workspace_id)
        except WorkspaceError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/api/workspaces/{workspace_id}/imports")
    async def start_import(
        workspace_id: str,
        request: WorkspaceImportRequest,
        user: UserDep,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key")
        ] = "",
    ) -> dict:
        deps.rate_limit(user)
        try:
            store = _require_store(deps)
            row = _owned(deps, user, workspace_id)
            cached = _idem_begin(
                deps, idempotency_key, "workspace.import"
            )
            if cached is not None:
                return cached
            if deps.agent_client is None:
                raise HTTPException(
                    status_code=503, detail="导入需要独立的 Agent Server"
                )
            if row.get("status") not in {
                "provisioning", "failed", "active"
            }:
                raise HTTPException(
                    status_code=409,
                    detail=f"当前状态不可导入：{row.get('status')}",
                )
            source = _validate_import_source(
                deps.settings,
                request.source_path,
                target=str(row.get("root_path") or ""),
            )
            if row.get("status") == "failed":
                store.set_status(workspace_id, "provisioning")
            context = _workspace_context(user, row)
            result = await deps.agent_client.workspace_import_start(
                context,
                source_path=source,
                mode=request.mode,
                quota_bytes=int(row.get("quota_bytes") or 0),
                max_files=request.max_files,
                max_bytes=request.max_bytes,
            )
        except HTTPException:
            _idem_abort(deps, idempotency_key)
            raise
        except Exception as exc:  # noqa: BLE001 - 代理错误统一映射
            _idem_abort(deps, idempotency_key)
            raise HTTPException(
                status_code=502,
                detail=f"Agent Server 导入启动失败：{type(exc).__name__}: {exc}",
            ) from exc
        _idem_finish(deps, idempotency_key, result)
        return result

    @app.get("/api/workspaces/{workspace_id}/imports/{job_id}")
    async def import_status(
        workspace_id: str, job_id: str, user: UserDep
    ) -> dict:
        deps.rate_limit(user)
        store = _require_store(deps)
        row = _owned(deps, user, workspace_id)
        if deps.agent_client is None:
            raise HTTPException(
                status_code=503, detail="导入需要独立的 Agent Server"
            )
        context = _workspace_context(user, row)
        try:
            result = await deps.agent_client.workspace_import_status(
                context, job_id
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=f"Agent Server 导入状态不可用：{type(exc).__name__}: {exc}",
            ) from exc
        if str(result.get("workspace_id") or "") != workspace_id:
            raise HTTPException(status_code=404, detail="导入任务不存在")
        status = str(result.get("status") or "")
        current = store.get(workspace_id)
        if status == "active" and current:
            revision = result.get("revision") or {}
            revision_id = str(revision.get("revision_id") or "")
            if revision_id and store.get_revision(revision_id) is None:
                store.add_revision(
                    workspace_id,
                    revision_id=revision_id,
                    source="local_import",
                    actor=user.name,
                    git_commit=str(revision.get("git_commit") or ""),
                    archive_path=str(
                        revision.get("archive_path") or ""
                    ),
                    archive_sha256=str(
                        revision.get("archive_sha256") or ""
                    ),
                    bytes=int(revision.get("bytes") or 0),
                    file_count=int(revision.get("files") or 0),
                    scan_status="skipped",
                    status="active",
                )
            store.set_usage(
                workspace_id,
                used_bytes=int(
                    revision.get("bytes") or result.get("bytes") or 0
                ),
                file_count=int(
                    revision.get("files") or result.get("files") or 0
                ),
            )
            if current.get("status") == "provisioning":
                store.set_status(workspace_id, "active")
            await _prune_workspace(
                deps, user, store.require(workspace_id)
            )
        elif status in {"failed", "cancelled"} and current:
            if current.get("status") == "provisioning":
                store.set_status(workspace_id, "failed")
        return result

    @app.delete("/api/workspaces/{workspace_id}/imports/{job_id}")
    async def cancel_import(
        workspace_id: str, job_id: str, user: UserDep
    ) -> dict:
        deps.rate_limit(user)
        _require_store(deps)
        row = _owned(deps, user, workspace_id)
        if deps.agent_client is None:
            raise HTTPException(
                status_code=503, detail="导入需要独立的 Agent Server"
            )
        context = _workspace_context(user, row)
        try:
            result = await deps.agent_client.workspace_import_cancel(
                context, job_id
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=f"Agent Server 取消失败：{type(exc).__name__}: {exc}",
            ) from exc
        if str(result.get("workspace_id") or "") != workspace_id:
            raise HTTPException(status_code=404, detail="导入任务不存在")
        return result

    @app.post("/api/workspaces/{workspace_id}/uploads")
    async def start_upload(
        workspace_id: str,
        user: UserDep,
        archive: Annotated[UploadFile, File()],
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key")
        ] = "",
    ) -> dict:
        deps.rate_limit(user)
        tmp_dir = ""
        try:
            store = _require_store(deps)
            row = _owned(deps, user, workspace_id)
            cached = _idem_begin(
                deps, idempotency_key, "workspace.upload"
            )
            if cached is not None:
                return cached
            if deps.agent_client is None:
                raise HTTPException(
                    status_code=503,
                    detail="上传需要独立的 Agent Server",
                )
            if row.get("status") not in {
                "provisioning", "failed", "active"
            }:
                raise HTTPException(
                    status_code=409,
                    detail=f"当前状态不可上传：{row.get('status')}",
                )
            tmp_dir = tempfile.mkdtemp(prefix="workspace-upload-")
            filename = os.path.basename(
                archive.filename or "upload.bin"
            )
            tmp_path = os.path.join(tmp_dir, filename)
            limit = int(
                deps.settings.workspace_upload_max_bytes or 0
            )
            total = 0
            with open(tmp_path, "wb") as output:
                while True:
                    chunk = await archive.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if limit and total > limit:
                        raise HTTPException(
                            status_code=413,
                            detail=f"上传超过上限 {limit} 字节",
                        )
                    output.write(chunk)
            if row.get("status") == "failed":
                store.set_status(workspace_id, "provisioning")
            context = _workspace_context(user, row)
            result = await deps.agent_client.workspace_upload_start(
                context,
                file_path=tmp_path,
                filename=filename,
                content_type=(
                    archive.content_type
                    or "application/octet-stream"
                ),
                quota_bytes=int(row.get("quota_bytes") or 0),
                max_files=int(
                    deps.settings.workspace_upload_max_files or 200_000
                ),
            )
        except HTTPException:
            _idem_abort(deps, idempotency_key)
            raise
        except Exception as exc:  # noqa: BLE001
            _idem_abort(deps, idempotency_key)
            raise HTTPException(
                status_code=502,
                detail=(
                    "Agent Server 上传启动失败："
                    f"{type(exc).__name__}: {exc}"
                ),
            ) from exc
        finally:
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
        _idem_finish(deps, idempotency_key, result)
        return result

    @app.get("/api/workspaces/{workspace_id}/uploads/{job_id}")
    async def upload_status(
        workspace_id: str, job_id: str, user: UserDep
    ) -> dict:
        deps.rate_limit(user)
        store = _require_store(deps)
        row = _owned(deps, user, workspace_id)
        if deps.agent_client is None:
            raise HTTPException(
                status_code=503, detail="上传需要独立的 Agent Server"
            )
        context = _workspace_context(user, row)
        try:
            result = await deps.agent_client.workspace_upload_status(
                context, job_id
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=(
                    "Agent Server 上传状态不可用："
                    f"{type(exc).__name__}: {exc}"
                ),
            ) from exc
        if str(result.get("workspace_id") or "") != workspace_id:
            raise HTTPException(status_code=404, detail="上传任务不存在")
        status = str(result.get("status") or "")
        current = store.get(workspace_id)
        if status == "active" and current:
            revision = result.get("revision") or {}
            revision_id = str(revision.get("revision_id") or "")
            if revision_id and store.get_revision(revision_id) is None:
                store.add_revision(
                    workspace_id,
                    revision_id=revision_id,
                    source="upload",
                    actor=user.name,
                    git_commit=str(revision.get("git_commit") or ""),
                    archive_path=str(
                        revision.get("archive_path") or ""
                    ),
                    archive_sha256=str(
                        revision.get("archive_sha256") or ""
                    ),
                    bytes=int(result.get("bytes") or 0),
                    file_count=int(result.get("files") or 0),
                    scan_status=str(
                        result.get("scan_status") or "skipped"
                    ),
                    status="active",
                )
            store.set_usage(
                workspace_id,
                used_bytes=int(
                    revision.get("bytes") or result.get("bytes") or 0
                ),
                file_count=int(
                    revision.get("files") or result.get("files") or 0
                ),
            )
            if current.get("status") == "provisioning":
                store.set_status(workspace_id, "active")
            pruned = await _prune_workspace(
                deps, user, store.require(workspace_id)
            )
            if pruned:
                result = {**result, "pruned": pruned}
        elif status in {"failed", "cancelled"} and current:
            if current.get("status") == "provisioning":
                store.set_status(workspace_id, "failed")
        return result

    @app.delete("/api/workspaces/{workspace_id}/uploads/{job_id}")
    async def cancel_upload(
        workspace_id: str, job_id: str, user: UserDep
    ) -> dict:
        deps.rate_limit(user)
        _require_store(deps)
        row = _owned(deps, user, workspace_id)
        if deps.agent_client is None:
            raise HTTPException(
                status_code=503, detail="上传需要独立的 Agent Server"
            )
        context = _workspace_context(user, row)
        try:
            result = await deps.agent_client.workspace_upload_cancel(
                context, job_id
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=502,
                detail=(
                    "Agent Server 上传取消失败："
                    f"{type(exc).__name__}: {exc}"
                ),
            ) from exc
        if str(result.get("workspace_id") or "") != workspace_id:
            raise HTTPException(status_code=404, detail="上传任务不存在")
        return result

    @app.get("/api/workspaces/{workspace_id}/revisions")
    async def list_revisions(
        workspace_id: str, user: UserDep, limit: int = 50
    ) -> dict:
        deps.rate_limit(user)
        store = _require_store(deps)
        _owned(deps, user, workspace_id)
        return {
            "revisions": store.list_revisions(
                workspace_id, limit=max(1, min(int(limit), 200))
            )
        }

    @app.post("/api/workspaces/{workspace_id}/rollback")
    async def rollback(
        workspace_id: str,
        request: WorkspaceRollbackRequest,
        user: UserDep,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key")
        ] = "",
    ) -> dict:
        deps.rate_limit(user)
        store = _require_store(deps)
        row = _owned(deps, user, workspace_id)
        if deps.agent_client is None:
            raise HTTPException(
                status_code=503, detail="回滚需要独立的 Agent Server"
            )
        revision = store.get_revision(request.revision_id)
        if (
            revision is None
            or revision["workspace_id"] != workspace_id
        ):
            raise HTTPException(status_code=404, detail="revision 不存在")
        if revision["status"] == "pruned":
            raise HTTPException(
                status_code=409, detail="归档已清理，无法回滚"
            )
        if not revision["git_commit"]:
            raise HTTPException(
                status_code=409, detail="该 revision 没有 git commit"
            )
        cached = _idem_begin(
            deps, idempotency_key, "workspace.rollback"
        )
        if cached is not None:
            return cached
        context = _workspace_context(user, row)
        try:
            result = await deps.agent_client.workspace_rollback(
                context,
                revision_id=request.revision_id,
                git_commit=revision["git_commit"],
            )
        except HTTPException:
            _idem_abort(deps, idempotency_key)
            raise
        except Exception as exc:  # noqa: BLE001
            _idem_abort(deps, idempotency_key)
            raise HTTPException(
                status_code=502,
                detail=f"Agent Server 回滚失败：{type(exc).__name__}: {exc}",
            ) from exc
        pre = result.get("pre_rollback") or {}
        pre_id = str(pre.get("revision_id") or "")
        if pre_id and store.get_revision(pre_id) is None:
            store.add_revision(
                workspace_id,
                revision_id=pre_id,
                source="rollback",
                actor=user.name,
                git_commit=str(pre.get("git_commit") or ""),
                archive_path=str(pre.get("archive_path") or ""),
                archive_sha256=str(pre.get("archive_sha256") or ""),
                bytes=int(pre.get("bytes") or 0),
                file_count=int(pre.get("files") or 0),
                scan_status="skipped",
                status="superseded",
            )
        store.activate_revision(workspace_id, request.revision_id)
        store.set_usage(
            workspace_id,
            used_bytes=int(result.get("bytes") or 0),
            file_count=int(result.get("files") or 0),
        )
        _idem_finish(deps, idempotency_key, result)
        return result

    @app.post(
        "/api/workspaces/{workspace_id}/revisions/{revision_id}/restore"
    )
    async def restore_revision(
        workspace_id: str,
        revision_id: str,
        user: UserDep,
        idempotency_key: Annotated[
            str, Header(alias="Idempotency-Key")
        ] = "",
    ) -> dict:
        deps.rate_limit(user)
        store = _require_store(deps)
        row = _owned(deps, user, workspace_id)
        if deps.agent_client is None:
            raise HTTPException(
                status_code=503, detail="恢复需要独立的 Agent Server"
            )
        revision = store.get_revision(revision_id)
        if (
            revision is None
            or revision["workspace_id"] != workspace_id
        ):
            raise HTTPException(status_code=404, detail="revision 不存在")
        if revision["status"] == "pruned":
            raise HTTPException(
                status_code=409, detail="归档已清理，无法恢复"
            )
        if not revision["archive_path"]:
            raise HTTPException(
                status_code=409, detail="该 revision 没有归档文件"
            )
        cached = _idem_begin(
            deps, idempotency_key, "workspace.restore"
        )
        if cached is not None:
            return cached
        context = _workspace_context(user, row)
        try:
            result = await deps.agent_client.workspace_restore(
                context,
                revision_id=revision_id,
                archive_path=revision["archive_path"],
                archive_sha256=revision["archive_sha256"],
                git_commit=revision["git_commit"],
            )
        except HTTPException:
            _idem_abort(deps, idempotency_key)
            raise
        except Exception as exc:  # noqa: BLE001
            _idem_abort(deps, idempotency_key)
            raise HTTPException(
                status_code=502,
                detail=(
                    "Agent Server 恢复失败："
                    f"{type(exc).__name__}: {exc}"
                ),
            ) from exc
        pre = result.get("pre_restore") or {}
        pre_id = str(pre.get("revision_id") or "")
        if pre_id and store.get_revision(pre_id) is None:
            store.add_revision(
                workspace_id,
                revision_id=pre_id,
                source="restore",
                actor=user.name,
                git_commit=str(pre.get("git_commit") or ""),
                archive_path=str(pre.get("archive_path") or ""),
                archive_sha256=str(pre.get("archive_sha256") or ""),
                bytes=int(pre.get("bytes") or 0),
                file_count=int(pre.get("files") or 0),
                scan_status="skipped",
                status="superseded",
            )
        store.activate_revision(workspace_id, revision_id)
        store.set_usage(
            workspace_id,
            used_bytes=int(result.get("bytes") or 0),
            file_count=int(result.get("files") or 0),
        )
        _idem_finish(deps, idempotency_key, result)
        return result

    @app.post("/api/workspaces/{workspace_id}/revisions/prune")
    async def prune_revisions(workspace_id: str, user: UserDep) -> dict:
        deps.rate_limit(user)
        _require_store(deps)
        row = _owned(deps, user, workspace_id)
        if deps.agent_client is None:
            raise HTTPException(
                status_code=503, detail="清理归档需要独立的 Agent Server"
            )
        pruned = await _prune_workspace(deps, user, row)
        return {"pruned": pruned}

    @app.post(
        "/api/workspaces/{workspace_id}/revisions/{revision_id}/pin"
    )
    async def pin_revision(
        workspace_id: str,
        revision_id: str,
        request: WorkspacePinRequest,
        user: UserDep,
    ) -> dict:
        deps.rate_limit(user)
        store = _require_store(deps)
        _owned(deps, user, workspace_id)
        revision = store.get_revision(revision_id)
        if (
            revision is None
            or revision["workspace_id"] != workspace_id
        ):
            raise HTTPException(status_code=404, detail="revision 不存在")
        return store.set_revision_pinned(
            revision_id, request.pinned
        )
