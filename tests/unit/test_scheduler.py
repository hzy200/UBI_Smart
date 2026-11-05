"""事件驱动 DAG 调度器：依赖即时解锁、失败短路、动态扩展与重试。"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from typing import Any

import pytest

from ubismart.config import Settings
from ubismart.knowledge.store import current_knowledge_run_id
from ubismart.llm.ratelimit import GLOBAL_LIMITER
from ubismart.orchestrator import scheduler_run as scheduler_runtime
from ubismart.orchestrator.contracts import WorkerResult
from ubismart.orchestrator.scheduler import make_scheduler_node
from ubismart.session.steering import SteeringCommand, SteeringController
from ubismart.session.worker_journal import (
    WorkerJournal,
    request_fingerprint,
    task_fingerprint,
    workspace_fingerprint,
)


@pytest.fixture(autouse=True)
def _reset_global_limiter():
    """scheduler 的 429 会写进程级冷却；不让跨用例污染。"""
    GLOBAL_LIMITER.configure(None)
    GLOBAL_LIMITER.reset()
    yield
    GLOBAL_LIMITER.reset()


def _settings(**overrides: Any) -> Settings:
    base = {
        "orchestrator_max_concurrent_workers": 2,
        "orchestrator_task_retries": 0,
        "orchestrator_checkpoint_every": 20,
        "orchestrator_run_deadline_s": 0,
        "orchestrator_max_tasks": 6,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _task(
    task_id: str,
    *,
    deps: list[str] | None = None,
    role: str = "explore",
    write_scope: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": task_id,
        "role": role,
        "goal": task_id,
        "inputs": [],
        "acceptance": [],
        "depends_on": deps or [],
        "depends_mode": "full",
        "write_scope": write_scope or [],
    }


def _state(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "request": "做事",
        "cwd": "/ws",
        "tasks": tasks,
        "task_status": {str(task["id"]): "pending" for task in tasks},
        "task_attempts": {},
        "worker_results": [],
        "orchestrator_round": 0,
        "task_extensions_seen": 0,
    }


def _result(
    task: dict[str, Any],
    *,
    status: str = "success",
    patch_path: str = "",
) -> WorkerResult:
    return WorkerResult(
        task_id=str(task["id"]),
        role=str(task["role"]),
        worker_id=f"{task['role']}:{task['id']}",
        goal=str(task["goal"]),
        status=status,  # type: ignore[arg-type]
        summary=status,
        patch_path=patch_path,
    )


def test_dependent_starts_as_soon_as_its_predecessor_finishes() -> None:
    # 记录**执行顺序**而不是时间戳：满载 runner 上单调时钟粒度可能到
    # 15.6ms，先后事件会取到同一个值，严格 `<` 会误判为回归。
    timeline: list[tuple[str, str]] = []
    b_started = asyncio.Event()

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        task_id = str(task["id"])
        timeline.append(("start", task_id))
        if task_id == "a":
            await asyncio.sleep(0.05)
        elif task_id == "c":
            # 让 c 必须等 b 先启动：旧实现（等 c 结束才启动 b）会在这里
            # 超时，从而由下面的顺序断言稳定地暴露出来。
            try:
                await asyncio.wait_for(b_started.wait(), timeout=15.0)
            except TimeoutError:
                pass
        elif task_id == "b":
            b_started.set()
        timeline.append(("end", task_id))
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b", deps=["a"]), _task("c")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    starts = {
        task_id: index
        for index, (kind, task_id) in enumerate(timeline)
        if kind == "start"
    }
    ends = {
        task_id: index
        for index, (kind, task_id) in enumerate(timeline)
        if kind == "end"
    }
    # 旧实现会等 c 结束才启动 b；事件驱动下 b 应在 a 结束后立即启动。
    assert starts["b"] < ends["c"]
    assert out["task_status"] == {"a": "success", "b": "success", "c": "success"}


def test_worker_receives_a_hard_token_slice() -> None:
    seen: list[tuple[str, int | None]] = []

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        seen.append((str(task["id"]), task.get("max_tokens")))
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_total_tokens=5_000),
        steering=SteeringController(),
    )
    asyncio.run(
        node(
            _state([_task("a")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    # explore 的预留是 2000；全局预算 5000 足以放行，切片随 worker 下传。
    assert seen == [("a", 2_000)]


def test_document_worker_gets_role_budget_not_admission_reserve() -> None:
    seen: list[tuple[str, int | None]] = []

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        seen.append((str(task["id"]), task.get("max_tokens")))
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_total_tokens=500_000),
        steering=SteeringController(),
    )
    asyncio.run(
        node(
            _state([_task("d", role="document")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    # document 的准入预留只有 6k，但硬上限应取角色总预算 120k。
    assert seen == [("d", 120_000)]


def test_failure_blocks_dependents_without_waiting_for_the_whole_batch() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        if task["id"] == "a":
            return _result(task, status="failed")
        await asyncio.sleep(0.05)
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_task_retries=0),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b", deps=["a"]), _task("c")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert out["task_status"]["a"] == "failed"
    assert out["task_status"]["b"] == "blocked"
    assert out["task_status"]["c"] == "success"


def test_dynamic_readonly_proposal_is_scheduled() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        result = _result(task)
        if task["id"] == "a":
            result.proposed_tasks = [
                {"role": "review", "goal": "补一轮审查", "inputs": []}
            ]
        return result

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert [r["task_id"] for r in out["worker_results"]] == ["a", "ext-1"]
    assert out["task_extensions_applied"][0]["id"] == "ext-1"


def test_retry_carries_the_previous_failure_context() -> None:
    seen: list[dict[str, Any]] = []

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        seen.append(dict(task))
        if len(seen) == 1:
            result = _result(task, status="failed")
            result.summary = "第一次失败"
            return result
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_task_retries=1),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert len(seen) == 2
    assert seen[0]["escalate"] is False
    assert seen[1]["escalate"] is True
    assert seen[1]["retry_context"]["summary"] == "第一次失败"
    assert out["task_attempts"]["a"] == 2
    assert out["task_status"]["a"] == "success"


def test_worker_runs_with_the_knowledge_context() -> None:
    seen: list[str] = []

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        seen.append(current_knowledge_run_id())
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(),
        steering=SteeringController(),
    )
    asyncio.run(
        node(
            {
                **_state([_task("a")]),
                "knowledge_run_id": "run-1",
            },
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert seen == ["run-1"]


def test_worker_outer_timeout_bounds_a_hanging_runner() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        await asyncio.sleep(30)
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_worker_hard_timeout_s=0.05),
        steering=SteeringController(),
    )
    started = time.monotonic()
    out = asyncio.run(
        node(
            _state([_task("a")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert time.monotonic() - started < 5
    assert out["task_status"]["a"] == "failed"
    assert out["worker_results"][0]["timed_out"] is True
    assert "外层超时" in out["worker_results"][0]["summary"]


def test_completed_implement_is_not_relaunched_while_waiting_for_merge(
    monkeypatch,
) -> None:
    """一个 implement 完成、另一个还在跑时，前者只能等合并，不能被重排。"""
    async def fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        return {
            record["task_id"]: (True, "", ["snap"]) for record in records
        }

    monkeypatch.setattr(
        "ubismart.orchestrator.merge_queue.merge_implement_patches_atomic",
        fake_merge,
    )
    calls: dict[str, int] = {}

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        task_id = str(task["id"])
        calls[task_id] = calls.get(task_id, 0) + 1
        await asyncio.sleep(0.01 if task_id == "a" else 0.08)
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_max_concurrent_workers=2),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state(
                [
                    _task("a", role="implement"),
                    _task("b", role="implement"),
                ]
            ),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert calls == {"a": 1, "b": 1}
    assert out["task_status"] == {"a": "success", "b": "success"}
    # 合并进了主工作区：必须把「待验证」交给父图，而不是只置 dirty。
    assert out["dirty"] is True
    assert out["run_dirty"] is True
    assert out["pending_verification"] is True


def test_merge_backpressure_limits_concurrent_implement(monkeypatch) -> None:
    async def fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        return {
            record["task_id"]: (True, "", ["snap"]) for record in records
        }

    monkeypatch.setattr(
        "ubismart.orchestrator.merge_queue.merge_implement_patches_atomic",
        fake_merge,
    )
    active = 0
    max_active = 0

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        nonlocal active, max_active
        if task["role"] == "implement":
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_max_concurrent_workers=4,
            orchestrator_max_pending_merges=1,
        ),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state(
                [
                    _task("i1", role="implement"),
                    _task("i2", role="implement"),
                ]
            ),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert max_active == 1
    assert out["task_status"] == {"i1": "success", "i2": "success"}


def test_checkpoint_boundary_does_not_cap_launches() -> None:
    """边界只控制“何时返回”，不再限制“这一边界能启动几个”。"""
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_max_concurrent_workers=4,
            orchestrator_checkpoint_every=2,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b"), _task("c"), _task("d")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    # 旧实现每边界最多启动 checkpoint_every 个，只会跑 2 个。
    assert len(out["worker_results"]) == 4
    assert out["scheduler_done"] is True


def test_checkpoint_boundary_returns_continue_for_remaining_tasks() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_checkpoint_every=2),
        steering=SteeringController(),
    )
    first = asyncio.run(
        node(
            _state([_task("a"), _task("b"), _task("c")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert first["scheduler_continue"] is True
    assert len(first["worker_results"]) == 2

    second = asyncio.run(
        node(
            {**_state([_task("a"), _task("b"), _task("c")]), **first},
            {"configurable": {"thread_id": "t"}},
        )
    )
    assert second["scheduler_done"] is True
    assert len(second["worker_results"]) == 3


def test_total_token_budget_blocks_tasks_that_really_do_not_fit() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        result = _result(task)
        result.input_tokens = 2_000
        return result

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_total_tokens=2_200,
            orchestrator_max_concurrent_workers=1,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    # 第一个 explore 预留 2000 并跑完（实际 2000）；第二个的 EWMA 估算
    # 已经是 2400，放不进剩余预算。
    assert out["task_status"]["a"] == "success"
    assert out["task_status"]["b"] == "blocked"
    assert out["token_budget_exhausted"] is True
    assert out["token_budget_limit"] == 2_200
    assert out["token_budget_spent"] == 2_000
    assert out["token_budget_reserved"] == 0


def test_observed_usage_lets_a_small_task_fit_where_static_reserve_would_not() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        result = _result(task)
        result.input_tokens = 100
        return result

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_total_tokens=2_100,
            orchestrator_max_concurrent_workers=1,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    # 静态 2000 的预留会把第二个卡死；用实际用量（100 → 估算 500）后两者都能跑。
    assert out["task_status"] == {"a": "success", "b": "success"}
    assert out["token_budget_exhausted"] is False
    assert out["token_budget_spent"] == 200


def test_token_budget_defaults_to_500k() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert out["task_status"] == {"a": "success", "b": "success"}
    assert out["token_budget_exhausted"] is False
    assert out["token_budget_limit"] == 500_000


def test_budget_exhaustion_cancels_readonly_inflight_but_keeps_implement() -> None:
    """预算耗尽：停派发、取消只读在飞；implement 的成果仍然保留。"""

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        if task["id"] == "a":
            await asyncio.sleep(0.02)
            result = _result(task)
            result.input_tokens = 2_000
            return result
        await asyncio.sleep(10)
        return _result(task)

    tasks = []
    for task_id, role in (("a", "implement"), ("b", "explore"), ("c", "explore")):
        task = _task(task_id, role=role)
        task["max_tokens"] = 1_000
        tasks.append(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_total_tokens=2_500,
            orchestrator_max_concurrent_workers=2,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state(tasks),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert out["token_budget_exhausted"] is True
    assert out["task_status"]["a"] == "success"
    assert out["task_status"]["b"] == "cancelled"
    assert out["task_status"]["c"] == "blocked"


def test_rate_limit_backoff_requeues_the_task() -> None:
    attempts: dict[str, int] = {}

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        task_id = str(task["id"])
        attempts[task_id] = attempts.get(task_id, 0) + 1
        if attempts[task_id] == 1:
            result = _result(task, status="failed")
            result.summary = "429 Too Many Requests"
            return result
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_task_retries=1,
            orchestrator_rate_limit_backoff_s=0.01,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert attempts["a"] == 2
    assert out["task_status"]["a"] == "success"


def test_rate_limit_state_is_exposed_for_the_next_boundary() -> None:
    """429 之后 scheduler 返回的 state 必须带着降并发结果。"""
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        result = _result(task, status="failed")
        result.summary = "429 Too Many Requests"
        return result

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_max_concurrent_workers=4,
            orchestrator_task_retries=0,
            orchestrator_rate_limit_backoff_s=0.01,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    saved = out["scheduler_rate_limit"]
    assert saved["current_limit"] < 4
    assert saved["streak"] >= 1
    assert saved["not_before_wall"] > 0


def test_deadline_does_not_finish_a_worker_that_never_started(
    monkeypatch,
) -> None:
    """排队等 semaphore 的任务不能只发 finished、不发 started。"""
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scheduler_runtime, "get_stream_writer", lambda: events.append
    )

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        # 第一个 worker 持住唯一的并发槽，直到 deadline 把它 cancel。
        await asyncio.sleep(5)
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_max_concurrent_workers=1,
            orchestrator_run_deadline_s=1,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    started = {
        str(event.get("task_id"))
        for event in events
        if event.get("type") == "worker_started"
    }
    finished = {
        str(event.get("result", {}).get("task_id"))
        for event in events
        if event.get("type") == "worker_finished"
    }
    # 关键不变量：finished 必须是 started 的子集，不能出现“只结束没开始”。
    assert finished <= started
    assert started == {"a"}
    assert out["task_status"]["b"] in {"blocked", "failed"}


def test_blocked_dependent_emits_task_blocked(monkeypatch) -> None:
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(
        scheduler_runtime, "get_stream_writer", lambda: events.append
    )

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        if task["id"] == "a":
            return _result(task, status="failed")
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_task_retries=0),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b", deps=["a"])]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert out["task_status"]["b"] == "blocked"
    blocked = [
        event for event in events if event.get("type") == "task_blocked"
    ]
    assert blocked
    assert blocked[0]["task_id"] == "b"
    assert "依赖" in blocked[0]["reason"]


def test_deterministic_failure_is_not_retried() -> None:
    seen: list[str] = []

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        seen.append(str(task["id"]))
        result = _result(task, status="failed")
        result.summary = "implement 任务没有 write_scope，拒绝执行"
        return result

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_task_retries=1),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert seen == ["a"]
    assert out["task_attempts"]["a"] == 1
    assert out["task_status"]["a"] == "failed"


def test_semantic_failure_is_not_retried() -> None:
    """上下文超长属于语义失败，重跑同一输入只会再烧一次预算。"""
    seen: list[str] = []

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        seen.append(str(task["id"]))
        result = _result(task, status="failed")
        result.summary = "maximum context length exceeded"
        return result

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_task_retries=1),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert seen == ["a"]
    assert out["task_attempts"]["a"] == 1
    assert out["task_status"]["a"] == "failed"


def test_transient_failure_is_still_retried() -> None:
    seen: list[str] = []

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        seen.append(str(task["id"]))
        if len(seen) == 1:
            result = _result(task, status="failed")
            result.summary = "503 service unavailable"
            return result
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_task_retries=1,
            orchestrator_rate_limit_backoff_s=0.01,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert seen == ["a", "a"]
    assert out["task_status"]["a"] == "success"


def test_role_token_budget_blocks_remaining_tasks() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        result = _result(task)
        result.input_tokens = 100
        return result

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_vision_token_budget=100,
            orchestrator_max_concurrent_workers=1,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state(
                [
                    _task("a", role="vision"),
                    _task("b", role="vision"),
                ]
            ),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert out["task_status"]["a"] == "success"
    assert out["task_status"]["b"] == "blocked"
    assert "vision" in out["tasks"][1]["blocked_reason"]


def test_role_budgets_have_bounded_defaults() -> None:
    settings = _settings()

    assert settings.orchestrator_document_token_budget == 120_000
    assert settings.orchestrator_vision_token_budget == 60_000
    assert settings.orchestrator_author_token_budget == 60_000


def _clean_summary(head: str = "abc123") -> dict[str, Any]:
    return {
        "git": True,
        "git_root": True,
        "git_clean": True,
        "git_head": head,
        "file_count": 50,
        "test_file_count": 4,
    }


def _seed_journal(
    tmp_path: Any,
    task: dict[str, Any],
    *,
    status: str = "success",
    head: str = "abc123",
) -> WorkerJournal:
    journal = WorkerJournal(tmp_path / "worker_journal.jsonl")
    journal.record(
        thread_id="t",
        request_hash=request_fingerprint("做事"),
        task_hash=task_fingerprint(task),
        workspace_hash=workspace_fingerprint(_clean_summary(head)),
        role=str(task["role"]),
        status=status,
        attempt=1,
        result={
            **_result(task).model_dump(),
            "summary": "缓存结论",
            "findings": ["缓存发现"],
            "input_tokens": 999,
            "output_tokens": 111,
        },
    )
    return journal


def test_completed_readonly_result_is_reused_on_resume(tmp_path) -> None:
    task = _task("a")
    _seed_journal(tmp_path, task)
    calls: list[str] = []

    async def runner(task_data: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        calls.append(str(task_data["id"]))
        return _result(task_data)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(audit_dir=str(tmp_path)),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            {
                **_state([task]),
                "repo_summary": _clean_summary(),
            },
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert calls == []
    assert out["task_status"]["a"] == "success"
    record = out["worker_results"][0]
    assert record["reused"] is True
    assert record["summary"] == "缓存结论"
    assert record["input_tokens"] == 0
    assert record["output_tokens"] == 0


def test_reused_result_unblocks_dependents(tmp_path) -> None:
    first = _task("a")
    second = _task("b", deps=["a"])
    _seed_journal(tmp_path, first)
    calls: list[str] = []

    async def runner(task_data: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        calls.append(str(task_data["id"]))
        return _result(task_data)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(audit_dir=str(tmp_path)),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            {
                **_state([first, second]),
                "repo_summary": _clean_summary(),
            },
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert calls == ["b"]
    assert out["task_status"] == {"a": "success", "b": "success"}


def test_result_is_not_reused_when_workspace_changed(tmp_path) -> None:
    task = _task("a")
    _seed_journal(tmp_path, task, head="old-head")
    calls: list[str] = []

    async def runner(task_data: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        calls.append(str(task_data["id"]))
        return _result(task_data)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(audit_dir=str(tmp_path)),
        steering=SteeringController(),
    )
    asyncio.run(
        node(
            {
                **_state([task]),
                "repo_summary": _clean_summary(head="new-head"),
            },
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert calls == ["a"]


def test_partial_result_is_not_reused_by_default(tmp_path) -> None:
    task = _task("a")
    _seed_journal(tmp_path, task, status="partial")
    calls: list[str] = []

    async def runner(task_data: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        calls.append(str(task_data["id"]))
        return _result(task_data)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(audit_dir=str(tmp_path)),
        steering=SteeringController(),
    )
    asyncio.run(
        node(
            {
                **_state([task]),
                "repo_summary": _clean_summary(),
            },
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert calls == ["a"]


def test_implement_results_are_never_journaled(tmp_path) -> None:
    task = _task("i1", role="implement")

    async def runner(task_data: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task_data)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(audit_dir=str(tmp_path)),
        steering=SteeringController(),
    )
    asyncio.run(
        node(
            {
                **_state([task]),
                "repo_summary": _clean_summary(),
            },
            {"configurable": {"thread_id": "t"}},
        )
    )

    journal = WorkerJournal(tmp_path / "worker_journal.jsonl")
    assert (
        journal.lookup(
            thread_id="t",
            request_hash=request_fingerprint("做事"),
            task_hash=task_fingerprint(task),
            workspace_hash=workspace_fingerprint(_clean_summary()),
        )
        is None
    )


def _wave_contract() -> dict[str, Any]:
    return {
        "waves": [
            {
                "stage": 0,
                "files": ["src/a.py"],
                "modules": ["pkg.a"],
                "gate_tests": ["tests/test_a.py"],
            },
            {
                "stage": 1,
                "files": ["src/b.py"],
                "modules": ["pkg.b"],
                "gate_tests": ["tests/test_b.py"],
            },
        ],
        "affected_tests": ["tests/test_a.py"],
    }


def _wave_tasks() -> list[dict[str, Any]]:
    first = _task("a", role="implement", write_scope=["src/a.py"])
    first["stage"] = 0
    second = _task("b", role="implement", write_scope=["src/b.py"])
    second["stage"] = 1
    return [first, second]


def _wave_node(monkeypatch, gate_status: str):
    async def fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        return {record["task_id"]: (True, "", ["snap"]) for record in records}

    monkeypatch.setattr(
        "ubismart.orchestrator.merge_queue.merge_implement_patches_atomic",
        fake_merge,
    )

    class _GateResult:
        status = gate_status

    monkeypatch.setattr(
        scheduler_runtime,
        "run_verification",
        lambda *args, **kwargs: _GateResult(),
    )

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task)

    return make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            deep_workflows_enabled=True,
            interface_first_writes=True,
            orchestrator_max_concurrent_workers=1,
            orchestrator_incremental_merge=False,
            orchestrator_task_retries=0,
        ),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=SteeringController(),
    )


def test_interface_first_waves_run_gate_between_stages(monkeypatch) -> None:
    node = _wave_node(monkeypatch, "ok")
    out = asyncio.run(
        node(
            {
                **_state(_wave_tasks()),
                "change_contract": _wave_contract(),
            },
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert out["task_status"] == {"a": "success", "b": "success"}
    assert [item["stage"] for item in out["wave_gates"]] == [0, 1]
    # 第一波跑 gate；最后一波由主图 verify 兜底，标记 skipped。
    assert [item["status"] for item in out["wave_gates"]] == [
        "passed",
        "skipped",
    ]


def test_failed_wave_gate_blocks_later_stages(monkeypatch) -> None:
    node = _wave_node(monkeypatch, "failed")
    out = asyncio.run(
        node(
            {
                **_state(_wave_tasks()),
                "change_contract": _wave_contract(),
            },
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert out["task_status"]["a"] == "success"
    assert out["task_status"]["b"] == "blocked"
    assert out["wave_gates"][0]["status"] == "failed"
    assert "停止后续波次" in out["tasks"][1]["blocked_reason"]


def test_gate_timeout_blocks_later_stages(monkeypatch) -> None:
    async def fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        return {record["task_id"]: (True, "", ["snap"]) for record in records}

    monkeypatch.setattr(
        "ubismart.orchestrator.merge_queue.merge_implement_patches_atomic",
        fake_merge,
    )

    def slow_verify(*_args, **_kwargs):  # noqa: ANN002, ANN003
        time.sleep(0.5)
        class _Result:
            status = "ok"
        return _Result()

    monkeypatch.setattr(
        scheduler_runtime, "run_verification", slow_verify
    )

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            deep_workflows_enabled=True,
            interface_first_writes=True,
            orchestrator_max_concurrent_workers=1,
            orchestrator_incremental_merge=False,
            orchestrator_task_retries=0,
            orchestrator_gate_timeout_s=0.1,
        ),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            {
                **_state(_wave_tasks()),
                "change_contract": _wave_contract(),
            },
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert out["wave_gates"][0]["status"] == "failed"
    assert "超时" in out["wave_gates"][0]["reason"]
    assert out["task_status"]["b"] == "blocked"


def test_gate_wait_is_interrupted_by_user_cancel(monkeypatch) -> None:
    async def fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        return {record["task_id"]: (True, "", ["snap"]) for record in records}

    monkeypatch.setattr(
        "ubismart.orchestrator.merge_queue.merge_implement_patches_atomic",
        fake_merge,
    )
    started = threading.Event()

    def slow_verify(*_args, **_kwargs):  # noqa: ANN002, ANN003
        started.set()
        time.sleep(0.6)
        class _Result:
            status = "ok"
        return _Result()

    monkeypatch.setattr(
        scheduler_runtime, "run_verification", slow_verify
    )
    steering = SteeringController()

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            deep_workflows_enabled=True,
            interface_first_writes=True,
            orchestrator_max_concurrent_workers=1,
            orchestrator_incremental_merge=False,
            orchestrator_task_retries=0,
            orchestrator_gate_timeout_s=5,
        ),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=steering,
    )

    async def scenario() -> dict[str, Any]:
        task = asyncio.create_task(
            node(
                {
                    **_state(_wave_tasks()),
                    "change_contract": _wave_contract(),
                },
                {"configurable": {"thread_id": "t"}},
            )
        )
        await asyncio.to_thread(started.wait, 2)
        steering.submit("t", SteeringCommand(action="cancel"))
        return await task

    out = asyncio.run(scenario())
    assert out["wave_gates"][0]["status"] == "cancelled"
    assert out["task_status"]["b"] == "blocked"


def test_patch_review_timeout_does_not_block_merge(monkeypatch) -> None:
    async def fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        return {record["task_id"]: (True, "", ["snap"]) for record in records}

    monkeypatch.setattr(
        "ubismart.orchestrator.merge_queue.merge_implement_patches_atomic",
        fake_merge,
    )

    async def fake_read(self, record):  # noqa: ANN001
        return "diff --git a/a.py b/a.py\n"

    monkeypatch.setattr(
        scheduler_runtime.SchedulerRun, "_read_patch_text", fake_read
    )

    async def slow_review(*_args, **_kwargs):  # noqa: ANN002, ANN003
        await asyncio.sleep(0.5)
        return {"issues": []}

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task, patch_path=f"/ws/{task['id']}.patch")

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_patch_review=True,
            orchestrator_patch_review_timeout_s=0.1,
            orchestrator_incremental_merge=False,
        ),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=SteeringController(),
        patch_reviewer=slow_review,
    )
    out = asyncio.run(
        node(
            _state([_task("i1", role="implement", write_scope=["a.py"])]),
            {"configurable": {"thread_id": "t"}},
        )
    )
    assert out["task_status"]["i1"] == "success"


def test_gate_without_tests_is_skipped(monkeypatch) -> None:
    async def fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        return {record["task_id"]: (True, "", ["snap"]) for record in records}

    monkeypatch.setattr(
        "ubismart.orchestrator.merge_queue.merge_implement_patches_atomic",
        fake_merge,
    )

    def boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("没有 gate_tests 时不应跑验证")

    monkeypatch.setattr(scheduler_runtime, "run_verification", boom)

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            deep_workflows_enabled=True,
            interface_first_writes=True,
            orchestrator_max_concurrent_workers=1,
            orchestrator_incremental_merge=False,
            orchestrator_task_retries=0,
        ),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=SteeringController(),
    )
    contract = {
        "waves": [
            {"stage": 0, "files": ["src/a.py"], "gate_tests": []},
            {"stage": 1, "files": ["src/b.py"], "gate_tests": []},
        ],
        "affected_tests": [],
    }
    out = asyncio.run(
        node(
            {
                **_state(_wave_tasks()),
                "change_contract": contract,
            },
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert out["task_status"] == {"a": "success", "b": "success"}
    assert [item["status"] for item in out["wave_gates"]] == [
        "skipped",
        "skipped",
    ]


def _incremental_hooks(monkeypatch):
    batches: list[list[str]] = []
    # 用递增序号而不是时间戳：CI 满载时 time.monotonic() 的粒度会让
    # 先后发生的两个事件取到同一个值，严格比较会随机失败。
    timeline: dict[str, int] = {}
    order = itertools.count()
    first_merge = asyncio.Event()

    async def fake_merge(sandbox, root, records, *, allow_overlap=False):  # noqa: ANN001
        batches.append([str(record["task_id"]) for record in records])
        timeline.setdefault("first_merge_at", next(order))
        first_merge.set()
        return {
            record["task_id"]: (True, "", ["snap"]) for record in records
        }

    def fake_preflight(sandbox, patch_path, root):  # noqa: ANN001
        name = patch_path.rsplit("/", 1)[-1].removesuffix(".patch")
        if name == "unknown":
            return [], "无法解析补丁路径"
        return [f"{name}.py"], ""

    monkeypatch.setattr(
        "ubismart.orchestrator.merge_queue.merge_implement_patches_atomic",
        fake_merge,
    )
    monkeypatch.setattr(
        scheduler_runtime, "preflight_patch_paths", fake_preflight
    )
    return batches, timeline, order, first_merge


def test_disjoint_completed_implement_merges_while_another_is_in_flight(
    monkeypatch,
) -> None:
    batches, timeline, order, first_merge = _incremental_hooks(monkeypatch)

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        task_id = str(task["id"])
        if task_id == "i2":
            # i2 保持 in-flight，直到 i1 的增量合并发生。旧实现（等整批）
            # 会超时，随后 batches 断言会以“同一批”失败。
            try:
                await asyncio.wait_for(first_merge.wait(), timeout=15.0)
            except TimeoutError:
                pass
            timeline["i2_done_at"] = next(order)
        else:
            await asyncio.sleep(0.01)
        return _result(task, patch_path=f"/ws/{task_id}.patch")

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_max_concurrent_workers=2,
            orchestrator_incremental_merge=True,
        ),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state(
                [
                    _task("i1", role="implement", write_scope=["a.py"]),
                    _task("i2", role="implement", write_scope=["b.py"]),
                ]
            ),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert batches[0] == ["i1"]
    # 序号由 fake_merge 与 i2 runner 的执行顺序产生：i2 只有等到
    # first_merge 被 set 才会继续，因此这里的先后关系不依赖时钟精度。
    assert timeline["first_merge_at"] < timeline["i2_done_at"]
    assert out["task_status"] == {"i1": "success", "i2": "success"}
    assert out["merged_paths"] == ["i1.py", "i2.py"]
    assert {
        "tasks": ["i1"],
        "merged": ["i1"],
        "incremental": True,
    } in out["merge_batches"]
    assert all(
        "merge_wait_ms" in record
        for record in out["worker_results"]
        if record.get("role") == "implement" and record.get("merged")
    )


def test_shared_file_implement_still_waits_for_batch(monkeypatch) -> None:
    batches, timeline, order, _ = _incremental_hooks(monkeypatch)
    # 两条补丁都命中同一文件：增量必须整组等待。
    monkeypatch.setattr(
        scheduler_runtime,
        "preflight_patch_paths",
        lambda sandbox, patch_path, root: (["app/x.py"], ""),
    )

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        task_id = str(task["id"])
        if task_id == "i2":
            await asyncio.sleep(0.15)
            timeline["i2_done_at"] = next(order)
        else:
            await asyncio.sleep(0.01)
        return _result(task, patch_path=f"/ws/{task_id}.patch")

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_max_concurrent_workers=2,
            orchestrator_incremental_merge=True,
        ),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state(
                [
                    _task("i1", role="implement", write_scope=["app/x.py"]),
                    _task("i2", role="implement", write_scope=["app/x.py"]),
                ]
            ),
            {"configurable": {"thread_id": "t"}},
        )
    )

    # 共享文件必须整组等待，不能在 i2 还在跑时先合并 i1。
    assert batches == [["i1", "i2"]]
    assert timeline["first_merge_at"] >= timeline["i2_done_at"]
    assert out["task_status"] == {"i1": "success", "i2": "success"}


def test_unknown_patch_paths_fall_back_to_batch_flush(monkeypatch) -> None:
    batches, timeline, order, _ = _incremental_hooks(monkeypatch)

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        task_id = str(task["id"])
        if task_id == "i2":
            await asyncio.sleep(0.15)
            timeline["i2_done_at"] = next(order)
        else:
            await asyncio.sleep(0.01)
        return _result(task, patch_path=f"/ws/{task_id}.patch")

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_max_concurrent_workers=2,
            orchestrator_incremental_merge=True,
        ),
        sandbox=object(),  # type: ignore[arg-type]
        root="/ws",
        steering=SteeringController(),
    )
    # 让 i1 的补丁路径无法解析：增量必须整体回退。
    monkeypatch.setattr(
        scheduler_runtime,
        "preflight_patch_paths",
        lambda sandbox, patch_path, root: (
            ([], "无法解析补丁路径")
            if "i1.patch" in patch_path
            else (["i2.py"], "")
        ),
    )
    out = asyncio.run(
        node(
            _state(
                [
                    _task("i1", role="implement", write_scope=["a.py"]),
                    _task("i2", role="implement", write_scope=["b.py"]),
                ]
            ),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert batches == [["i1", "i2"]]
    assert timeline["first_merge_at"] >= timeline["i2_done_at"]
    assert out["task_status"] == {"i1": "success", "i2": "success"}


def test_sequential_stage_receives_baseline_paths() -> None:
    seen: dict[str, Any] = {}

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        seen.update(task)
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_sequential_writes=True),
        steering=SteeringController(),
    )
    state = _state(
        [
            _task("i0", role="implement", write_scope=["app/a.py"]),
            _task(
                "i1",
                role="implement",
                write_scope=["app/a.py"],
                deps=["i0"],
            ),
        ]
    )
    state["tasks"][0]["stage"] = 0
    state["tasks"][1]["stage"] = 1
    state["task_status"] = {"i0": "success", "i1": "pending"}
    state["merged_paths"] = ["app/a.py"]

    out = asyncio.run(node(state, {"configurable": {"thread_id": "t"}}))

    assert seen["seed_from_workspace"] is True
    assert seen["baseline_paths"] == ["app/a.py"]
    assert out["merged_paths"] == ["app/a.py"]


def test_sequential_dependent_starts_after_a_partial_merge() -> None:
    ran: list[str] = []

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        ran.append(str(task["id"]))
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_sequential_writes=True),
        steering=SteeringController(),
    )
    state = _state(
        [
            _task("i0", role="implement", write_scope=["app/a.py"]),
            {
                **_task(
                    "i1",
                    role="implement",
                    write_scope=["app/a.py"],
                    deps=["i0"],
                ),
                "stage": 1,
                "depends_mode": "any",
            },
        ]
    )
    state["tasks"][0]["stage"] = 0
    state["task_status"] = {"i0": "partial", "i1": "pending"}

    out = asyncio.run(node(state, {"configurable": {"thread_id": "t"}}))

    assert ran == ["i1"]
    assert out["task_status"]["i1"] == "success"


def test_fix_mode_worker_receives_baseline_paths() -> None:
    seen: dict[str, Any] = {}

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        seen.update(task)
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_fix_rounds=1),
        steering=SteeringController(),
    )
    state = _state(
        [_task("fix-1", role="implement", write_scope=["app/a.py"])]
    )
    state["orchestrator_fix_mode"] = True
    state["merged_paths"] = ["app/a.py"]

    out = asyncio.run(node(state, {"configurable": {"thread_id": "t"}}))

    # 修复轮的任务可能是 stage 0，但仍必须基于已合并改动。
    assert seen["seed_from_workspace"] is True
    assert seen["baseline_paths"] == ["app/a.py"]
    assert out["merged_paths"] == ["app/a.py"]


def test_orphan_worktrees_are_pruned_once_per_run(monkeypatch) -> None:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        "ubismart.orchestrator.implement.prune_orphan_worktrees",
        lambda *args: calls.append(args),
    )

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_write_enabled=True),
        sandbox=object(),
        root="/ws",
        steering=SteeringController(),
    )
    config = {"configurable": {"thread_id": "t"}}

    first = asyncio.run(node(_state([_task("a")]), config))
    assert len(calls) == 1
    assert first["worktrees_pruned"] is True

    # 模拟 checkpoint 边界重入：状态里已带 pruned 标志，不应再扫一次。
    second_state = _state([_task("b")])
    second_state["worktrees_pruned"] = True
    second = asyncio.run(node(second_state, config))

    assert len(calls) == 1
    assert second["worktrees_pruned"] is True


def test_step_budget_stops_new_dispatch_and_reports_partial_result() -> None:
    ran: list[str] = []

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        ran.append(str(task["id"]))
        return _result(task)

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            orchestrator_max_concurrent_workers=1,
            orchestrator_total_steps=1,
            orchestrator_total_tokens=0,
        ),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(
            _state([_task("a"), _task("b")]),
            {"configurable": {"thread_id": "t"}},
        )
    )

    assert ran == ["a"]
    assert out["task_status"]["a"] == "success"
    assert out["task_status"]["b"] == "blocked"
    assert out["budget_abort"]["reason"].startswith("预算耗尽")
    assert out["budget_abort"]["completed_tasks"] == ["a"]
    assert out["budget_snapshot"]["steps"] == 1


def test_model_max_tokens_is_clamped_to_remaining_parent_budget() -> None:
    seen: list[int | None] = []

    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        seen.append(task.get("max_tokens"))
        return _result(task)

    task = _task("a")
    task["max_tokens"] = 100_000
    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_total_tokens=3_000),
        steering=SteeringController(),
    )
    asyncio.run(
        node(_state([task]), {"configurable": {"thread_id": "t"}})
    )

    # 父预算只有 3000；模型写的 100000 必须被夹到父剩余额度，而不是原样下传。
    assert seen == [3_000]


def test_phase_gates_write_control_checkpoints(tmp_path) -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        result = _result(task)
        result.input_tokens = 100
        return result

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            audit_dir=str(tmp_path),
            orchestrator_total_tokens=100_000,
        ),
        steering=SteeringController(),
    )
    state = _state(
        [
            _task("a"),
            {**_task("b", deps=["a"]), "stage": 1},
        ]
    )
    state["estimation_card"] = {"gate": {"phase_gates": True}}

    out = asyncio.run(node(state, {"configurable": {"thread_id": "t"}}))

    assert [item["stage"] for item in out["control_checkpoints"]] == [0, 1]
    assert all(
        item["status"] == "passed" for item in out["control_checkpoints"]
    )
    checkpoint_dir = tmp_path / "control_checkpoints"
    assert len(list(checkpoint_dir.rglob("*.json"))) == 2


def test_phase_budget_failure_blocks_later_stages(tmp_path) -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        result = _result(task)
        result.input_tokens = 60_000
        return result

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(
            audit_dir=str(tmp_path),
            orchestrator_total_tokens=100_000,
        ),
        steering=SteeringController(),
    )
    state = _state(
        [
            _task("a"),
            {**_task("b", deps=["a"]), "stage": 1},
        ]
    )
    state["estimation_card"] = {"gate": {"phase_gates": True}}

    out = asyncio.run(node(state, {"configurable": {"thread_id": "t"}}))

    # 两阶段均分 100k → 每阶段 50k；stage 0 花了 60k，后一阶段必须停。
    assert out["task_status"]["b"] == "blocked"
    assert any(
        item["stage"] == 0 and item["status"] == "failed"
        for item in out["control_checkpoints"]
    )


def test_repeated_tool_action_trips_the_loop_breaker() -> None:
    async def runner(task: dict[str, Any], **_kwargs: Any) -> WorkerResult:
        result = _result(task)
        result.tool_calls = [
            {"name": "search_code", "args": {"query": "x"}},
            {"name": "search_code", "args": {"query": "x"}},
        ]
        return result

    node = make_scheduler_node(
        worker_runner=runner,
        settings=_settings(orchestrator_repeat_action_limit=2),
        steering=SteeringController(),
    )
    out = asyncio.run(
        node(_state([_task("a")]), {"configurable": {"thread_id": "t"}})
    )

    assert out["loop_abort"]["tool"] == "search_code"
    assert out["loop_abort"]["count"] == 2
