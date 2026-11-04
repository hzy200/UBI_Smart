"""M4：会话级运行租约。"""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

from ubismart.control.leases import LeaseHeartbeat, LeaseHeld, RunLeaseStore
from ubismart.control.schema import Base, RunLease
from ubismart.web.routes.runs import lease_conflict_detail
from ubismart.web.services import AgentGateway, _terminal_marker


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


def test_lease_conflict_detail_points_to_stream() -> None:
    detail = lease_conflict_detail(
        "t1", "k1", {"lease_id": "k1"}
    )
    assert detail["code"] == "run_in_progress"
    assert detail["thread_id"] == "t1"
    assert detail["stream_url"] == "/api/stream/t1"
    assert detail["idempotent"] is True
    other = lease_conflict_detail("t1", "k1", {"lease_id": "other"})
    assert "idempotent" not in other


def test_sqlite_control_store_enables_wal(tmp_path) -> None:
    """没有 WAL / busy_timeout 时，多 worker 并发写会直接 `database is locked`。"""
    from ubismart.control.store import ControlStore

    store = ControlStore(f"sqlite:///{tmp_path / 'control.db'}")
    with store.engine.connect() as conn:
        mode = str(
            conn.exec_driver_sql("PRAGMA journal_mode").scalar()
        ).lower()
        timeout = int(
            conn.exec_driver_sql("PRAGMA busy_timeout").scalar() or 0
        )
    assert mode == "wal"
    assert timeout >= 1000


def test_concurrent_acquire_grants_exactly_one_lease(tmp_path) -> None:
    """SQLite 会**静默忽略**行锁：读-改-写不是原子的话多实例会同时拿到租约。"""
    from ubismart.control.store import ControlStore

    store = ControlStore(f"sqlite:///{tmp_path / 'control.db'}")
    leases = RunLeaseStore(store.engine)
    granted: list[str] = []
    rejected: list[LeaseHeld] = []
    failures: list[BaseException] = []
    barrier = threading.Barrier(4)

    def _worker(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            granted.append(
                leases.acquire("alice:t1", f"a2-{index}", ttl_s=60)
            )
        except LeaseHeld as exc:
            rejected.append(exc)
        except BaseException as exc:  # noqa: BLE001 - 线程里要收集意外失败
            failures.append(exc)

    threads = [
        threading.Thread(target=_worker, args=(index,)) for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    # 只允许一种结局：一个人拿到、其余全部被拒。任何 OperationalError /
    # IntegrityError 泄漏都说明并发路径还在裸奔。
    assert failures == []
    assert len(granted) == 1
    assert len(rejected) == 3


def test_terminal_marker_detection() -> None:
    assert _terminal_marker(b'data: {"type": "run_finished"}')
    assert _terminal_marker(b'data: {"type":"approval_requested"}')
    assert _terminal_marker(
        b'data: {"type": "steering_applied", "action": "pause"}'
    )
    assert not _terminal_marker(b'data: {"type": "assistant_token"}')


class _CountingRenew:
    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls = 0

    def renew(self, _key: str, _lease: str, *, ttl_s: int) -> bool:
        self.calls += 1
        return self.result


def test_lease_heartbeat_renews_independently() -> None:
    """长静默工具期间没有 SSE 事件，heartbeat 仍要续租。"""
    fake = _CountingRenew()

    async def scenario() -> tuple[int, int]:
        heartbeat = LeaseHeartbeat(
            fake, "alice:t1", "lease-1", ttl_s=3, interval_s=0.02
        )
        await heartbeat.start()
        await asyncio.sleep(0.2)
        await heartbeat.stop()
        calls = fake.calls
        await asyncio.sleep(0.05)
        return calls, fake.calls

    calls, after_stop = asyncio.run(scenario())
    assert calls >= 2
    assert after_stop == calls


def test_lease_heartbeat_marks_lost() -> None:
    fake = _CountingRenew(result=False)

    async def scenario() -> bool:
        heartbeat = LeaseHeartbeat(
            fake, "alice:t1", "lease-1", ttl_s=3, interval_s=0.01
        )
        await heartbeat.start()
        # 固定 sleep 在满载 CI 上可能竞争不过事件循环；轮询到 lost 或超时。
        for _ in range(100):
            if heartbeat.lost:
                break
            await asyncio.sleep(0.01)
        await heartbeat.stop()
        return heartbeat.lost

    assert asyncio.run(scenario()) is True


def test_proxy_releases_on_stream_end_without_terminal_marker() -> None:
    """客户端断开 / 上游异常时没有 terminal 标记，也必须触发 on_terminal。"""
    fired: list[str] = []

    class _Stream:
        status_code = 200

        async def aiter_bytes(self):  # noqa: ANN202
            yield b'data: {"type": "assistant_token"}\n\n'

    async def scenario() -> None:
        gateway = object.__new__(AgentGateway)
        response = await gateway.proxy(
            _Stream(),
            public_thread="t1",
            on_terminal=lambda: fired.append("terminal"),
        )
        async for _chunk in response.body_iterator:
            pass

    asyncio.run(scenario())
    assert fired == ["terminal"]
