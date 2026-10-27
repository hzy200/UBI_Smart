"""A2L 生命周期：单实例、暂停/离线、随客户端启停。"""

from __future__ import annotations

import json
import os

from coding_agent.host_broker.lifecycle import (
    STATE_OFFLINE,
    STATE_PAUSED,
    STATE_RUNNING,
    STATE_STOPPED,
    BrokerLifecycle,
)


def test_start_stop_and_single_instance(tmp_path) -> None:
    lock = tmp_path / "a2l.lock"
    first = BrokerLifecycle(lock, client_id="c-1")
    second = BrokerLifecycle(lock, client_id="c-2")

    assert first.start() is True
    assert first.state == STATE_RUNNING
    assert second.start() is False
    assert second.state == STATE_STOPPED

    first.stop()
    assert first.state == STATE_STOPPED
    assert second.start() is True
    second.stop()


def test_pause_resume_and_offline(tmp_path) -> None:
    lifecycle = BrokerLifecycle(tmp_path / "a2l.lock")
    assert lifecycle.start() is True
    assert lifecycle.can_serve() is True

    lifecycle.pause()
    assert lifecycle.state == STATE_PAUSED
    assert lifecycle.can_serve() is False
    lifecycle.resume()
    assert lifecycle.state == STATE_RUNNING

    lifecycle.mark_offline()
    assert lifecycle.state == STATE_OFFLINE
    assert lifecycle.can_serve() is False
    lifecycle.mark_online()
    assert lifecycle.state == STATE_RUNNING
    lifecycle.stop()


def test_stale_lock_is_recovered(tmp_path) -> None:
    lock = tmp_path / "a2l.lock"
    lock.write_text(
        json.dumps({"pid": 999_999_999, "client_id": "stale"}),
        encoding="utf-8",
    )
    lifecycle = BrokerLifecycle(lock)
    assert lifecycle.start() is True
    assert lifecycle.can_serve() is True
    lifecycle.stop()


def test_snapshot_reports_no_system_service(tmp_path) -> None:
    lifecycle = BrokerLifecycle(tmp_path / "a2l.lock", client_id="c-1")
    lifecycle.start()
    snapshot = lifecycle.snapshot()
    assert snapshot.state == STATE_RUNNING
    assert snapshot.serving is True
    assert snapshot.client_id == "c-1"
    assert snapshot.pid == os.getpid()
    assert snapshot.lock_path.endswith("a2l.lock")
    lifecycle.stop()
