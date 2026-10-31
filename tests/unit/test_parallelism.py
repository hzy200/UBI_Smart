"""并行收益预估：去重、宽度与收益比。"""

from __future__ import annotations

from typing import Any

from ubismart.orchestrator.parallelism import (
    dedupe_readonly_tasks,
    parallel_stats,
    worth_parallelizing,
)


def _task(
    task_id: str,
    *,
    role: str = "explore",
    goal: str = "定位",
    inputs: list[str] | None = None,
    deps: list[str] | None = None,
    stage: int = 0,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "role": role,
        "goal": goal,
        "inputs": inputs or [],
        "depends_on": deps or [],
        "stage": stage,
    }


def test_identical_readonly_tasks_are_deduped() -> None:
    tasks, dropped = dedupe_readonly_tasks(
        [
            _task("t1", inputs=["a.py"]),
            _task("t2", inputs=["./a.py"]),
            _task("t3", inputs=["b.py"]),
        ]
    )

    assert [task["id"] for task in tasks] == ["t1", "t3"]
    assert dropped == ["t2"]


def test_duplicate_with_dependents_is_kept() -> None:
    tasks, dropped = dedupe_readonly_tasks(
        [
            _task("t1"),
            _task("t2"),
            _task("t3", role="review", deps=["t2"]),
        ]
    )

    assert [task["id"] for task in tasks] == ["t1", "t2", "t3"]
    assert dropped == []


def test_implement_tasks_are_never_deduped() -> None:
    tasks, dropped = dedupe_readonly_tasks(
        [
            _task("i1", role="implement", inputs=["src/"]),
            _task("i2", role="implement", inputs=["./src/"]),
        ]
    )

    assert len(tasks) == 2
    assert dropped == []


def test_parallel_stats_reports_width_and_gain() -> None:
    stats = parallel_stats(
        [
            _task("t1"),
            _task("t2"),
            _task("t3", deps=["t1"]),
        ]
    )

    assert stats["task_count"] == 3
    assert stats["width"] == 2
    assert stats["critical_path"] == 2
    assert stats["gain"] == 1.5


def test_serial_chain_is_not_worth_parallelizing() -> None:
    chain = [_task("t1"), _task("t2", deps=["t1"])]

    assert parallel_stats(chain)["gain"] == 1.0
    assert not worth_parallelizing(chain, 1.2)
    assert worth_parallelizing(chain, 0)


def test_independent_tasks_are_worth_parallelizing() -> None:
    independent = [_task("t1"), _task("t2")]

    assert parallel_stats(independent)["gain"] == 2.0
    assert worth_parallelizing(independent, 1.2)
