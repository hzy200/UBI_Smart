"""M4：A2 工作区写操作的幂等键。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from ubismart.config import Settings
from ubismart.control.idempotency import IdempotencyStore
from ubismart.control.schema import Base
from ubismart.control.workspaces import WorkspacePolicy, WorkspaceStore
from ubismart.web.auth import AuthUser
from ubismart.web.dependencies import current_user
from ubismart.web.routes.workspaces import (
    WorkspaceRegistryDeps,
    register_workspace_registry_routes,
)


class _Agent:
    def __init__(self) -> None:
        self.starts = 0

    async def workspace_import_start(self, context, **kwargs):
        self.starts += 1
        return {
            "job_id": "job1",
            "workspace_id": context.workspace_id,
            "status": "queued",
        }


def _setup(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'control.db'}", future=True
    )
    Base.metadata.create_all(engine)
    store = WorkspaceStore(engine, WorkspacePolicy(root="/srv/ws"))
    idempotency = IdempotencyStore(engine)
    agent = _Agent()
    app = FastAPI()
    register_workspace_registry_routes(
        app,
        WorkspaceRegistryDeps(
            rate_limit=lambda _user: None,
            store=store,
            enabled=True,
            settings=Settings(
                _env_file=None,
                workspace_registry=True,
                workspace_mount_roots="/mnt/d",
            ),
            agent_client=agent,
            idempotency=idempotency,
        ),
    )
    app.dependency_overrides[current_user] = lambda: AuthUser(
        name="alice"
    )
    return TestClient(app), store, idempotency, agent


def test_create_replays_with_same_key(tmp_path) -> None:
    client, store, _idem, _agent = _setup(tmp_path)
    first = client.post(
        "/api/workspaces",
        json={"display_name": "Demo", "source": "upload"},
        headers={"Idempotency-Key": "k1"},
    )
    second = client.post(
        "/api/workspaces",
        json={"display_name": "Different", "source": "upload"},
        headers={"Idempotency-Key": "k1"},
    )
    assert first.status_code == second.status_code == 200
    assert first.json()["workspace_id"] == second.json()["workspace_id"]
    assert len(store.list("alice")) == 1


def test_different_key_creates_new_workspace(tmp_path) -> None:
    client, store, _idem, _agent = _setup(tmp_path)
    first = client.post(
        "/api/workspaces",
        json={"display_name": "Demo"},
        headers={"Idempotency-Key": "k1"},
    ).json()
    second = client.post(
        "/api/workspaces",
        json={"display_name": "Demo"},
        headers={"Idempotency-Key": "k2"},
    ).json()
    assert first["workspace_id"] != second["workspace_id"]
    assert len(store.list("alice")) == 2


def test_missing_key_does_not_dedupe(tmp_path) -> None:
    client, store, _idem, _agent = _setup(tmp_path)
    client.post("/api/workspaces", json={"display_name": "Demo"})
    client.post("/api/workspaces", json={"display_name": "Demo"})
    assert len(store.list("alice")) == 2


def test_in_flight_key_returns_409(tmp_path) -> None:
    client, _store, idempotency, _agent = _setup(tmp_path)
    assert idempotency.begin("k1", "workspace.create") is None
    response = client.post(
        "/api/workspaces",
        json={"display_name": "Demo"},
        headers={"Idempotency-Key": "k1"},
    )
    assert response.status_code == 409


def test_import_start_is_idempotent(tmp_path) -> None:
    client, _store, _idem, agent = _setup(tmp_path)
    row = client.post(
        "/api/workspaces", json={"display_name": "Demo"}
    ).json()
    first = client.post(
        f"/api/workspaces/{row['workspace_id']}/imports",
        json={"source_path": "/mnt/d/proj"},
        headers={"Idempotency-Key": "import-1"},
    )
    second = client.post(
        f"/api/workspaces/{row['workspace_id']}/imports",
        json={"source_path": "/mnt/d/proj"},
        headers={"Idempotency-Key": "import-1"},
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert agent.starts == 1
