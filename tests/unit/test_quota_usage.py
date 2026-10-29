"""用量幂等累加与每日配额预留。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
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


def test_concurrent_reservations_do_not_oversubscribe(tmp_path) -> None:
    """两个并发预留只能有一个成功；SQLite 忽略行锁时也不能双双通过。"""
    store = _store(tmp_path)
    store.set_quota(
        "default",
        "alice",
        daily_tokens=100,
        daily_cost_cny=7.2,
        enabled=True,
    )

    def reserve(_index: int) -> int:
        try:
            return store.reserve_quota(
                "default", "alice", DAY, tokens=60
            )["tokens"]
        except QuotaExceeded:
            return 0

    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(reserve, range(2)))
    assert sorted(values) == [0, 60]
    assert (
        store.get_usage_day("default", "alice", DAY)["reserved_tokens"]
        == 60
    )
    store.close()


def test_concurrent_settle_releases_once(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_quota(
        "default",
        "alice",
        daily_tokens=100,
        daily_cost_cny=7.2,
        enabled=True,
    )
    reservation = store.reserve_quota(
        "default", "alice", DAY, tokens=60
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(
            pool.map(
                lambda _index: store.settle_quota(reservation["id"]),
                range(2),
            )
        )
    assert (
        store.get_usage_day("default", "alice", DAY)["reserved_tokens"]
        == 0
    )
    store.close()


def test_concurrent_expired_purge_reclaims_once(tmp_path) -> None:
    store = _store(tmp_path)
    store.set_quota(
        "default",
        "alice",
        daily_tokens=100,
        daily_cost_cny=7.2,
        enabled=True,
    )
    store.reserve_quota("default", "alice", DAY, tokens=100, ttl_s=1)
    with ThreadPoolExecutor(max_workers=2) as pool:
        counts = list(
            pool.map(
                lambda _index: store.purge_expired_reservations(
                    "9999-12-31T23:59:59+00:00"
                ),
                range(2),
            )
        )
    assert sum(counts) == 1
    assert (
        store.get_usage_day("default", "alice", DAY)["reserved_tokens"]
        == 0
    )
    store.close()


def test_concurrent_check_only_reservations_do_not_race(tmp_path) -> None:
    """默认 reserve=0 时只做运行前检查，首次建行也不能撞唯一键。"""
    store = _store(tmp_path)
    store.set_quota(
        "default",
        "alice",
        daily_tokens=100,
        daily_cost_cny=7.2,
        enabled=True,
    )
    failures: list[BaseException] = []

    def check(_index: int) -> None:
        try:
            store.reserve_quota("default", "alice", DAY)
        except BaseException as exc:  # noqa: BLE001 - 线程里收集失败
            failures.append(exc)

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(check, range(4)))
    assert failures == []
    assert (
        store.get_usage_day("default", "alice", DAY)["reserved_tokens"]
        == 0
    )
    store.close()


def test_concurrent_usage_updates_are_not_lost(tmp_path) -> None:
    store = _store(tmp_path)

    def add(index: int) -> None:
        store.record_usage(
            "default",
            "alice",
            [
                {
                    "kind": "run_end",
                    "hash": f"h{index}",
                    "thread_id": f"t{index}",
                    "run_epoch": "e1",
                    "ts": f"{DAY}T10:00:00+00:00",
                    "input_tokens": 10,
                }
            ],
        )

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(add, range(8)))
    usage = store.get_usage_day("default", "alice", DAY)
    assert usage["tokens"] == 80
    assert usage["runs"] == 8
    store.close()


def test_concurrent_first_cursor_insert_does_not_double_count(
    tmp_path, monkeypatch
) -> None:
    """两个"进程"同时看到 cursor 没有行时，不能各按全量累加。

    真实环境里 `_USAGE_LOCK` 把同进程的写入串行化了，MySQL 上还有行锁 ——
    这里**故意把进程内锁换掉**并放大窗口，模拟"SQLite 上没有行锁、两个进程
    同时读到空游标"的最坏情形。

    收敛靠的是：唯一键冲突 → 整个事务回滚 → 重试时读到对方已提交的 cursor，
    按差值补。所以这条用例守的不是某个实现细节，而是**这条收敛路径还在**：
    有人把重试删掉、或把游标插入改成 `INSERT OR REPLACE`，就会静默多记一次。
    """
    import contextlib
    import threading
    import time

    from coding_agent.control import store as store_module

    monkeypatch.setattr(store_module, "_USAGE_LOCK", contextlib.nullcontext())
    original_daily_add = store_module.ControlStore._daily_add

    def slow_daily_add(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        time.sleep(0.15)  # 让两边都完成 SELECT 之后再写
        return original_daily_add(self, *args, **kwargs)

    monkeypatch.setattr(
        store_module.ControlStore, "_daily_add", slow_daily_add
    )

    url = f"sqlite:///{tmp_path / 'control.db'}"
    first = store_module.ControlStore(url)
    second = store_module.ControlStore(url)
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def write(store, record) -> None:  # noqa: ANN001
        try:
            barrier.wait(timeout=5)
            store.record_usage("default", "alice", [record])
        except BaseException as exc:  # noqa: BLE001 - 失败也要收集，不能挂死
            errors.append(exc)

    first_thread = threading.Thread(
        target=write,
        args=(first, _record(digest="h1", epoch="e1", input_tokens=40)),
    )
    second_thread = threading.Thread(
        target=write,
        args=(second, _record(digest="h2", epoch="e1", input_tokens=100)),
    )
    first_thread.start()
    second_thread.start()
    first_thread.join(timeout=10)
    second_thread.join(timeout=10)

    assert errors == []
    usage = first.get_usage_day("default", "alice", DAY)
    # 同一 epoch 的两次快照是"累计值"，正确答案是较大的那个，不是两者之和。
    assert usage["input_tokens"] == 100
    assert usage["runs"] == 1
    first.close()
    second.close()


def test_usage_report_filters_user_and_pages_offset(tmp_path) -> None:
    store = _store(tmp_path)
    store.record_usage(
        "default", "alice", [_record(digest="u1", epoch="e1", input_tokens=10)]
    )
    store.record_usage(
        "default", "bob", [_record(digest="u2", epoch="e1", input_tokens=20)]
    )
    report = store.usage_report("default", DAY, DAY, user="bob")
    assert [row["user"] for row in report["rows"]] == ["bob"]
    assert report["totals"]["tokens"] == 20

    report = store.usage_report("default", DAY, DAY, limit=1, offset=1)
    assert len(report["rows"]) == 1
    store.close()
