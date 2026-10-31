"""RFC-0001：A3 导入任务的校验与纯函数。"""

from __future__ import annotations

import asyncio
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from ubismart.agent_protocol import AgentContext
from ubismart.config import Settings
from ubismart.servers.agent_server_routes import workspace_import as imports
from ubismart.servers.agent_server_routes.workspace_import import (
    ImportDeps,
    _activate,
    _spawn_job,
    _tree_size,
    register_import_routes,
    tar_command,
    validate_source,
)


def _settings(**kwargs) -> Settings:
    data = {
        "_env_file": None,
        "workspace_registry": True,
        "workspace_mount_roots": "/mnt/d,/mnt/e",
        "agent_data_root": tempfile.mkdtemp(prefix="a3-jobs-"),
    }
    data.update(kwargs)
    return Settings(**data)


def test_validate_source_accepts_allowed_root(monkeypatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    source = validate_source(
        _settings(), "/mnt/d/proj", target="/home/agent/ws/demo"
    )
    assert source == "/mnt/d/proj"


def test_validate_source_rejects_outside_root(monkeypatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    with pytest.raises(HTTPException) as exc:
        validate_source(
            _settings(), "/home/secret", target="/home/agent/ws/demo"
        )
    assert exc.value.status_code == 403


def test_validate_source_requires_roots(monkeypatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    with pytest.raises(HTTPException) as exc:
        validate_source(
            _settings(workspace_mount_roots=""),
            "/mnt/d/proj",
            target="/home/agent/ws/demo",
        )
    assert exc.value.status_code == 400


def test_validate_source_rejects_ancestor(monkeypatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    with pytest.raises(HTTPException) as exc:
        validate_source(
            _settings(),
            "/mnt/d/proj",
            target="/mnt/d/proj/sub/demo",
        )
    assert exc.value.status_code == 400


def test_tar_command_quotes_paths() -> None:
    command = tar_command("/mnt/d/my proj", "/tmp/stage x")
    assert "--exclude=./.agent" in command
    assert "'/mnt/d/my proj'" in command
    assert "'/tmp/stage x'" in command


def test_tree_size_counts_files(tmp_path) -> None:
    (tmp_path / "a.txt").write_text("12345", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "b.txt").write_text("123", encoding="utf-8")
    total, files = _tree_size(str(tmp_path))
    assert total == 8
    assert files == 2


def test_activate_moves_staging(tmp_path) -> None:
    staging = tmp_path / "stage"
    target = tmp_path / "target"
    staging.mkdir()
    (staging / "x").write_text("x", encoding="utf-8")
    _activate(str(staging), str(target))
    assert not staging.exists()
    assert (target / "x").read_text(encoding="utf-8") == "x"


def test_activate_rejects_non_empty_target(tmp_path) -> None:
    staging = tmp_path / "stage"
    target = tmp_path / "target"
    staging.mkdir()
    target.mkdir()
    (target / "existing").write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError):
        _activate(str(staging), str(target))


def _app(settings: Settings) -> TestClient:
    app = FastAPI()
    register_import_routes(
        app,
        ImportDeps(
            context=lambda _request: AgentContext(
                workspace="/home/agent/ws/demo", workspace_id="wid"
            ),
            new_runtime=lambda _context: None,
            close_runtime=_noop,
            settings=settings,
        ),
    )
    return TestClient(app)


async def _noop(_runtime) -> None:
    return None


def test_route_rejects_source_outside_roots(monkeypatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    client = _app(_settings())
    response = client.post(
        "/v1/workspaces/import",
        json={"source_path": "/home/secret"},
    )
    assert response.status_code == 403


def test_route_requires_roots(monkeypatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    client = _app(_settings(workspace_mount_roots=""))
    response = client.post(
        "/v1/workspaces/import",
        json={"source_path": "/mnt/d/proj"},
    )
    assert response.status_code == 400


def test_unknown_job_is_404() -> None:
    client = _app(_settings())
    assert (
        client.get("/v1/workspaces/import/missing").status_code == 404
    )
    assert (
        client.delete("/v1/workspaces/import/missing").status_code == 404
    )


def test_registry_off_returns_404(monkeypatch) -> None:
    monkeypatch.setattr(Path, "is_dir", lambda self: True)
    client = _app(_settings(workspace_registry=False))
    response = client.post(
        "/v1/workspaces/import",
        json={"source_path": "/mnt/d/proj"},
    )
    assert response.status_code == 404


def _job_deps(tmp_path) -> ImportDeps:
    return ImportDeps(
        context=lambda _request: AgentContext(
            workspace="/home/agent/ws/demo", workspace_id="wid"
        ),
        new_runtime=lambda _context: None,
        close_runtime=_noop,
        settings=_settings(agent_data_root=str(tmp_path)),
    )


def test_spawn_job_keeps_reference_and_records_failure(tmp_path) -> None:
    deps = _job_deps(tmp_path)
    job = {
        "job_id": "job-1",
        "status": "queued",
        "_cancel": threading.Event(),
    }

    async def boom() -> None:
        await asyncio.sleep(0)
        raise RuntimeError("boom")

    async def scenario() -> None:
        task = _spawn_job(deps, job, boom())
        assert job["_task"] is task
        with pytest.raises(RuntimeError):
            await task
        await asyncio.sleep(0)
        assert "_task" not in job
        assert job["status"] == "failed"
        assert "boom" in job["error"]

    asyncio.run(scenario())


def test_spawn_job_timeout_marks_failed(tmp_path) -> None:
    deps = _job_deps(tmp_path)
    job = {
        "job_id": "job-2",
        "status": "queued",
        "_cancel": threading.Event(),
    }

    async def slow() -> None:
        await asyncio.sleep(10)

    async def scenario() -> None:
        task = _spawn_job(deps, job, slow(), timeout_s=0.01)
        with pytest.raises(TimeoutError):
            await task
        await asyncio.sleep(0)
        assert job["status"] == "failed"
        assert "超时" in job["error"]
        assert job["_cancel"].is_set()

    asyncio.run(scenario())


class _Exec:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok

    def host_run(self, command: str, *, kind: str, cwd: str):
        from types import SimpleNamespace

        return SimpleNamespace(
            ok=self.ok,
            stdout="",
            stderr="" if self.ok else "failed",
        )


class _ImportRuntime:
    def __init__(self, execution) -> None:
        self.execution = execution


def _import_deps(tmp_path, execution, close=None):
    settings = _settings()
    context = AgentContext(
        workspace=str(tmp_path / "ws"),
        workspace_id="w1",
        user="alice",
    )
    deps = ImportDeps(
        context=lambda _request: context,
        new_runtime=lambda _context: _ImportRuntime(execution),
        close_runtime=close or _noop,
        settings=settings,
    )
    return deps, context


def _import_job(tmp_path) -> dict:
    return {
        "job_id": "j1",
        "source": str(tmp_path / "src"),
        "target": str(tmp_path / "ws"),
        "_cancel": threading.Event(),
        "status": "queued",
    }


def test_run_import_success_and_cancel(tmp_path, monkeypatch) -> None:
    from ubismart.servers.agent_server_models import WorkspaceImportBody

    exec_ = _Exec()
    deps, context = _import_deps(tmp_path, exec_)
    monkeypatch.setattr(imports, "_tree_size", lambda _path: (10, 2))
    monkeypatch.setattr(imports, "_attach_git_history", lambda *a: None)
    monkeypatch.setattr(imports, "_activate_replace", lambda *a: None)
    monkeypatch.setattr(imports, "_cleanup", lambda *a: None)
    monkeypatch.setattr(imports, "_remove_file", lambda *a: None)
    monkeypatch.setattr(
        imports,
        "_create_revision",
        lambda *a, **k: {"revision_id": "r1", "archive_path": ""},
    )
    body = WorkspaceImportBody(
        source_path=str(tmp_path / "src"), max_files=10
    )
    job = _import_job(tmp_path)
    asyncio.run(imports._run_import(deps, context, job, body))
    assert job["status"] == "active"
    assert job["revision"]["revision_id"] == "r1"

    job = _import_job(tmp_path)
    job["_cancel"].set()
    asyncio.run(imports._run_import(deps, context, job, body))
    assert job["status"] == "cancelled"


def test_run_import_failure_branches(tmp_path, monkeypatch) -> None:
    from ubismart.servers.agent_server_models import WorkspaceImportBody

    body = WorkspaceImportBody(
        source_path=str(tmp_path / "src"), max_files=10
    )
    monkeypatch.setattr(imports, "_cleanup", lambda *a: None)
    monkeypatch.setattr(imports, "_remove_file", lambda *a: None)
    monkeypatch.setattr(imports, "_attach_git_history", lambda *a: None)
    monkeypatch.setattr(imports, "_activate_replace", lambda *a: None)
    monkeypatch.setattr(
        imports,
        "_create_revision",
        lambda *a, **k: {"revision_id": "r1", "archive_path": ""},
    )

    # 执行面不可用。
    class _NoExec:
        pass

    deps, context = _import_deps(tmp_path, _NoExec())
    job = _import_job(tmp_path)
    asyncio.run(imports._run_import(deps, context, job, body))
    assert job["status"] == "failed"

    # host_run 失败。
    deps, context = _import_deps(tmp_path, _Exec(ok=False))
    job = _import_job(tmp_path)
    asyncio.run(imports._run_import(deps, context, job, body))
    assert job["status"] == "failed"

    # 配额/字节/文件数上限。
    for kwargs in (
        {"quota_bytes": 1},
        {"max_bytes": 1},
        {"max_files": 1},
    ):
        deps, context = _import_deps(tmp_path, _Exec())
        monkeypatch.setattr(imports, "_tree_size", lambda _path: (10, 2))
        job = _import_job(tmp_path)
        limited = WorkspaceImportBody(
            source_path=str(tmp_path / "src"), **kwargs
        )
        asyncio.run(imports._run_import(deps, context, job, limited))
        assert job["status"] == "failed"

    # close_runtime 抛错不影响任务结果。
    async def _boom(_runtime):
        raise RuntimeError("close failed")

    deps, context = _import_deps(tmp_path, _Exec(), close=_boom)
    monkeypatch.setattr(imports, "_tree_size", lambda _path: (10, 2))
    job = _import_job(tmp_path)
    asyncio.run(imports._run_import(deps, context, job, body))
    assert job["status"] == "active"
