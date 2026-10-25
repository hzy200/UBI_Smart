"""RFC-0001：工作区注册表 HTTP 路由。"""

from __future__ import annotations

import pytest
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


def _store(tmp_path) -> WorkspaceStore:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'control.db'}", future=True
    )
    Base.metadata.create_all(engine)
    return WorkspaceStore(
        engine,
        WorkspacePolicy(root="/srv/ws", mount_roots=("/mnt/d",)),
    )


def _client(
    store: WorkspaceStore | None,
    *,
    enabled: bool = True,
    user: str = "alice",
) -> tuple[TestClient, dict[str, str]]:
    app = FastAPI()
    register_workspace_registry_routes(
        app,
        WorkspaceRegistryDeps(
            rate_limit=lambda _user: None,
            store=store,
            enabled=enabled,
            settings=Settings(
                _env_file=None, workspace_mount_roots="/mnt/d"
            ),
            agent_client=None,
        ),
    )
    holder = {"user": user}
    app.dependency_overrides[current_user] = lambda: AuthUser(
        name=holder["user"]
    )
    return TestClient(app), holder


def test_create_managed_workspace_is_provisioning(tmp_path) -> None:
    client, _ = _client(_store(tmp_path))
    response = client.post(
        "/api/workspaces",
        json={"display_name": "Demo", "source": "upload"},
    )
    assert response.status_code == 200
    row = response.json()
    assert row["status"] == "provisioning"
    assert row["root_path"] == "/srv/ws/alice/alice/demo"
    assert row["tenant"] == "alice"
    assert row["owner"] == "alice"


def test_create_local_import_with_root_is_active(tmp_path) -> None:
    client, _ = _client(_store(tmp_path))
    response = client.post(
        "/api/workspaces",
        json={
            "display_name": "Imported",
            "source": "local_import",
            "root_path": "/srv/ws/alice/alice/imported",
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "active"


def test_managed_source_rejects_manual_root(tmp_path) -> None:
    client, _ = _client(_store(tmp_path))
    response = client.post(
        "/api/workspaces",
        json={
            "display_name": "Demo",
            "source": "upload",
            "root_path": "/srv/ws/alice/alice/demo",
        },
    )
    assert response.status_code == 400


def test_create_rejects_outside_root(tmp_path) -> None:
    client, _ = _client(_store(tmp_path))
    response = client.post(
        "/api/workspaces",
        json={
            "display_name": "Bad",
            "source": "local_import",
            "root_path": "/etc",
        },
    )
    assert response.status_code == 400


def test_get_update_and_delete(tmp_path) -> None:
    client, _ = _client(_store(tmp_path))
    created = client.post(
        "/api/workspaces",
        json={"display_name": "Lifecycle", "source": "upload"},
    ).json()

    fetched = client.get(f"/api/workspaces/{created['workspace_id']}")
    assert fetched.status_code == 200
    assert fetched.json()["display_name"] == "Lifecycle"

    updated = client.patch(
        f"/api/workspaces/{created['workspace_id']}",
        json={"display_name": "Renamed", "quota_bytes": 1024},
    )
    assert updated.status_code == 200
    assert updated.json()["display_name"] == "Renamed"
    assert updated.json()["quota_bytes"] == 1024

    deleted = client.delete(
        f"/api/workspaces/{created['workspace_id']}"
    )
    assert deleted.status_code == 200
    assert deleted.json()["status"] == "deleted"
    assert client.get("/api/workspaces").json()["workspaces"] == []
    assert (
        len(
            client.get("/api/workspaces?include_deleted=true").json()[
                "workspaces"
            ]
        )
        == 1
    )


def test_tenant_isolation(tmp_path) -> None:
    client, holder = _client(_store(tmp_path), user="alice")
    created = client.post(
        "/api/workspaces",
        json={"display_name": "Secret", "source": "upload"},
    ).json()

    holder["user"] = "bob"
    assert client.get("/api/workspaces").json()["workspaces"] == []
    assert (
        client.get(f"/api/workspaces/{created['workspace_id']}").status_code
        == 404
    )
    assert (
        client.delete(f"/api/workspaces/{created['workspace_id']}").status_code
        == 404
    )


def test_registry_disabled_returns_404(tmp_path) -> None:
    client, _ = _client(_store(tmp_path), enabled=False)
    assert client.get("/api/workspaces").status_code == 404
    assert (
        client.post(
            "/api/workspaces", json={"display_name": "X"}
        ).status_code
        == 404
    )


def test_registry_without_store_returns_503() -> None:
    client, _ = _client(None, enabled=True)
    assert client.get("/api/workspaces").status_code == 503


def test_empty_display_name_is_rejected(tmp_path) -> None:
    client, _ = _client(_store(tmp_path))
    response = client.post(
        "/api/workspaces", json={"display_name": "", "source": "upload"}
    )
    assert response.status_code == 422


@pytest.mark.parametrize("source", ["upload", "template"])
def test_template_sources_generate_root(tmp_path, source: str) -> None:
    client, _ = _client(_store(tmp_path))
    response = client.post(
        "/api/workspaces",
        json={"display_name": "Scaffold", "source": source},
    )
    assert response.status_code == 200
    assert response.json()["root_path"].startswith("/srv/ws/alice/alice/")
