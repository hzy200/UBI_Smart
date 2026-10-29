"""A3 导入/上传任务入口、状态与取消路由。"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding_agent.agent_protocol import AgentContext
from coding_agent.agent_server_routes import imports
from coding_agent.agent_server_routes.imports import (
    ImportDeps,
    register_import_routes,
)
from coding_agent.config import Settings


async def _close(_runtime) -> None:
    return None


def _settings(tmp_path, **kwargs) -> Settings:
    return Settings(
        _env_file=None,
        workspace_registry=True,
        workspace_mount_roots="/mnt/d",
        workspace_upload_max_bytes=1024,
        agent_data_root=str(tmp_path / "agent"),
        **kwargs,
    )


def _client(
    settings: Settings, *, workspace: str
) -> tuple[TestClient, ImportDeps]:
    context = AgentContext(
        workspace=workspace, workspace_id="w1", user="alice"
    )
    deps = ImportDeps(
        context=lambda _request: context,
        new_runtime=lambda _context: SimpleNamespace(execution=None),
        close_runtime=_close,
        settings=settings,
    )
    app = FastAPI()
    register_import_routes(app, deps)
    return TestClient(app), deps


def _drop(_deps, _job, coro, **_kwargs) -> None:
    coro.close()


def _job(status: str = "queued") -> dict:
    return {
        "job_id": "j1",
        "status": status,
        "target": "/mnt/d/ws",
        "_cancel": threading.Event(),
        "progress": 0,
        "files": 0,
        "bytes": 0,
    }


def test_import_start_status_cancel(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(imports, "validate_source", lambda *a, **k: "/mnt/d/src")
    monkeypatch.setattr(imports, "_spawn_job", _drop)
    client, deps = _client(_settings(tmp_path), workspace="/mnt/d/ws")

    response = client.post(
        "/v1/workspaces/import",
        json={"source_path": "/mnt/d/src"},
    )
    assert response.status_code == 200
    job_id = response.json()["job_id"]
    assert deps.jobs[job_id]["status"] == "queued"

    # 已有进行中的任务 → 409。
    assert client.post(
        "/v1/workspaces/import",
        json={"source_path": "/mnt/d/src"},
    ).status_code == 409

    status = client.get(f"/v1/workspaces/import/{job_id}")
    assert status.status_code == 200
    cancel = client.delete(f"/v1/workspaces/import/{job_id}")
    assert cancel.status_code == 200
    assert deps.jobs[job_id]["_cancel"].is_set()

    # 终态重复取消原样返回。
    deps.jobs[job_id]["status"] = "active"
    assert client.delete(
        f"/v1/workspaces/import/{job_id}"
    ).status_code == 200
    assert client.get("/v1/workspaces/import/missing").status_code == 404


def test_import_routes_registry_off(tmp_path) -> None:
    client, _ = _client(
        Settings(_env_file=None, workspace_registry=False),
        workspace="/mnt/d/ws",
    )
    assert client.post(
        "/v1/workspaces/import", json={"source_path": "/mnt/d/src"}
    ).status_code == 404
    assert client.get("/v1/workspaces/import/x").status_code == 404
    assert client.delete("/v1/workspaces/import/x").status_code == 404
    assert client.get("/v1/workspaces/uploads/x").status_code == 404
    assert client.delete("/v1/workspaces/uploads/x").status_code == 404


def test_upload_start_status_cancel_and_limits(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(imports, "_spawn_job", _drop)
    client, deps = _client(_settings(tmp_path), workspace=str(tmp_path / "ws"))

    response = client.post(
        "/v1/workspaces/uploads",
        files={"archive": ("a.zip", b"abc", "application/zip")},
    )
    assert response.status_code == 200, response.text
    job_id = response.json()["job_id"]
    assert deps.jobs[job_id]["status"] == "receiving"
    assert client.get(
        f"/v1/workspaces/uploads/{job_id}"
    ).status_code == 200

    cancel = client.delete(f"/v1/workspaces/uploads/{job_id}")
    assert cancel.status_code == 200
    assert deps.jobs[job_id]["_cancel"].is_set()

    # 超过上传上限 → 413，且不留下任务。
    settings = _settings(tmp_path)
    settings = settings.model_copy(update={"workspace_upload_max_bytes": 2})
    client, deps = _client(settings, workspace=str(tmp_path / "ws2"))
    before = set(deps.jobs)
    response = client.post(
        "/v1/workspaces/uploads",
        files={"archive": ("a.zip", b"abc", "application/zip")},
    )
    assert response.status_code == 413
    assert set(deps.jobs) == before

    # 进行中的任务阻止第二次上传。
    workspace3 = str(tmp_path / "ws3")
    client, deps = _client(_settings(tmp_path), workspace=workspace3)
    deps.jobs["busy"] = {**_job(status="receiving"), "target": workspace3}
    assert client.post(
        "/v1/workspaces/uploads",
        files={"archive": ("a.zip", b"x", "application/zip")},
    ).status_code == 409
