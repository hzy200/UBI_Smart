"""RFC-0001：A3 导入任务的校验与纯函数。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from coding_agent.agent_protocol import AgentContext
from coding_agent.agent_server_routes.imports import (
    ImportDeps,
    _activate,
    _tree_size,
    register_import_routes,
    tar_command,
    validate_source,
)
from coding_agent.config import Settings


def _settings(**kwargs) -> Settings:
    data = {
        "_env_file": None,
        "workspace_registry": True,
        "workspace_mount_roots": "/mnt/d,/mnt/e",
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
