"""RFC-0001：从 revision 归档做灾难恢复。"""

from __future__ import annotations

import hashlib
import io
import tarfile
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coding_agent.agent_protocol import AgentContext
from coding_agent.agent_server_routes import imports
from coding_agent.agent_server_routes.imports import (
    ImportDeps,
    register_import_routes,
)
from coding_agent.config import Settings


class _Exec:
    def host_run(self, command: str, *, kind: str, cwd: str):
        if kind == "workspace_revision_probe":
            return SimpleNamespace(ok=True, stdout="deadbeef\n", stderr="")
        return SimpleNamespace(ok=True, stdout="", stderr="")


class _Runtime:
    execution = _Exec()


async def _noop(_runtime) -> None:
    return None


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        workspace_registry=True,
        workspace_archive_root=str(tmp_path / "archive"),
        workspace_scan_provider="none",
    )


def _app(context: AgentContext, settings: Settings) -> TestClient:
    app = FastAPI()
    register_import_routes(
        app,
        ImportDeps(
            context=lambda _request: context,
            new_runtime=lambda _context: _Runtime(),
            close_runtime=_noop,
            settings=settings,
        ),
    )
    return TestClient(app)


def _archive(path, entries: dict[str, bytes]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(path, "w:gz") as archive:
        for name, data in entries.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))


def _sha(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(
        imports, "_archive_root", lambda _s: str(tmp_path / "archive")
    )
    monkeypatch.setattr(
        imports, "normalize_root", lambda value: str(value)
    )

    def inside(path, root):
        if "outside" in str(path):
            raise imports.PathGuardError("outside")
        return str(path)

    monkeypatch.setattr(imports, "ensure_inside", inside)
    target = tmp_path / "ws"
    target.mkdir()
    (target / "old.txt").write_text("old", encoding="utf-8")
    source = tmp_path / "archive" / "wid" / "rev.tar.gz"
    _archive(
        source,
        {
            ".git/config": b"[core]",
            "README.md": b"restored",
        },
    )
    context = AgentContext(
        workspace=str(target), workspace_id="wid", user="alice"
    )
    return target, source, context, _settings(tmp_path)


def _restore(
    client: TestClient, source, **overrides
) -> object:
    payload = {
        "revision_id": "rev",
        "archive_path": str(source),
        "archive_sha256": _sha(source),
        "git_commit": "deadbeef",
    }
    payload.update(overrides)
    return client.post("/v1/workspaces/restore", json=payload)


def test_restore_replaces_broken_workspace(env) -> None:
    target, source, context, settings = env
    response = _restore(_app(context, settings), source)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["status"] == "active"
    assert (target / "README.md").read_text(encoding="utf-8") == "restored"
    assert not (target / "old.txt").exists()
    assert (target / ".git" / "config").exists()
    assert payload["pre_restore"]["archive_path"]


def test_restore_rejects_sha_mismatch(env) -> None:
    _target, source, context, settings = env
    response = _restore(
        _app(context, settings), source, archive_sha256="bad"
    )
    assert response.status_code == 400


def test_restore_rejects_outside_archive(env) -> None:
    _target, source, context, settings = env
    response = _restore(
        _app(context, settings), source, archive_path="outside.tar.gz"
    )
    assert response.status_code == 400


def test_restore_rejects_missing_archive(env) -> None:
    _target, source, context, settings = env
    missing = source.parent / "missing.tar.gz"
    response = _restore(
        _app(context, settings), source, archive_path=str(missing)
    )
    assert response.status_code == 400
