"""M4：短期幂等键。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from ubismart.control.idempotency import (
    IdempotencyInFlight,
    IdempotencyStore,
    normalize_key,
)
from ubismart.control.schema import Base, IdempotencyKey


@pytest.fixture()
def store(tmp_path) -> IdempotencyStore:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'control.db'}", future=True
    )
    Base.metadata.create_all(engine)
    return IdempotencyStore(engine)


def test_begin_finish_replays(store: IdempotencyStore) -> None:
    assert store.begin("k1", "scope") is None
    store.finish("k1", {"ok": True, "value": 1})
    assert store.begin("k1", "scope") == {"ok": True, "value": 1}


def test_in_flight_is_rejected(store: IdempotencyStore) -> None:
    assert store.begin("k1", "scope") is None
    with pytest.raises(IdempotencyInFlight):
        store.begin("k1", "scope")


def test_abort_allows_retry(store: IdempotencyStore) -> None:
    assert store.begin("k1", "scope") is None
    store.abort("k1")
    assert store.begin("k1", "scope") is None


def test_expired_key_can_be_reused(store: IdempotencyStore) -> None:
    assert store.begin("k1", "scope", ttl_s=600) is None
    store.finish("k1", {"ok": True})
    engine = store.engine
    past = (
        datetime.now(UTC) - timedelta(seconds=5)
    ).isoformat(timespec="microseconds")
    with Session(engine) as session, session.begin():
        session.execute(
            update(IdempotencyKey)
            .where(IdempotencyKey.key == "k1")
            .values(expires_at=past)
        )
    assert store.begin("k1", "scope") is None


def test_purge_expired(store: IdempotencyStore) -> None:
    store.begin("k1", "scope")
    engine = store.engine
    past = (
        datetime.now(UTC) - timedelta(seconds=5)
    ).isoformat(timespec="microseconds")
    with Session(engine) as session, session.begin():
        session.execute(
            update(IdempotencyKey).values(expires_at=past)
        )
    assert store.purge_expired() == 1


def test_normalize_key() -> None:
    assert normalize_key("") == ""
    assert normalize_key("  abc  ") == "abc"
    with pytest.raises(ValueError):
        normalize_key("x" * 129)
