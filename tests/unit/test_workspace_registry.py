"""RFC-0001：WorkspaceStore 的 CRUD、状态机与 revision。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from coding_agent.control.schema import Base
from coding_agent.control.workspaces import (
    WorkspaceError,
    WorkspaceNotFound,
    WorkspacePathError,
    WorkspacePolicy,
    WorkspaceStatusError,
    WorkspaceStore,
)


@pytest.fixture()
def engine(tmp_path) -> Engine:
    engine = create_engine(f"sqlite:///{tmp_path / 'control.db'}", future=True)
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture()
def store(engine: Engine) -> WorkspaceStore:
    return WorkspaceStore(
        engine,
        WorkspacePolicy(
            root="/srv/ws",
            archive_root="/srv/archive",
            mount_roots=("/mnt/d",),
        ),
    )


def test_create_and_get(store: WorkspaceStore) -> None:
    created = store.create("tenant", "alice", "Demo Project")
    assert created["status"] == "provisioning"
    assert created["root_path"] == "/srv/ws/tenant/alice/demo-project"
    assert created["root_kind"] == "ext4"
    assert created["degraded_fs"] is False
    assert store.get(created["workspace_id"])["display_name"] == "Demo Project"


def test_create_conflict_adds_id_suffix(store: WorkspaceStore) -> None:
    first = store.create("tenant", "alice", "Same Name")
    second = store.create("tenant", "alice", "Same Name")
    assert first["slug"] == "same-name"
    assert second["slug"] != first["slug"]
    assert second["slug"].startswith("same-name-")


def test_create_rejects_path_outside_root(store: WorkspaceStore) -> None:
    with pytest.raises(WorkspacePathError):
        store.create(
            "tenant", "alice", "Bad", root_path="/srv/outside/x"
        )


def test_create_rejects_legacy_source(store: WorkspaceStore) -> None:
    with pytest.raises(WorkspacePathError):
        store.create("tenant", "alice", "Legacy", source="legacy")


def test_list_scopes_by_tenant_owner_and_hides_deleted(
    store: WorkspaceStore,
) -> None:
    store.create("t1", "alice", "A")
    store.create("t1", "bob", "B")
    other = store.create("t2", "alice", "C")
    assert len(store.list("t1")) == 2
    assert len(store.list("t1", owner="alice")) == 1
    assert len(store.list("t2")) == 1
    store.delete(other["workspace_id"])
    assert store.list("t2") == []
    assert len(store.list("t2", include_deleted=True)) == 1


def test_state_transitions_and_tombstone(store: WorkspaceStore) -> None:
    created = store.create("tenant", "alice", "Lifecycle")
    assert store.set_status(created["workspace_id"], "active")["status"] == "active"
    assert store.set_status(created["workspace_id"], "archived")["status"] == "archived"
    assert store.set_status(created["workspace_id"], "active")["status"] == "active"
    assert store.delete(created["workspace_id"])["status"] == "deleted"
    with pytest.raises(WorkspaceStatusError):
        store.set_status(created["workspace_id"], "active")
    with pytest.raises(WorkspaceNotFound):
        store.require(created["workspace_id"])


def test_update_fields(store: WorkspaceStore) -> None:
    created = store.create("tenant", "alice", "Update")
    updated = store.update(
        created["workspace_id"],
        display_name="Renamed",
        access_mode="auto",
        quota_bytes=1024,
        used_bytes=10,
        file_count=2,
    )
    assert updated["display_name"] == "Renamed"
    assert updated["access_mode"] == "auto"
    assert updated["quota_bytes"] == 1024
    assert updated["used_bytes"] == 10
    assert updated["file_count"] == 2
    assert updated["slug"] == created["slug"]


def test_missing_workspace_raises(store: WorkspaceStore) -> None:
    with pytest.raises(WorkspaceNotFound):
        store.require("missing")
    with pytest.raises(WorkspaceNotFound):
        store.update("missing", display_name="x")


def test_revision_supersedes_previous_active(store: WorkspaceStore) -> None:
    created = store.create("tenant", "alice", "Revisions")
    first = store.add_revision(
        created["workspace_id"],
        actor="alice",
        archive_path="/srv/archive/1.tar.zst",
        bytes=10,
    )
    second = store.add_revision(
        created["workspace_id"],
        actor="alice",
        archive_path="/srv/archive/2.tar.zst",
        bytes=20,
    )
    rows = store.list_revisions(created["workspace_id"])
    assert [row["revision_id"] for row in rows] == [
        second["revision_id"],
        first["revision_id"],
    ]
    statuses = {row["revision_id"]: row["status"] for row in rows}
    assert statuses[first["revision_id"]] == "superseded"
    assert statuses[second["revision_id"]] == "active"


def test_revision_requires_workspace(store: WorkspaceStore) -> None:
    with pytest.raises(WorkspaceNotFound):
        store.add_revision("missing", actor="alice")


def test_revision_rejects_bad_scan_status(store: WorkspaceStore) -> None:
    created = store.create("tenant", "alice", "Scan")
    with pytest.raises(WorkspaceError):
        store.add_revision(
            created["workspace_id"], scan_status="infected"
        )


def test_revisions_to_prune_keeps_newest_and_skips_pinned(
    store: WorkspaceStore,
) -> None:
    created = store.create("tenant", "alice", "Prune")
    revisions = [
        store.add_revision(
            created["workspace_id"],
            actor="alice",
            archive_path=f"/srv/archive/{index}.tar.zst",
        )
        for index in range(5)
    ]
    pinned = store.add_revision(
        created["workspace_id"], actor="alice", pinned=True
    )
    candidates = store.revisions_to_prune(created["workspace_id"], keep=2)
    candidate_ids = {row["revision_id"] for row in candidates}
    assert pinned["revision_id"] not in candidate_ids
    # 最新的两个（含 pinned）之外，最早的三条应被选中。
    assert candidate_ids == {
        revisions[0]["revision_id"],
        revisions[1]["revision_id"],
        revisions[2]["revision_id"],
    }
