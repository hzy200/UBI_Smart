"""A3 回滚/恢复/删除归档路由的边界分支。"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding_agent.agent_protocol import AgentContext
from coding_agent.agent_server_routes import workspace_import as imports
from coding_agent.agent_server_routes.workspace_import import (
    ImportDeps,
    register_import_routes,
)
from coding_agent.config import Settings


class _Exec:
    def __init__(self, *, ok: bool = True) -> None:
        self.ok = ok
        self.calls: list[str] = []

    def host_run(self, command: str, *, kind: str, cwd: str):
        self.calls.append(kind)
        return SimpleNamespace(
            ok=self.ok, stdout="ok", stderr="" if self.ok else "failed"
        )


async def _close(_runtime) -> None:
    return None


def _settings(tmp_path, **kwargs) -> Settings:
    return Settings(
        _env_file=None,
        workspace_registry=True,
        workspace_archive_root=str(tmp_path / "archive"),
        agent_data_root=str(tmp_path / "agent"),
        **kwargs,
    )


def _client(
    settings: Settings,
    *,
    execution=None,
    workspace: str = "/tmp/ws",
) -> TestClient:
    if execution is None:
        execution = _Exec()
    elif execution == "none":
        execution = None
    context = AgentContext(
        workspace=workspace, workspace_id="w1", user="alice"
    )
    app = FastAPI()
    register_import_routes(
        app,
        ImportDeps(
            context=lambda _request: context,
            new_runtime=lambda _context: SimpleNamespace(
                execution=execution
            ),
            close_runtime=_close,
            settings=settings,
        ),
    )
    return TestClient(app)


def _tarball(path: Path, files: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for name, content in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))


def test_registry_off_and_invalid_commit(tmp_path) -> None:
    off = Settings(_env_file=None, workspace_registry=False)
    client = _client(off)
    assert client.post(
        "/v1/workspaces/rollback",
        json={"revision_id": "rev1", "git_commit": "abcdef1"},
    ).status_code == 404
    assert client.post(
        "/v1/workspaces/restore",
        json={"revision_id": "rev1", "archive_path": "/tmp/x.tar.gz"},
    ).status_code == 404
    assert client.request(
        "DELETE",
        "/v1/workspaces/revisions/archive",
        json={"revision_id": "rev1", "archive_path": "/tmp/x.tar.gz"},
    ).status_code == 404

    client = _client(_settings(tmp_path))
    assert client.post(
        "/v1/workspaces/rollback",
        json={"revision_id": "rev1", "git_commit": "bad commit"},
    ).status_code == 400
    client = _client(_settings(tmp_path), execution="none")
    assert client.post(
        "/v1/workspaces/rollback",
        json={"revision_id": "rev1", "git_commit": "abcdef12"},
    ).status_code == 503


def test_rollback_success_and_git_failure(tmp_path, monkeypatch) -> None:
    target = tmp_path / "ws"
    target.mkdir()
    (target / "a.txt").write_text("hi", encoding="utf-8")
    monkeypatch.setattr(
        imports, "_create_revision", lambda *a, **k: {"revision_id": "pre"}
    )
    client = _client(_settings(tmp_path), workspace=str(target))
    response = client.post(
        "/v1/workspaces/rollback",
        json={"revision_id": "rev1", "git_commit": "abcdef12"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["files"] == 1 and body["bytes"] == 2
    assert body["pre_rollback"]["revision_id"] == "pre"

    client = _client(
        _settings(tmp_path), execution=_Exec(ok=False), workspace=str(target)
    )
    response = client.post(
        "/v1/workspaces/rollback",
        json={"revision_id": "rev1", "git_commit": "abcdef12"},
    )
    assert response.status_code == 409


def test_restore_validation_and_success(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    archive_root = Path(settings.workspace_archive_root)
    archive = archive_root / "rev.tar.gz"
    _tarball(archive, {"new.txt": b"new"})
    target = tmp_path / "ws"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    monkeypatch.setattr(
        imports, "_create_revision", lambda *a, **k: {"revision_id": "pre"}
    )
    monkeypatch.setattr(
        imports.os.path,
        "isfile",
        lambda path: "rev.tar.gz" in str(path),
    )
    monkeypatch.setattr(imports, "_sha256", lambda _path: "digest")
    monkeypatch.setattr(imports, "safe_extract", lambda *a, **k: None)
    monkeypatch.setattr(
        imports,
        "_tree_size",
        lambda _path: (3, 1),
    )

    def _activate(staging: str, workspace: str) -> None:
        Path(workspace).mkdir(parents=True, exist_ok=True)
        (Path(workspace) / "new.txt").write_text("new", encoding="utf-8")

    monkeypatch.setattr(imports, "_activate_replace", _activate)
    client = _client(settings, workspace=str(target))

    assert client.post(
        "/v1/workspaces/restore", json={"revision_id": "rev1", "archive_path": "/etc/passwd"}
    ).status_code == 400
    assert client.post(
        "/v1/workspaces/restore",
        json={"revision_id": "rev1", "archive_path": str(archive_root / "missing.tar.gz")},
    ).status_code == 400
    assert client.post(
        "/v1/workspaces/restore",
        json={"revision_id": "rev1", "archive_path": str(archive), "archive_sha256": "deadbeef"},
    ).status_code == 400
    assert client.post(
        "/v1/workspaces/restore",
        json={"revision_id": "rev1", "archive_path": str(archive), "git_commit": "bad commit"},
    ).status_code == 400

    response = client.post(
        "/v1/workspaces/restore", json={"revision_id": "rev1", "archive_path": str(archive)}
    )
    assert response.status_code == 200, response.text
    assert (target / "new.txt").read_text(encoding="utf-8") == "new"
    assert response.json()["pre_restore"]["revision_id"] == "pre"
    assert response.json()["files"] == 1

    # 归档里找不到 git commit → 409。
    client = _client(
        settings, execution=_Exec(ok=False), workspace=str(target)
    )
    assert client.post(
        "/v1/workspaces/restore",
        json={"revision_id": "rev1", "archive_path": str(archive), "git_commit": "abcdef12"},
    ).status_code == 409


def test_restore_without_archive_root_and_delete(
    tmp_path, monkeypatch
) -> None:
    settings = Settings(
        _env_file=None,
        workspace_registry=True,
        workspace_archive_root="",
    )
    client = _client(settings)
    assert client.post(
        "/v1/workspaces/restore",
        json={"revision_id": "rev1", "archive_path": "/tmp/x.tar.gz"},
    ).status_code == 400
    assert client.request(
        "DELETE",
        "/v1/workspaces/revisions/archive",
        json={"revision_id": "rev1", "archive_path": "/tmp/x.tar.gz"},
    ).status_code == 400

    settings = _settings(tmp_path)
    root = Path(settings.workspace_archive_root)
    root.mkdir(parents=True, exist_ok=True)
    removed: list[str] = []
    monkeypatch.setattr(imports.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(imports.os, "remove", removed.append)
    client = _client(settings)
    response = client.request(
        "DELETE",
        "/v1/workspaces/revisions/archive",
        json={"revision_id": "rev1", "archive_path": str(root / "old.tar.gz")},
    )
    assert response.status_code == 200
    assert removed and removed[0].endswith("old.tar.gz")
