"""RFC-0001：工作区注册表 HTTP 路由。"""

from __future__ import annotations

import pytest
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
    settings: Settings | None = None,
    agent_client: object | None = None,
) -> tuple[TestClient, dict[str, str]]:
    app = FastAPI()
    register_workspace_registry_routes(
        app,
        WorkspaceRegistryDeps(
            rate_limit=lambda _user: None,
            store=store,
            enabled=enabled,
            settings=settings or Settings(
                _env_file=None, workspace_mount_roots="/mnt/d"
            ),
            agent_client=agent_client,
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


def test_host_default_workspace_is_registered_once(tmp_path) -> None:
    """启动配置的本机目录要出现在项目列表里，否则前端无法绑定它。"""
    settings = Settings(
        _env_file=None,
        workspace_mount_roots="/mnt/d",
        workspace_kind_default="host",
        host_workspace_id="ws-local",
        host_root_handle="h_local",
        host_a2l_id="a2l-local",
    )
    client, _ = _client(_store(tmp_path), settings=settings)

    first = client.get("/api/workspaces").json()["workspaces"]
    assert len(first) == 1
    row = first[0]
    # A2L 用这个 id 查本地 roots，A2 不能换成随机 UUID。
    assert row["workspace_id"] == "ws-local"
    assert row["kind"] == "host"
    assert row["root_handle"] == "h_local"
    assert row["root_path"] == ""
    assert row["status"] == "active"

    second = client.get("/api/workspaces").json()["workspaces"]
    assert [item["workspace_id"] for item in second] == [
        row["workspace_id"]
    ]


def test_registered_host_workspace_keeps_a2l_id(tmp_path) -> None:
    class _AgentClient:
        async def register_host_workspace(self, _context):
            return {
                "workspace_id": "ws_picked",
                "root_handle": "h_picked",
                "display_name": "picked",
                "a2l_id": "a2l-local",
            }

    store = _store(tmp_path)
    client, _ = _client(store, agent_client=_AgentClient())

    first = client.post("/api/workspaces/host")
    assert first.status_code == 200
    assert first.json()["workspace_id"] == "ws_picked"

    # 重复注册同一目录时复用同一行，不产生第二个项目。
    second = client.post("/api/workspaces/host")
    assert second.status_code == 200
    assert second.json()["workspace_id"] == "ws_picked"
    rows = client.get("/api/workspaces").json()["workspaces"]
    assert [row["workspace_id"] for row in rows] == ["ws_picked"]
