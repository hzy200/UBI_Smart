"""嵌套编排：契约切片、深度/扇出边界、子结果回填。"""

from __future__ import annotations

import asyncio
from typing import Any

from ubismart.config import Settings
from ubismart.orchestrator import nested as nested_module
from ubismart.orchestrator.contracts import SubResult
from ubismart.orchestrator.plan_review import validate_nested_plan
from ubismart.orchestrator.scheduler import make_scheduler_node
from ubismart.session.steering import SteeringController


def _settings(**overrides: Any) -> Settings:
    base = {
        "orchestrator_max_concurrent_workers": 1,
        "orchestrator_task_retries": 0,
        "orchestrator_checkpoint_every": 20,
        "orchestrator_run_deadline_s": 0,
        "orchestrator_total_tokens": 10_000,
        "orchestrator_nested_enabled": True,
        "orchestrator_max_depth": 1,
        "orchestrator_max_fanout": 1,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


def _sub_task() -> dict[str, Any]:
    return {
        "id": "sub1",
        "role": "sub_orchestrator",
        "goal": "独立调研",
        "inputs": [],
        "acceptance": ["给出结论"],
        "depends_on": [],
        "sub_contract": {
            "goal": "独立调研",
            "acceptance": [{"rule": "给出结论", "auto": True}],
        },
        "max_tokens": 2_000,
    }


def test_nested_plan_enforces_depth_and_fanout() -> None:
    task = _sub_task()
    assert validate_nested_plan(
        [task], enabled=True, depth=0, max_depth=1, max_fanout=1
    ) == []
    assert validate_nested_plan(
        [task], enabled=False, depth=0, max_depth=1, max_fanout=1
    )
    assert validate_nested_plan(
        [task], enabled=True, depth=1, max_depth=1, max_fanout=1
    )
    assert validate_nested_plan(
        [task, {**task, "id": "sub2"}],
        enabled=True,
        depth=0,
        max_depth=1,
        max_fanout=1,
    )


def test_sub_orchestrator_runner_returns_structured_result() -> None:
    class FakeGraph:
        async def ainvoke(self, state, config):
            assert state["suppress_events"] is True
            assert state["sub_depth"] == 1
            assert state["token_budget_limit"] == 2_000
            return {
                "worker_results": [
                    {
                        "task_id": "child",
                        "status": "success",
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "duration_ms": 300,
                        "tool_calls": [{"name": "search"}],
                    }
                ],
                "orchestrator_summary": "完成",
                "control_checkpoints": [
                    {"checkpoint_id": "stage-0", "status": "passed"}
                ],
                "orchestration_handoff": {"tasks": []},
            }

    runner = nested_module.SubOrchestratorRunner(
        llm=object(),  # type: ignore[arg-type]
        planning_llm=None,
        settings=_settings(),
        sandbox=None,
        root="/ws",
        worker_runner=lambda *a, **k: None,  # type: ignore[arg-type]
        graph_factory=lambda **kwargs: FakeGraph(),
    )
    sub = asyncio.run(
        runner.run(
            _sub_task(),
            parent_thread_id="t",
            cwd="/ws",
            repo_summary={"file_count": 3},
            budget_tokens=2_000,
            wall_ms=10_000,
            plan_version=1,
            depth=1,
        )
    )

    assert sub.status == "success"
    assert sub.actual_cost["input_tokens"] == 100
    assert sub.actual_cost["tokens"] == 120
    assert sub.local_ckpt_digest == "stage-0"


def test_scheduler_backfills_sub_result_cost_to_parent() -> None:
    calls: list[dict[str, Any]] = []

    class FakeSubRunner:
        async def run(self, task, **kwargs):
            calls.append(kwargs)
            return SubResult(
                sub_id="sub:sub1",
                parent_task_id="sub1",
                depth=1,
                status="success",
                artifact={"summary": "子任务完成", "signals": []},
                local_ckpt_digest="cp-1",
                actual_cost={
                    "steps": 1,
                    "tool_calls": 2,
                    "tokens": 120,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "wall_ms": 300,
                },
            )

    async def worker_runner(*args, **kwargs):  # pragma: no cover - 不应调用
        raise AssertionError("sub_orchestrator 不应走普通 worker runner")

    node = make_scheduler_node(
        worker_runner=worker_runner,  # type: ignore[arg-type]
        settings=_settings(),
        steering=SteeringController(),
        sub_runner=FakeSubRunner(),
    )
    out = asyncio.run(
        node(
            {
                "request": "调研",
                "cwd": "/ws",
                "tasks": [_sub_task()],
                "task_status": {"sub1": "pending"},
                "task_attempts": {},
                "worker_results": [],
                "orchestrator_round": 0,
                "task_extensions_seen": 0,
            },
            {"configurable": {"thread_id": "t"}},
        )
    )

    result = out["worker_results"][0]
    assert result["status"] == "success"
    assert result["sub_result"]["sub_id"] == "sub:sub1"
    assert result["input_tokens"] == 100
    assert result["output_tokens"] == 20
    assert calls and calls[0]["depth"] == 1
    assert calls[0]["budget_tokens"] > 0
