"""RFC-0001：A2 导入代理与工作区状态回写。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from ubismart.config import Settings
from ubismart.control.schema import Base
from ubismart.control.workspaces import WorkspacePolicy, WorkspaceStore
from ubismart.web.auth import AuthUser
from ubismart.web.dependencies import current_user
from ubismart.web.routes.workspaces import (
    WorkspaceRegistryDeps,
    register_workspace_registry_routes,
)


class _Agent:
    def __init__(self, status: str = "queued") -> None:
        self.status = status
        self.started: list[tuple] = []
        self.cancelled: list[str] = []
        self.workspace_override = ""
        self.revision: dict | None = None

    async def workspace_import_start(self, context, **kwargs):
        self.started.append((context, kwargs))
        return {
            "job_id": "job1",
            "workspace_id": context.workspace_id,
            "status": "queued",
        }

    async def workspace_import_status(self, context, job_id):
        return {
            "job_id": job_id,
            "workspace_id": self.workspace_override or context.workspace_id,
            "status": self.status,
            "bytes": 12,
            "files": 2,
            "error": "",
            "revision": self.revision,
        }

    async def workspace_import_cancel(self, context, job_id):
        self.cancelled.append(job_id)
        return {
            "job_id": job_id,
            "workspace_id": context.workspace_id,
            "status": "cancelled",
        }


def _store(tmp_path) -> WorkspaceStore:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'control.db'}", future=True
    )
    Base.metadata.create_all(engine)
    return WorkspaceStore(
        engine, WorkspacePolicy(root="/srv/ws")
    )


def _app(
    store: WorkspaceStore | None,
    *,
    agent: object | None = None,
    roots: str = "/mnt/d",
) -> tuple[TestClient, dict]:
    app = FastAPI()
    register_workspace_registry_routes(
        app,
        WorkspaceRegistryDeps(
            rate_limit=lambda _user: None,
            store=store,
            enabled=True,
            settings=Settings(
                _env_file=None,
                workspace_mount_roots=roots,
                workspace_registry=True,
            ),
            agent_client=agent,
        ),
    )
    holder = {"user": "alice"}
    app.dependency_overrides[current_user] = lambda: AuthUser(
        name=holder["user"]
    )
    return TestClient(app), holder


def _workspace(client: TestClient) -> dict:
    return client.post(
        "/api/workspaces",
        json={"display_name": "Demo", "source": "upload"},
    ).json()


def test_start_import_forwards_context(tmp_path) -> None:
    agent = _Agent()
    client, _ = _app(_store(tmp_path), agent=agent)
    row = _workspace(client)
    response = client.post(
        f"/api/workspaces/{row['workspace_id']}/imports",
        json={"source_path": "/mnt/d/proj"},
    )
    assert response.status_code == 200
    assert response.json()["job_id"] == "job1"
    context, kwargs = agent.started[0]
    assert context.workspace == row["root_path"]
    assert context.workspace_id == row["workspace_id"]
    assert kwargs["source_path"] == "/mnt/d/proj"
    assert kwargs["mode"] == "copy"


def test_active_status_updates_workspace(tmp_path) -> None:
    agent = _Agent(status="active")
    store = _store(tmp_path)
    client, _ = _app(store, agent=agent)
    row = _workspace(client)
    client.post(
        f"/api/workspaces/{row['workspace_id']}/imports",
        json={"source_path": "/mnt/d/proj"},
    )
    response = client.get(
        f"/api/workspaces/{row['workspace_id']}/imports/job1"
    )
    assert response.status_code == 200
    updated = store.get(row["workspace_id"])
    assert updated["status"] == "active"
    assert updated["used_bytes"] == 12
    assert updated["file_count"] == 2


def test_active_status_records_import_revision(tmp_path) -> None:
    agent = _Agent(status="active")
    agent.revision = {
        "revision_id": "r-import",
        "git_commit": "abc1234",
        "archive_path": "/srv/archive/w/r-import.tar.gz",
        "archive_sha256": "sha",
        "bytes": 20,
        "files": 3,
    }
    store = _store(tmp_path)
    client, _ = _app(store, agent=agent)
    row = _workspace(client)
    client.post(
        f"/api/workspaces/{row['workspace_id']}/imports",
        json={"source_path": "/mnt/d/proj"},
    )
    client.get(f"/api/workspaces/{row['workspace_id']}/imports/job1")
    revision = store.get_revision("r-import")
    assert revision is not None
    assert revision["source"] == "local_import"
    assert revision["git_commit"] == "abc1234"
    updated = store.get(row["workspace_id"])
    assert updated["status"] == "active"
    assert updated["used_bytes"] == 20
    assert updated["file_count"] == 3


def test_active_workspace_can_import_again(tmp_path) -> None:
    agent = _Agent(status="queued")
    store = _store(tmp_path)
    client, _ = _app(store, agent=agent)
    row = _workspace(client)
    store.set_status(row["workspace_id"], "active")
    response = client.post(
        f"/api/workspaces/{row['workspace_id']}/imports",
        json={"source_path": "/mnt/d/proj"},
    )
    assert response.status_code == 200
    assert store.get(row["workspace_id"])["status"] == "active"


def test_active_import_failure_keeps_old_version(tmp_path) -> None:
    agent = _Agent(status="failed")
    store = _store(tmp_path)
    client, _ = _app(store, agent=agent)
    row = _workspace(client)
    store.set_status(row["workspace_id"], "active")
    client.post(
        f"/api/workspaces/{row['workspace_id']}/imports",
        json={"source_path": "/mnt/d/proj"},
    )
    client.get(f"/api/workspaces/{row['workspace_id']}/imports/job1")
    assert store.get(row["workspace_id"])["status"] == "active"


def test_failed_status_marks_workspace_and_allows_retry(tmp_path) -> None:
    agent = _Agent(status="failed")
    store = _store(tmp_path)
    client, _ = _app(store, agent=agent)
    row = _workspace(client)
    client.post(
        f"/api/workspaces/{row['workspace_id']}/imports",
        json={"source_path": "/mnt/d/proj"},
    )
    client.get(f"/api/workspaces/{row['workspace_id']}/imports/job1")
    assert store.get(row["workspace_id"])["status"] == "failed"

    retry = client.post(
        f"/api/workspaces/{row['workspace_id']}/imports",
        json={"source_path": "/mnt/d/proj"},
    )
    assert retry.status_code == 200
    assert store.get(row["workspace_id"])["status"] == "provisioning"


def test_cancelled_status_marks_workspace_failed(tmp_path) -> None:
    agent = _Agent(status="cancelled")
    store = _store(tmp_path)
    client, _ = _app(store, agent=agent)
    row = _workspace(client)
    client.post(
        f"/api/workspaces/{row['workspace_id']}/imports",
        json={"source_path": "/mnt/d/proj"},
    )
    client.get(f"/api/workspaces/{row['workspace_id']}/imports/job1")
    assert store.get(row["workspace_id"])["status"] == "failed"


def test_cancel_forwards(tmp_path) -> None:
    agent = _Agent()
    client, _ = _app(_store(tmp_path), agent=agent)
    row = _workspace(client)
    response = client.delete(
        f"/api/workspaces/{row['workspace_id']}/imports/job1"
    )
    assert response.status_code == 200
    assert agent.cancelled == ["job1"]


def test_source_outside_roots_is_403(tmp_path) -> None:
    client, _ = _app(_store(tmp_path), agent=_Agent())
    row = _workspace(client)
    response = client.post(
        f"/api/workspaces/{row['workspace_id']}/imports",
        json={"source_path": "/home/secret"},
    )
    assert response.status_code == 403


def test_missing_roots_is_400(tmp_path) -> None:
    client, _ = _app(_store(tmp_path), agent=_Agent(), roots="")
    row = _workspace(client)
    response = client.post(
        f"/api/workspaces/{row['workspace_id']}/imports",
        json={"source_path": "/mnt/d/proj"},
    )
    assert response.status_code == 400


def test_missing_agent_client_is_503(tmp_path) -> None:
    client, _ = _app(_store(tmp_path), agent=None)
    row = _workspace(client)
    response = client.post(
        f"/api/workspaces/{row['workspace_id']}/imports",
        json={"source_path": "/mnt/d/proj"},
    )
    assert response.status_code == 503


def test_job_from_other_workspace_is_404(tmp_path) -> None:
    agent = _Agent(status="active")
    agent.workspace_override = "another-workspace"
    client, _ = _app(_store(tmp_path), agent=agent)
    row = _workspace(client)
    response = client.get(
        f"/api/workspaces/{row['workspace_id']}/imports/job1"
    )
    assert response.status_code == 404
