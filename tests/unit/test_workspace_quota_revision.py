"""RFC-0001：配额原子更新与 revision 状态。"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from coding_agent.control.schema import Base
from coding_agent.control.workspaces import (
    WorkspaceError,
    WorkspacePolicy,
    WorkspaceQuotaExceeded,
    WorkspaceStore,
)


@pytest.fixture()
def store(tmp_path) -> WorkspaceStore:
    engine: Engine = create_engine(
        f"sqlite:///{tmp_path / 'control.db'}", future=True
    )
    Base.metadata.create_all(engine)
    return WorkspaceStore(engine, WorkspacePolicy(root="/srv/ws"))


def _workspace(store: WorkspaceStore, *, quota: int = 0) -> dict:
    return store.create(
        "t",
        "alice",
        "Quota",
        quota_bytes=quota,
        status="active",
    )


def test_add_usage_within_quota(store) -> None:
    row = _workspace(store, quota=100)
    updated = store.add_usage(row["workspace_id"], bytes=40, file_count=2)
    assert updated["used_bytes"] == 40
    assert updated["file_count"] == 2
    again = store.add_usage(row["workspace_id"], bytes=60, file_count=1)
    assert again["used_bytes"] == 100
    assert again["file_count"] == 3


def test_add_usage_exceeds_quota(store) -> None:
    row = _workspace(store, quota=10)
    with pytest.raises(WorkspaceQuotaExceeded):
        store.add_usage(row["workspace_id"], bytes=11)
    assert store.get(row["workspace_id"])["used_bytes"] == 0


def test_zero_quota_is_unlimited(store) -> None:
    row = _workspace(store, quota=0)
    updated = store.add_usage(row["workspace_id"], bytes=10**9)
    assert updated["used_bytes"] == 10**9


def test_set_usage_is_absolute(store) -> None:
    row = _workspace(store)
    store.add_usage(row["workspace_id"], bytes=50, file_count=5)
    updated = store.set_usage(
        row["workspace_id"], used_bytes=7, file_count=1
    )
    assert updated["used_bytes"] == 7
    assert updated["file_count"] == 1


def test_pinned_revision_is_not_pruned(store) -> None:
    row = _workspace(store)
    for index in range(3):
        store.add_revision(
            row["workspace_id"],
            actor="alice",
            archive_path=f"/arc/{index}.tar.gz",
            pinned=index == 0,
        )
    candidates = store.revisions_to_prune(row["workspace_id"], keep=1)
    assert len(candidates) == 1
    assert candidates[0]["archive_path"] == "/arc/1.tar.gz"


def test_activate_revision_supersedes_others(store) -> None:
    row = _workspace(store)
    first = store.add_revision(row["workspace_id"], actor="a")
    second = store.add_revision(row["workspace_id"], actor="a")
    assert store.get_revision(first["revision_id"])["status"] == "superseded"
    store.activate_revision(row["workspace_id"], first["revision_id"])
    assert store.get_revision(first["revision_id"])["status"] == "active"
    assert store.get_revision(second["revision_id"])["status"] == "superseded"


def test_pruned_revision_cannot_be_activated(store) -> None:
    row = _workspace(store)
    revision = store.add_revision(row["workspace_id"], actor="a")
    store.mark_revision_pruned(revision["revision_id"])
    with pytest.raises(WorkspaceError):
        store.activate_revision(
            row["workspace_id"], revision["revision_id"]
        )


def test_pin_toggle(store) -> None:
    row = _workspace(store)
    revision = store.add_revision(row["workspace_id"], actor="a")
    assert store.set_revision_pinned(
        revision["revision_id"], True
    )["pinned"] is True
    assert store.set_revision_pinned(
        revision["revision_id"], False
    )["pinned"] is False


def test_missing_revision(store) -> None:
    assert store.get_revision("missing") is None
