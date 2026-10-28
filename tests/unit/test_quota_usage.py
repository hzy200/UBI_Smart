"""用量幂等累加与每日配额预留。"""

from __future__ import annotations

from pathlib import Path

import pytest

from coding_agent.control.store import ControlStore, QuotaExceeded

DAY = "2026-10-28"


def _store(tmp_path: Path) -> ControlStore:
    return ControlStore(f"sqlite:///{tmp_path / 'control.db'}")


def _record(
    *,
    digest: str,
    epoch: str,
    input_tokens: int,
    output_tokens: int = 0,
    worker_input: int = 0,
    cost: float = 0.0,
) -> dict:
    return {
        "kind": "run_end",
        "hash": digest,
        "thread_id": "t1",
        "run_epoch": epoch,
        "ts": f"{DAY}T10:00:00+00:00",
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "worker_input_tokens": worker_input,
        "cost_usd": cost,
        "billed_usd": cost,
    }


def test_resume_usage_is_counted_by_delta(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_usage(
        "default", "alice", [_record(digest="h1", epoch="e1", input_tokens=40)]
    )
    # 重复审计记录不重复计费
    store.record_usage(
        "default", "alice", [_record(digest="h1", epoch="e1", input_tokens=40)]
    )
    # resume 后的 run_end 是同一 epoch 的累计值
    store.record_usage(
        "default",
        "alice",
        [
            _record(
                digest="h2",
                epoch="e1",
                input_tokens=60,
                output_tokens=10,
                worker_input=5,
            )
        ],
    )
    usage = store.get_usage_day("default", "alice", DAY)
    assert usage["input_tokens"] == 60
    assert usage["output_tokens"] == 10
    assert usage["worker_input_tokens"] == 5
    assert usage["tokens"] == 75
    assert usage["runs"] == 1

    # 新用户消息 -> 新 epoch -> 再计一次运行
    store.record_usage(
        "default",
        "alice",
        [_record(digest="h3", epoch="e2", input_tokens=5, cost=0.2)],
    )
    usage = store.get_usage_day("default", "alice", DAY)
    assert usage["tokens"] == 80
    assert usage["runs"] == 2
    assert usage["cost_usd"] == pytest.approx(0.2)

    report = store.usage_report("default", DAY, DAY)
    assert report["totals"]["tokens"] == 80
    assert report["rows"][0]["user"] == "alice"
    store.close()


def test_usage_report_pages_rows_but_keeps_full_totals(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_usage(
        "default",
        "alice",
        [_record(digest="p1", epoch="e1", input_tokens=10)],
    )
    store.record_usage(
        "default",
        "bob",
        [_record(digest="p2", epoch="e1", input_tokens=20)],
    )
    report = store.usage_report(
        "default", DAY, DAY, limit=1, offset=0
    )
    assert len(report["rows"]) == 1
    assert report["totals"]["tokens"] == 30
    store.close()


def test_quota_reserve_and_settle(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_quota(
        "default",
        "alice",
        daily_tokens=100,
        daily_cost_cny=7.2,
        enabled=True,
    )
    reservation = store.reserve_quota(
        "default",
        "alice",
        DAY,
        tokens=60,
        cost_usd=0.4,
        usd_to_cny=7.2,
    )
    assert reservation["tokens"] == 60
    assert reservation["cost_usd"] == pytest.approx(0.4)
    assert reservation["id"]
    with pytest.raises(QuotaExceeded) as exc:
        store.reserve_quota("default", "alice", DAY, tokens=50)
    assert exc.value.details["kind"] == "tokens"

    store.settle_quota(reservation["id"])
    usage = store.get_usage_day("default", "alice", DAY)
    assert usage["reserved_tokens"] == 0
    assert usage["reserved_usd"] == pytest.approx(0.0)
    store.close()


def test_disabled_quota_never_blocks(tmp_path) -> None:
    store = _store(tmp_path)
    reservation = store.reserve_quota(
        "default", "alice", DAY, tokens=10_000, cost_usd=100
    )
    assert reservation["tokens"] == 0
    assert reservation["cost_usd"] == 0.0
    store.close()


def test_expired_reservation_is_reclaimed(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_quota(
        "default",
        "alice",
        daily_tokens=100,
        daily_cost_cny=7.2,
        enabled=True,
    )
    store.reserve_quota(
        "default", "alice", DAY, tokens=100, ttl_s=1
    )
    assert store.get_usage_day("default", "alice", DAY)["reserved_tokens"] == 100
    store.purge_expired_reservations("9999-12-31T23:59:59+00:00")
    assert store.get_usage_day("default", "alice", DAY)["reserved_tokens"] == 0
    store.close()


def test_settle_only_releases_own_reservation(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_quota(
        "default",
        "alice",
        daily_tokens=100,
        daily_cost_cny=7.2,
        enabled=True,
    )
    first = store.reserve_quota(
        "default", "alice", DAY, tokens=40, owner="instance-a"
    )
    store.reserve_quota(
        "default", "alice", DAY, tokens=40, owner="instance-b"
    )
    store.settle_quota(first["id"])
    usage = store.get_usage_day("default", "alice", DAY)
    assert usage["reserved_tokens"] == 40
    store.close()
