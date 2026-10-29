"""快照、checkpoint、fork/rewind 路由。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Response
from pydantic import BaseModel

from coding_agent.audit.logger import read_records_many
from coding_agent.identity.tenancy import WebTenant
from coding_agent.session.run_compare import build_runs, compare_runs
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import UserDep
from coding_agent.web.models import SessionConfig
from coding_agent.web.validation import check_thread_id


class RestoreRequest(BaseModel):
    thread_id: str
    path: str = ""
    snapshot_id: str = ""
    prefix: str = ""
    dry_run: bool = False


@dataclass(frozen=True)
class SnapshotDeps:
    config_and_tenant: Callable[[AuthUser, str], tuple[WebTenant, Any]]
    reject_active: Callable[[AuthUser, str], None]
    agent_client: Any
    agent_json: Callable[..., Awaitable[dict[str, Any]]]
    ops_runtime: Callable[[WebTenant, Any], Any]
    set_config: Callable[[AuthUser, WebTenant, Any], None]
    effective_access: Callable[[Any], str | None]


def register_snapshot_routes(app: FastAPI, deps: SnapshotDeps) -> None:
    @app.get("/api/snapshots")
    async def snapshots(
        user: UserDep,
        thread_id: str,
        limit: int = 20,
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        tenant, config = deps.config_and_tenant(user, public_thread)
        if deps.agent_client is not None:
            return await deps.agent_json(
                user, tenant, config, "GET", "/v1/snapshots",
                params={"thread_id": public_thread, "limit": limit},
            )
        runtime = deps.ops_runtime(tenant, config)
        try:
            entries = runtime.list_snapshots(limit=max(1, limit))
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        return {"snapshots": [asdict(entry) for entry in entries]}

    @app.get("/api/diff")
    async def diff(
        user: UserDep,
        thread_id: str,
        path: str = "",
        snapshot_id: str = "",
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        tenant, config = deps.config_and_tenant(user, public_thread)
        if deps.agent_client is not None:
            return await deps.agent_json(
                user, tenant, config, "GET", "/v1/diff",
                params={
                    "thread_id": public_thread,
                    "path": path,
                    "snapshot_id": snapshot_id,
                },
            )
        runtime = deps.ops_runtime(tenant, config)
        try:
            text = runtime.diff_snapshot(
                path=path or None,
                snapshot_id=snapshot_id or None,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        return {"diff": text}

    @app.post("/api/restore")
    async def restore(
        request: RestoreRequest,
        user: UserDep,
    ) -> dict:
        public_thread = check_thread_id(request.thread_id)
        deps.reject_active(user, public_thread)
        tenant, config = deps.config_and_tenant(user, public_thread)
        if deps.agent_client is not None:
            return await deps.agent_json(
                user, tenant, config, "POST", "/v1/restore",
                json_body=request.model_dump(),
            )
        runtime = deps.ops_runtime(tenant, config)
        try:
            if request.prefix:
                results = runtime.restore_since(
                    request.prefix, dry_run=request.dry_run
                )
                return {"results": [asdict(item) for item in results]}
            result = runtime.restore(
                path=request.path or None,
                snapshot_id=request.snapshot_id or None,
            )
            return asdict(result)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()

    @app.get("/api/run_started/{thread_id}")
    async def run_started(
        thread_id: str,
        user: UserDep,
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        tenant, config = deps.config_and_tenant(user, public_thread)
        if deps.agent_client is not None:
            return await deps.agent_json(
                user, tenant, config, "GET",
                f"/v1/run_started/{public_thread}",
            )
        runtime = deps.ops_runtime(tenant, config)
        try:
            prefix = runtime.last_run_started_at(
                tenant.thread_id(public_thread)
            )
        finally:
            await runtime.aclose()
        return {"prefix": prefix}

    @app.get("/api/sessions/{thread_id}/patch")
    async def session_patch(
        thread_id: str,
        user: UserDep,
    ) -> Response:
        """下载本会话本轮改动的合并补丁（`git apply` 可直接用）。

        为什么按 thread 而不是按 snapshot：用户的心智模型是"这次运行改了什么"，
        不是"第 37 号快照"。合成逻辑在 `runtime.session_patch`。
        """
        public_thread = check_thread_id(thread_id)
        tenant, config = deps.config_and_tenant(user, public_thread)
        if deps.agent_client is not None:
            payload = await deps.agent_json(
                user, tenant, config, "GET",
                f"/v1/sessions/{public_thread}/patch",
            )
            text = str(payload.get("patch") or "")
        else:
            runtime = deps.ops_runtime(tenant, config)
            try:
                text = runtime.session_patch(tenant.thread_id(public_thread))
            except Exception as exc:  # noqa: BLE001
                raise HTTPException(
                    status_code=503,
                    detail=f"{type(exc).__name__}: {exc}",
                ) from exc
            finally:
                await runtime.aclose()
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        return Response(
            content=text,
            media_type="text/x-patch",
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{public_thread[:8]}-{stamp}.patch"'
                )
            },
        )

    @app.get("/api/sessions/{thread_id}/runs")
    async def session_runs(
        thread_id: str,
        user: UserDep,
        limit: int = 20,
    ) -> dict:
        """按审计 RUN_START/END 切分运行，供并排对比选择。"""
        public_thread = check_thread_id(thread_id)
        tenant, config = deps.config_and_tenant(user, public_thread)
        size = max(1, min(50, int(limit)))
        if deps.agent_client is not None:
            return await deps.agent_json(
                user,
                tenant,
                config,
                "GET",
                f"/v1/sessions/{public_thread}/runs",
                params={"limit": size},
            )
        runtime = deps.ops_runtime(tenant, config)
        try:
            records = read_records_many(
                runtime.audit_files,
                thread_id=tenant.thread_id(public_thread),
                limit=5000,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        return {
            "runs": build_runs(
                [record.model_dump(mode="json") for record in records],
                limit=size,
            )
        }

    @app.get("/api/sessions/{thread_id}/runs/compare")
    async def compare_session_runs(
        thread_id: str,
        user: UserDep,
        left: int,
        right: int,
        max_files: int = 30,
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        tenant, config = deps.config_and_tenant(user, public_thread)
        size = max(1, min(50, int(max_files)))
        if deps.agent_client is not None:
            return await deps.agent_json(
                user,
                tenant,
                config,
                "GET",
                f"/v1/sessions/{public_thread}/runs/compare",
                params={
                    "left": int(left),
                    "right": int(right),
                    "max_files": size,
                },
            )
        runtime = deps.ops_runtime(tenant, config)
        try:
            records = read_records_many(
                runtime.audit_files,
                thread_id=tenant.thread_id(public_thread),
                limit=5000,
            )
            runs = build_runs(
                [record.model_dump(mode="json") for record in records],
                limit=1000,
            )
            try:
                data = compare_runs(runs, left, right)
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
                    except Exception:  # noqa: BLE001 - 单文件 diff 失败不整体失败
                        info["diff"] = ""
            data["files"] = files[:size]
            data["truncated_files"] = truncated
            return data
        finally:
            await runtime.aclose()

    @app.get("/api/checkpoints/{thread_id}")
    async def checkpoints(
        thread_id: str,
        user: UserDep,
        limit: int = 20,
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        tenant, config = deps.config_and_tenant(user, public_thread)
        if deps.agent_client is not None:
            return await deps.agent_json(
                user, tenant, config, "GET",
                f"/v1/checkpoints/{public_thread}",
                params={"limit": limit},
            )
        runtime = deps.ops_runtime(tenant, config)
        try:
            found = await runtime.checkpoints(
                tenant.thread_id(public_thread),
                limit=max(1, limit),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=503,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        return {"checkpoints": found}

    @app.post("/api/fork/{thread_id}")
    async def fork(
        thread_id: str,
        user: UserDep,
        checkpoint_id: str = "",
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        deps.reject_active(user, public_thread)
        tenant, config = deps.config_and_tenant(user, public_thread)
        if deps.agent_client is not None:
            result = await deps.agent_json(
                user, tenant, config, "POST",
                f"/v1/fork/{public_thread}",
                params={"checkpoint_id": checkpoint_id},
            )
            target_public = str(result.get("thread_id") or "")
            if target_public:
                deps.set_config(
                    user,
                    tenant,
                    SessionConfig(
                        thread_id=target_public,
                        workspace=(
                            config.workspace
                            if config
                            else tenant.workspace
                        ),
                        workspace_id=config.workspace_id if config else "",
                        access_mode=deps.effective_access(config),
                        model=config.model if config else "",
                        provider=config.provider if config else "",
                    ),
                )
            return result
        runtime = deps.ops_runtime(tenant, config)
        try:
            target_internal = await runtime.fork(
                tenant.thread_id(public_thread),
                checkpoint_id=checkpoint_id,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=409,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        target_public = tenant.public_thread(target_internal)
        deps.set_config(
            user,
            tenant,
            SessionConfig(
                thread_id=target_public,
                workspace=config.workspace if config else tenant.workspace,
                workspace_id=config.workspace_id if config else "",
                access_mode=deps.effective_access(config),
                model=config.model if config else "",
                provider=config.provider if config else "",
            ),
        )
        return {"thread_id": target_public}

    @app.post("/api/rewind/{thread_id}")
    async def rewind(
        thread_id: str,
        user: UserDep,
        steps: int = 1,
        checkpoint_id: str = "",
    ) -> dict:
        public_thread = check_thread_id(thread_id)
        deps.reject_active(user, public_thread)
        tenant, config = deps.config_and_tenant(user, public_thread)
        if deps.agent_client is not None:
            return await deps.agent_json(
                user, tenant, config, "POST",
                f"/v1/rewind/{public_thread}",
                params={
                    "steps": max(1, steps),
                    "checkpoint_id": checkpoint_id,
                },
            )
        runtime = deps.ops_runtime(tenant, config)
        try:
            found = await runtime.rewind(
                tenant.thread_id(public_thread),
                checkpoint_id=checkpoint_id,
                steps=max(1, steps),
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=409,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc
        finally:
            await runtime.aclose()
        return {"checkpoint_id": found}
