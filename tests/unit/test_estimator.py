"""Estimator：规则先验、历史分位与 gate 决策。"""

from __future__ import annotations

from ubismart.orchestrator.estimator import (
    EstimationStore,
    Estimator,
    sample_from_result,
)


def _samples(count: int, *, tokens: int, wall_ms: int, tools: int):
    return [
        {
            "task_type": "parallel_readonly",
            "role": "explore",
            "model": "m1",
            "input_tokens": tokens,
            "output_tokens": 0,
            "duration_ms": wall_ms,
            "tool_calls": tools,
        }
        for _ in range(count)
    ]


def test_estimate_uses_history_percentiles_when_enough_samples() -> None:
    estimator = Estimator(min_samples=3, short_task_s=60, long_task_s=120)
    tasks = [{"id": "t1", "role": "explore"}]
    history = (
        _samples(3, tokens=1000, wall_ms=10_000, tools=4)
        + _samples(3, tokens=9000, wall_ms=90_000, tools=20)
    )

    card = estimator.estimate(
        tasks, task_type="parallel_readonly", model="m1", history=history
    )

    assert card["p50_tokens"] > 1000
    assert card["p90_tokens"] > card["p50_tokens"]
    assert card["p90_wall_ms"] >= card["p50_wall_ms"]
    assert card["confidence"] in {"med", "high"}


def test_estimate_falls_back_to_priors_without_history() -> None:
    estimator = Estimator(min_samples=3)
    tasks = [{"id": "t1", "role": "implement"}]

    card = estimator.estimate(
        tasks, task_type="orchestrated_implement", model="m1", history=[]
    )

    assert card["confidence"] == "low"
    assert card["p90_tokens"] > 0
    assert card["recommended"] in {"notice", "confirm", "split"}
    assert card["probe_required"] is True


def test_long_task_gate_enables_fine_checkpoint_and_watchdog() -> None:
    estimator = Estimator(min_samples=5, short_task_s=60, long_task_s=120)
    tasks = [{"id": "t1", "role": "implement"}]

    card = estimator.estimate(
        tasks, task_type="orchestrated_implement", model="m1", history=[]
    )
    gate = card["gate"]

    assert card["recommended"] == "confirm"
    assert gate["checkpoint_granularity"] == "fine"
    assert gate["phase_gates"] is True
    assert gate["watchdog_ms"] > 0


def test_unbounded_branch_forces_low_confidence() -> None:
    estimator = Estimator(min_samples=1)
    tasks = [{"id": "t1", "role": "explore", "branching": "unbounded"}]

    card = estimator.estimate(
        tasks,
        task_type="parallel_readonly",
        model="m1",
        history=_samples(5, tokens=1000, wall_ms=10_000, tools=4),
    )

    assert card["confidence"] == "low"


def test_sample_from_result_records_cost_fields() -> None:
    sample = sample_from_result(
        {
            "role": "explore",
            "input_tokens": 100,
            "output_tokens": 20,
            "duration_ms": 3000,
            "llm_ms": 1000,
            "tool_ms": 2000,
            "tool_calls": [{"name": "search"}, {"name": "file_read"}],
            "status": "success",
        },
        task_type="parallel_readonly",
        model="m1",
    )

    assert sample["input_tokens"] == 100
    assert sample["tool_calls"] == 2
    assert sample["task_type"] == "parallel_readonly"


def test_estimation_store_round_trips_samples(tmp_path) -> None:
    store = EstimationStore(tmp_path / "estimates.jsonl")
    store.record({"task_type": "x", "role": "explore", "model": "m"})

    assert store.load()[0]["task_type"] == "x"
