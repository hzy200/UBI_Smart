"""组合 E2E：增量合并 + 顺序写 + verify 失败后的修复轮（真实 WSL）。

用假 LLM 驱动真实文件工具，覆盖：

- 两个无交集的 wave0 implement：慢 worker 在飞时，快 worker 的补丁增量合并；
- wave1 implement 依赖 wave0：worktree 注入前一波基线，补丁只含新增改动；
- 主图 verify 失败后进入有界修复轮；
- 修复 worker 基于已合并状态再改一次，verify 通过。
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ubismart.config import Settings
from ubismart.graph.nodes.orchestrator_fix import prepare_orchestrator_fix
from ubismart.graph.nodes.verify import make_verify_node
from ubismart.graph.routing import (
    ORCHESTRATOR_FIX,
    REVIEW,
    make_route_after_verify,
    route_after_synthesize,
)
from ubismart.orchestrator.contracts import (
    TaskGraph,
    TaskSpec,
    WorkerResult,
    WorkflowDecision,
)
from ubismart.orchestrator.implement import make_implement_runner
from ubismart.orchestrator.subgraph import build_orchestrator_subgraph
from ubismart.orchestrator.workflow import WorkflowPlan
from ubismart.session.steering import SteeringController


class _Structured:
    def __init__(self, resolver):  # noqa: ANN001
        self.resolver = resolver

    def with_retry(self, **_kwargs: Any) -> _Structured:
        return self

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        return self.resolver(messages)


class _E2ELLM:
    """一个对象同时承担工作流选择、任务规划和 implement worker 循环。"""

    def __init__(self, initial_tasks: list[TaskSpec]) -> None:
        self.initial_tasks = initial_tasks
        self.seen: list[str] = []

    def bind_tools(self, tools: list[Any]) -> _E2ELLM:
        self.tools = tools
        return self

    def with_structured_output(self, schema: Any, **_kwargs: Any) -> _Structured:
        if schema is WorkflowPlan:
            return _Structured(
                lambda messages: WorkflowPlan(
                    workflow="orchestrated_implement",
                    reason="e2e",
                    tasks=self.initial_tasks,
                )
            )
        if schema is WorkflowDecision:
            return _Structured(
                lambda messages: WorkflowDecision(
                    workflow="orchestrated_implement", reason="e2e"
                )
            )
        assert schema is TaskGraph
        return _Structured(self._task_graph)

    def _task_graph(self, messages: Any) -> TaskGraph:
        text = "\n".join(str(getattr(m, "content", "")) for m in messages)
        if "自动验证未通过" in text:
            return TaskGraph(
                tasks=[
                    TaskSpec(
                        id="fix-b",
                        role="implement",
                        goal="FIX_B",
                        write_scope=["app/b.py"],
                    )
                ]
            )
        return TaskGraph(tasks=self.initial_tasks)

    async def ainvoke(self, messages: Any, config: Any = None) -> Any:
        text = "\n".join(str(getattr(m, "content", "")) for m in messages)
        self.seen.append(text)
        # 同一 worker 已经发出过一次工具调用：收尾，避免无限循环。
        if any(getattr(m, "tool_calls", None) for m in messages):
            return AIMessage(content="完成")

        edits = {
            # 慢到足以让 wave0-b 在它还在飞时完成增量合并。
            "WAVE0_A": ("app/a.py", "VALUE = 1", "VALUE = 2", 2.0),
            "WAVE0_B": ("app/b.py", "B = 1", "B = 2", 0.01),
            "WAVE1_A": ("app/a.py", "VALUE = 2", "VALUE = 3", 0.01),
            "FIX_B": ("app/b.py", "B = 2", "B = 3", 0.01),
        }
        for marker, (path, old, new, delay) in edits.items():
            if marker in text:
                await asyncio.sleep(delay)
                return AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "file_edit",
                            "args": {
                                "path": path,
                                "old_string": old,
                                "new_string": new,
                                "reason": "e2e",
                            },
                            "id": f"call-{marker}",
                        }
                    ],
                )
        return AIMessage(content="无需修改")


def _settings(sandbox, root: str) -> Settings:  # noqa: ANN001
    return sandbox.settings.model_copy(
        update={
            "wsl_workspace": root,
            "orchestrator_enabled": True,
            "orchestrator_min_files": 1,
            "orchestrator_write_enabled": True,
            "orchestrator_sequential_writes": True,
            "orchestrator_incremental_merge": True,
            "orchestrator_fix_rounds": 1,
            "orchestrator_max_write_stages": 2,
            "orchestrator_max_tasks": 6,
            "orchestrator_max_concurrent_workers": 2,
            "orchestrator_task_retries": 0,
            "orchestrator_worker_rounds": 4,
            "orchestrator_worker_repairs": 0,
            "orchestrator_worker_timeout": 60,
            "orchestrator_run_deadline_s": 120,
            "orchestrator_repo_map_lazy": True,
            "orchestrator_plan_repair": True,
            "verify_command": "python3 tests/check_values.py",
            "verify_timeout": 30,
        }
    )


async def _run_subgraph(
    subgraph: Any, state: dict[str, Any]
) -> tuple[dict[str, Any], list[Any]]:
    states: dict[str, Any] = {}
    events: list[Any] = []
    config = {
        "configurable": {"thread_id": state.get("thread_id", "e2e")},
        "recursion_limit": 400,
    }
    async for chunk in subgraph.astream(
        state,
        config,
        stream_mode=["updates", "custom"],
        subgraphs=True,
    ):
        _namespace, mode, data = chunk
        if mode == "custom":
            events.append(data)
        elif mode == "updates":
            for value in data.values():
                if isinstance(value, dict):
                    states.update(value)
    return states, events


@pytest.mark.wsl
def test_incremental_sequential_and_fix_round_end_to_end(require_wsl) -> None:  # noqa: ANN001
    sandbox = require_wsl
    root = sandbox.run(
        "mktemp -d /tmp/agent-orch-e2e-XXXXXX"
    ).stdout.strip()
    assert root
    try:
        setup = sandbox.run(
            f"""
            cd {shlex.quote(root)}
            git init -q
            git config user.email eval@example.invalid
            git config user.name eval
            mkdir -p app tests
            touch app/__init__.py
            printf 'VALUE = 1\\n' > app/a.py
            printf 'B = 1\\n' > app/b.py
            cat > tests/check_values.py <<'PY'
import sys
sys.path.insert(0, ".")
from app.a import VALUE as A
from app.b import B
raise SystemExit(0 if A == 3 and B == 3 else 1)
PY
            printf '.agent/\\n__pycache__/\\n' > .gitignore
            git add -A
            git commit -qm init
            """,
            cwd=root,
        )
        assert setup.ok, setup.render(800)

        settings = _settings(sandbox, root)
        sandbox.settings = settings

        initial_tasks = [
            TaskSpec(
                id="wave0-a",
                role="implement",
                goal="WAVE0_A",
                write_scope=["app/a.py"],
                stage=0,
            ),
            TaskSpec(
                id="wave0-b",
                role="implement",
                goal="WAVE0_B",
                write_scope=["app/b.py"],
                stage=0,
            ),
            TaskSpec(
                id="wave1-a",
                role="implement",
                goal="WAVE1_A",
                write_scope=["app/a.py"],
                stage=1,
                depends_on=["wave0-a"],
            ),
        ]
        llm = _E2ELLM(initial_tasks)
        implement_runner = make_implement_runner(
            llm=llm,  # type: ignore[arg-type]
            sandbox=sandbox,
            root=root,
            settings=settings,
        )

        async def worker_runner(
            task: dict[str, Any], **kwargs: Any
        ) -> WorkerResult:
            if task.get("role") != "implement":
                return WorkerResult(
                    task_id=str(task.get("id", "")),
                    role=str(task.get("role", "")),
                    status="failed",
                    summary="e2e 只测 implement",
                )
            kwargs.pop("dependency_context", None)
            return await implement_runner(task, **kwargs)

        subgraph = build_orchestrator_subgraph(
            llm=llm,  # type: ignore[arg-type]
            settings=settings,
            sandbox=sandbox,
            root=root,
            worker_runner=worker_runner,
            steering=SteeringController(),
            allow_write=True,
        )
        initial_state = {
            "request": "同时修改两个模块并完成第一阶段",
            "messages": [HumanMessage(content="同时修改两个模块并完成第一阶段")],
            "cwd": root,
            "thread_id": "e2e-orch",
            "repo_profile": {"file_count": 10, "test_file_count": 1},
            "execution_mode": "orchestrated",
            "merged_paths": [],
            "merge_batches": [],
            "worktrees_pruned": False,
        }
        states, _events = asyncio.run(_run_subgraph(subgraph, initial_state))

        # worker 内部验证在合并不完整时是 partial；关键是补丁已合并、
        # wave1 未被 partial 依赖阻塞。
        assert states["task_status"] == {
            "wave0-a": "partial",
            "wave0-b": "partial",
            "wave1-a": "partial",
        }
        assert states["pending_verification"] is True
        assert set(states["merged_paths"]) == {"app/a.py", "app/b.py"}
        # 快 worker 在慢 worker 仍运行时被增量合并。
        assert any(
            batch.get("incremental") and batch.get("tasks") == ["wave0-b"]
            for batch in states["merge_batches"]
        )
        wave1 = next(
            result
            for result in states["worker_results"]
            if result["task_id"] == "wave1-a"
        )
        assert wave1["seed_files"] == ["app/a.py", "app/b.py"]
        assert wave1["seed_bytes"] > 0

        assert "VALUE = 3" in sandbox.run("cat app/a.py", cwd=root).stdout
        assert "B = 2" in sandbox.run("cat app/b.py", cwd=root).stdout

        verify_node = make_verify_node(sandbox, root)
        verify_state = {
            **initial_state,
            **states,
            "dirty": True,
            "plan": ["完成"],
            "plan_kinds": ["implement"],
            "step_idx": 0,
            "retry": 0,
            "verification": {},
            "last_verification": {},
            "last_failure_signature": "",
            "same_failure_count": 0,
        }
        first_verify = verify_node(verify_state, None)
        assert first_verify["verification"]["status"] == "failed"
        assert first_verify["orchestrator_fix_eligible"] is True
        assert first_verify["pending_verification"] is False

        route = make_route_after_verify(
            settings.max_repair_rounds,
            max_same_failures=settings.max_same_failures,
            max_orchestrator_fix_rounds=settings.orchestrator_fix_rounds,
        )
        after_verify = {**verify_state, **first_verify}
        assert route(after_verify) == ORCHESTRATOR_FIX

        fix_prep = prepare_orchestrator_fix(after_verify)
        fix_state = {**after_verify, **fix_prep}
        fix_states, _fix_events = asyncio.run(
            _run_subgraph(subgraph, fix_state)
        )

        fix_record = next(
            result
            for result in fix_states["worker_results"]
            if result["task_id"] == "fx1-fix-b"
        )
        assert fix_states["task_status"]["fx1-fix-b"] == "success", {
            key: fix_record.get(key)
            for key in (
                "status",
                "summary",
                "error",
                "seed_files",
                "seed_bytes",
                "patch_path",
            )
        }
        assert fix_states["pending_verification"] is True
        assert route_after_synthesize(
            {**fix_state, **fix_states}
        ) == "verify"
        fix_result = next(
            result
            for result in fix_states["worker_results"]
            if result["task_id"] == "fx1-fix-b"
        )
        assert fix_result["seed_files"] == ["app/a.py", "app/b.py"]
        assert fix_result["seed_bytes"] > 0
        assert "B = 3" in sandbox.run("cat app/b.py", cwd=root).stdout

        second_verify = verify_node(
            {
                **fix_state,
                **fix_states,
                "dirty": True,
                "pending_verification": True,
                "plan": ["完成"],
                "plan_kinds": ["implement"],
                "step_idx": 0,
                "retry": 0,
            },
            None,
        )
        assert second_verify["verification"]["status"] == "ok"
        assert second_verify["orchestrator_fix_eligible"] is False
        assert second_verify["orchestrator_fix_mode"] is False
        assert (
            make_route_after_verify(
                settings.max_repair_rounds,
                max_same_failures=settings.max_same_failures,
                max_orchestrator_fix_rounds=settings.orchestrator_fix_rounds,
            )({**fix_state, **fix_states, **second_verify})
            == REVIEW
        )
    finally:
        sandbox.run(f"rm -rf {shlex.quote(root)}")
