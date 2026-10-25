"""M4：会话级运行租约。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from coding_agent.control.leases import LeaseHeld, RunLeaseStore
from coding_agent.control.schema import Base, RunLease
from coding_agent.web.services import _terminal_marker


@pytest.fixture()
def store(tmp_path) -> RunLeaseStore:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'control.db'}", future=True
    )
    Base.metadata.create_all(engine)
    return RunLeaseStore(engine)


def test_acquire_and_release(store: RunLeaseStore) -> None:
    lease = store.acquire("alice:t1", "a2-1", ttl_s=60)
    assert lease
    assert store.active("alice:t1")["lease_id"] == lease
    assert store.release("alice:t1", lease) is True
    assert store.active("alice:t1") is None


def test_second_acquire_is_rejected(store: RunLeaseStore) -> None:
    store.acquire("alice:t1", "a2-1", ttl_s=60)
    with pytest.raises(LeaseHeld):
        store.acquire("alice:t1", "a2-2", ttl_s=60)


def test_same_lease_id_can_reacquire(store: RunLeaseStore) -> None:
    first = store.acquire(
        "alice:t1", "a2-1", ttl_s=60, lease_id="same"
    )
    second = store.acquire(
        "alice:t1", "a2-1", ttl_s=60, lease_id="same"
    )
    assert first == second == "same"


def test_expired_lease_can_be_taken(store: RunLeaseStore) -> None:
    lease = store.acquire("alice:t1", "a2-1", ttl_s=1)
    engine = store.engine
    past = (
        datetime.now(UTC) - timedelta(seconds=5)
    ).isoformat(timespec="microseconds")
    with Session(engine) as session, session.begin():
        session.execute(
            update(RunLease)
            .where(RunLease.session_key == "alice:t1")
            .values(expires_at=past)
        )
    assert store.active("alice:t1") is None
    replacement = store.acquire("alice:t1", "a2-2", ttl_s=60)
    assert replacement != lease


def test_renew_and_force_release(store: RunLeaseStore) -> None:
    lease = store.acquire("alice:t1", "a2-1", ttl_s=60)
    assert store.renew("alice:t1", lease, ttl_s=120) is True
    assert store.renew("alice:t1", "wrong", ttl_s=120) is False
    assert store.force_release("alice:t1") is True
    assert store.active("alice:t1") is None


def test_purge_expired(store: RunLeaseStore) -> None:
    store.acquire("alice:t1", "a2-1", ttl_s=1)
    engine = store.engine
    past = (
        datetime.now(UTC) - timedelta(seconds=5)
    ).isoformat(timespec="microseconds")
    with Session(engine) as session, session.begin():
        session.execute(
            update(RunLease).values(expires_at=past)
        )
    assert store.purge_expired() == 1
    assert store.list_active() == []


def test_terminal_marker_detection() -> None:
    assert _terminal_marker(b'data: {"type": "run_finished"}')
    assert _terminal_marker(b'data: {"type":"approval_requested"}')
    assert _terminal_marker(
        b'data: {"type": "steering_applied", "action": "pause"}'
    )
    assert not _terminal_marker(b'data: {"type": "assistant_token"}')
