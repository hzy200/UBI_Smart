"""RFC-0001：A2 上传/回滚代理与 revision 记录。"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from coding_agent.config import Settings
from coding_agent.control.schema import Base
from coding_agent.control.workspaces import WorkspacePolicy, WorkspaceStore
from coding_agent.web.auth import AuthUser
from coding_agent.web.dependencies import current_user
from coding_agent.web.routes.workspaces import (
    WorkspaceRegistryDeps,
    register_workspace_registry_routes,
)


class _Agent:
    def __init__(self) -> None:
        self.status = "active"
        self.upload_started: tuple | None = None
        self.rollback_payload: dict | None = None
        self.deleted: list[str] = []
        self.cancelled: list[str] = []

    async def workspace_upload_start(self, context, **kwargs):
        self.upload_started = (context, kwargs)
        return {
            "job_id": "u1",
            "workspace_id": context.workspace_id,
            "status": "receiving",
        }

    async def workspace_upload_status(self, context, job_id):
        return {
            "job_id": job_id,
            "workspace_id": context.workspace_id,
            "status": self.status,
            "bytes": 5,
            "files": 1,
            "scan_status": "clean",
            "revision": {
                "revision_id": "r1",
                "git_commit": "abc1234",
                "archive_path": "/srv/archive/w/r1.tar.gz",
                "archive_sha256": "sha",
                "bytes": 6,
                "files": 2,
            },
        }

    async def workspace_upload_cancel(self, context, job_id):
        self.cancelled.append(job_id)
        return {
            "job_id": job_id,
            "workspace_id": context.workspace_id,
            "status": "cancelled",
        }

    async def workspace_rollback(self, context, **kwargs):
        self.rollback_payload = kwargs
        return {
            "status": "active",
            "pre_rollback": {
                "revision_id": "pre1",
                "git_commit": "def5678",
                "archive_path": "/srv/archive/w/pre1.tar.gz",
                "archive_sha256": "sha2",
                "bytes": 6,
                "files": 2,
            },
            "bytes": 7,
            "files": 2,
        }

    async def workspace_archive_delete(self, context, archive_path):
        self.deleted.append(archive_path)
        return {"ok": True}


def _store(tmp_path) -> WorkspaceStore:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'control.db'}", future=True
    )
    Base.metadata.create_all(engine)
    return WorkspaceStore(engine, WorkspacePolicy(root="/srv/ws"))


def _app(
    store: WorkspaceStore,
    *,
    agent: _Agent | None = None,
    upload_max: int = 1000,
) -> TestClient:
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
                workspace_upload_max_bytes=upload_max,
                workspace_upload_max_files=100,
                workspace_revisions_keep=10,
            ),
            agent_client=agent or _Agent(),
        ),
    )
    app.dependency_overrides[current_user] = lambda: AuthUser(
        name="alice"
    )
    return TestClient(app)


def _workspace(client: TestClient) -> dict:
    return client.post(
        "/api/workspaces",
        json={"display_name": "Demo", "source": "upload"},
    ).json()


def test_upload_forwards_file(tmp_path) -> None:
    agent = _Agent()
    client = _app(_store(tmp_path), agent=agent)
    row = _workspace(client)
    response = client.post(
        f"/api/workspaces/{row['workspace_id']}/uploads",
        files={"archive": ("proj.zip", b"PK\x03\x04data", "application/zip")},
    )
    assert response.status_code == 200
    assert response.json()["job_id"] == "u1"
    assert agent.upload_started is not None
    _context, kwargs = agent.upload_started
    assert kwargs["filename"] == "proj.zip"
    assert kwargs["data"] == b"PK\x03\x04data"


def test_upload_size_limit_is_413(tmp_path) -> None:
    agent = _Agent()
    client = _app(_store(tmp_path), agent=agent, upload_max=3)
    row = _workspace(client)
    response = client.post(
        f"/api/workspaces/{row['workspace_id']}/uploads",
        files={"archive": ("proj.zip", b"12345", "application/zip")},
    )
    assert response.status_code == 413
    assert agent.upload_started is None


def test_active_status_records_revision_and_usage(tmp_path) -> None:
    agent = _Agent()
    store = _store(tmp_path)
    client = _app(store, agent=agent)
    row = _workspace(client)
    client.post(
        f"/api/workspaces/{row['workspace_id']}/uploads",
        files={"archive": ("proj.zip", b"data", "application/zip")},
    )
    response = client.get(
        f"/api/workspaces/{row['workspace_id']}/uploads/u1"
    )
    assert response.status_code == 200
    revision = store.get_revision("r1")
    assert revision is not None
    assert revision["status"] == "active"
    assert revision["git_commit"] == "abc1234"
    updated = store.get(row["workspace_id"])
    assert updated["status"] == "active"
    assert updated["used_bytes"] == 6
    assert updated["file_count"] == 2


def test_active_workspace_can_upload_again(tmp_path) -> None:
    agent = _Agent()
    store = _store(tmp_path)
    client = _app(store, agent=agent)
    row = _workspace(client)
    store.set_status(row["workspace_id"], "active")
    response = client.post(
        f"/api/workspaces/{row['workspace_id']}/uploads",
        files={"archive": ("proj.zip", b"data", "application/zip")},
    )
    assert response.status_code == 200
    assert store.get(row["workspace_id"])["status"] == "active"


def test_failed_status_marks_workspace(tmp_path) -> None:
    agent = _Agent()
    agent.status = "failed"
    store = _store(tmp_path)
    client = _app(store, agent=agent)
    row = _workspace(client)
    client.post(
        f"/api/workspaces/{row['workspace_id']}/uploads",
        files={"archive": ("proj.zip", b"data", "application/zip")},
    )
    client.get(f"/api/workspaces/{row['workspace_id']}/uploads/u1")
    assert store.get(row["workspace_id"])["status"] == "failed"


def test_cancel_forwards(tmp_path) -> None:
    agent = _Agent()
    client = _app(_store(tmp_path), agent=agent)
    row = _workspace(client)
    response = client.delete(
        f"/api/workspaces/{row['workspace_id']}/uploads/u1"
    )
    assert response.status_code == 200
    assert agent.cancelled == ["u1"]


def test_rollback_records_pre_and_activates_target(tmp_path) -> None:
    agent = _Agent()
    store = _store(tmp_path)
    client = _app(store, agent=agent)
    row = _workspace(client)
    target = store.add_revision(
        row["workspace_id"],
        actor="alice",
        git_commit="abc1234",
        archive_path="/srv/archive/w/r1.tar.gz",
    )
    response = client.post(
        f"/api/workspaces/{row['workspace_id']}/rollback",
        json={"revision_id": target["revision_id"]},
    )
    assert response.status_code == 200
    assert agent.rollback_payload == {
        "revision_id": target["revision_id"],
        "git_commit": "abc1234",
    }
    pre = store.get_revision("pre1")
    assert pre is not None
    assert pre["status"] == "superseded"
    assert (
        store.get_revision(target["revision_id"])["status"] == "active"
    )
    assert store.get(row["workspace_id"])["used_bytes"] == 7


def test_pin_and_prune(tmp_path) -> None:
    agent = _Agent()
    store = _store(tmp_path)
    client = _app(store, agent=agent)
    row = _workspace(client)
    revisions = [
        store.add_revision(
            row["workspace_id"],
            actor="alice",
            archive_path=f"/srv/archive/w/{index}.tar.gz",
        )
        for index in range(11)
    ]
    response = client.post(
        f"/api/workspaces/{row['workspace_id']}/revisions/prune"
    )
    assert response.status_code == 200
    pruned = response.json()["pruned"]
    assert len(pruned) == 1
    assert agent.deleted == ["/srv/archive/w/0.tar.gz"]
    assert store.get_revision(revisions[0]["revision_id"])["status"] == "pruned"

    target = revisions[1]["revision_id"]
    pinned = client.post(
        f"/api/workspaces/{row['workspace_id']}/revisions/{target}/pin",
        json={"pinned": True},
    )
    assert pinned.status_code == 200
    assert pinned.json()["pinned"] is True


def test_upload_job_from_other_workspace_is_404(tmp_path) -> None:
    agent = _Agent()
    agent.status = "active"
    client = _app(_store(tmp_path), agent=agent)
    row = _workspace(client)

    async def wrong(context, job_id):
        return {
            "job_id": job_id,
            "workspace_id": "other",
            "status": "active",
        }

    agent.workspace_upload_status = wrong  # type: ignore[method-assign]
    response = client.get(
        f"/api/workspaces/{row['workspace_id']}/uploads/u1"
    )
    assert response.status_code == 404
