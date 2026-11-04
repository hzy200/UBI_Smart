"""跑偏轨迹回归集：结构化写入、按 kind 汇总。"""

from __future__ import annotations

from ubismart.session.regression import (
    load_trajectories,
    record_trajectory,
    summarize_trajectories,
)


def test_trajectory_round_trip_and_summary(tmp_path) -> None:
    path = tmp_path / "regression.jsonl"
    record_trajectory(
        path,
        kind="abort:execution",
        thread_id="t1",
        reason="预算耗尽",
        metrics={"invalid_step_ratio": 0.5},
    )
    record_trajectory(path, kind="verify_fail", thread_id="t1")
    record_trajectory(path, kind="abort:execution", thread_id="t2")

    items = load_trajectories(path)
    assert len(items) == 3
    assert items[0]["metrics"]["invalid_step_ratio"] == 0.5
    assert summarize_trajectories(items) == {
        "abort:execution": 2,
        "verify_fail": 1,
    }
