"""四维预算守卫：预警、熔断与模型预算 clamp。"""

from __future__ import annotations

from ubismart.orchestrator.budget import BudgetGuard


def _result(tokens: int = 100, tool_calls: int = 2, attempt: int = 1):
    return {
        "task_id": "t1",
        "attempt": attempt,
        "input_tokens": tokens,
        "output_tokens": 0,
        "tool_calls": [{"name": "search"}] * tool_calls,
    }


def test_guard_counts_four_dimensions_without_double_counting() -> None:
    guard = BudgetGuard(
        step_limit=10,
        tool_call_limit=20,
        token_limit=1_000,
        wall_limit_ms=60_000,
        spent_tokens=100,
        results=[_result(tokens=100, tool_calls=2)],
    )
    guard.consume(_result(tokens=100, tool_calls=2))
    guard.consume(_result(tokens=100, tool_calls=2))

    snapshot = guard.snapshot()
    assert snapshot.steps == 1, "同一 (task_id, attempt) 只计一次"
    assert snapshot.tool_calls == 2
    assert snapshot.tokens == 100


def test_guard_warns_at_70_percent_and_exhausts_at_100() -> None:
    guard = BudgetGuard(
        step_limit=10,
        tool_call_limit=10,
        token_limit=100,
        wall_limit_ms=60_000,
        spent_tokens=70,
    )
    warning = guard.check()
    assert "tokens" in warning.warning
    assert warning.exhausted == []

    guard.refresh_tokens(spent=100, reserved=0)
    exhausted = guard.check()
    assert "tokens" in exhausted.exhausted


def test_model_budget_is_clamped_to_parent_remaining() -> None:
    guard = BudgetGuard(
        token_limit=1_000,
        spent_tokens=600,
        reserved_tokens=200,
        wall_limit_ms=10_000,
    )

    assert guard.remaining_tokens() == 200
    assert guard.clamp_max_tokens(50_000) == 200
    assert guard.clamp_timeout(9_999) <= 10.0


def test_unlimited_budget_does_not_clamp() -> None:
    guard = BudgetGuard(token_limit=0, wall_limit_ms=0)

    assert guard.remaining_tokens() is None
    assert guard.clamp_max_tokens(50_000) == 50_000
    assert guard.clamp_timeout(9_999) == 9_999


def test_abort_report_contains_completed_blocked_and_suggestion() -> None:
    guard = BudgetGuard(step_limit=1, results=[_result()])
    report = guard.abort_report(
        completed_tasks=["t1"], blocked_tasks=["t2"]
    )

    assert report["completed_tasks"] == ["t1"]
    assert report["blocked_tasks"] == ["t2"]
    assert report["suggestion"]
    assert "预算耗尽" in report["reason"]
