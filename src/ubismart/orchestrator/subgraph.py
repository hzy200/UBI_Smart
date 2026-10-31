"""独立 orchestrator 子图（事件驱动 DAG）。

```
survey → select_workflow → plan_tasks → steering_plan → write_gate
      → scheduler ⇄ steering_dispatch → fan_in → END
```

父图只看到 `orchestrator` 一个节点；内层节点通过 `subgraphs=True` 的
stream 上报事件。`write_gate` 默认 no-op，只在显式开启写计划审批时
于并发调度前一次性 interrupt。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from ubismart.config import Settings

# 与 graph/nodes/steering.py 的节点工厂同名，避免两套 steering 语义。
from ubismart.graph.nodes.steering import (
    make_steering_dispatch_node,
    make_steering_plan_node,
)
from ubismart.graph.routing import (
    PLAN_TASKS,
    STEERING_DISPATCH,
    STEERING_PLAN,
    SYNTHESIZE,
    route_after_steering_plan,
)
from ubismart.graph.routing import WRITE_GATE as WRITE_GATE_NODE
from ubismart.orchestrator.fanin import make_fan_in_node
from ubismart.orchestrator.plan import make_plan_tasks_node
from ubismart.orchestrator.routing import route_after_select
from ubismart.orchestrator.scheduler import SCHEDULER, make_scheduler_node
from ubismart.orchestrator.state import OrchestratorState
from ubismart.orchestrator.survey import (
    build_repo_map_summary,
    make_survey_node,
)
from ubismart.orchestrator.workers import WorkerRunner
from ubismart.orchestrator.workflow import make_select_workflow_node
from ubismart.orchestrator.write_gate import (
    WRITE_GATE,
    make_write_gate_node,
)
from ubismart.platform.concurrency import ConcurrencyGovernor
from ubismart.sandbox.wsl_exec import WslSandbox
from ubismart.session.steering import SteeringController


def _route_after_scheduler_gate(state: OrchestratorState) -> str:
    if state.get("steering_cancelled"):
        return SYNTHESIZE
    if state.get("steering_replan"):
        return PLAN_TASKS
    if any(
        value == "pending"
        for value in (state.get("task_status") or {}).values()
    ):
        return SCHEDULER
    return SYNTHESIZE


def build_orchestrator_subgraph(
    *,
    llm: BaseChatModel,
    settings: Settings,
    sandbox: WslSandbox,
    root: str,
    worker_runner: WorkerRunner,
    steering: SteeringController,
    allow_write: bool = False,
    governor: ConcurrencyGovernor | None = None,
    planning_llm: BaseChatModel | None = None,
    patch_reviewer: Any = None,
):
    """组装并编译独立子图。checkpointer 由父图持有。"""
    graph = StateGraph(OrchestratorState)
    planner_llm = planning_llm or llm
    repo_map_builder = None
    if bool(getattr(settings, "orchestrator_repo_map_lazy", False)):
        repo_map_builder = lambda: build_repo_map_summary(  # noqa: E731
            sandbox, root, settings
        )

    graph.add_node("survey", make_survey_node(sandbox, root, settings))
    graph.add_node(
        "select_workflow",
        make_select_workflow_node(
            planner_llm, settings, allow_write=allow_write
        ),
    )
    graph.add_node(
        PLAN_TASKS,
        make_plan_tasks_node(
            planner_llm,
            settings,
            allow_write=allow_write,
            repo_map_builder=repo_map_builder,
        ),
    )
    graph.add_node(
        STEERING_PLAN,
        make_steering_plan_node(steering, settings),
    )
    graph.add_node(WRITE_GATE, make_write_gate_node(settings))
    graph.add_node(
        STEERING_DISPATCH,
        make_steering_dispatch_node(steering, settings),
    )
    graph.add_node(
        SCHEDULER,
        make_scheduler_node(
            worker_runner=worker_runner,
            settings=settings,
            sandbox=sandbox,
            root=root,
            steering=steering,
            governor=governor,
            patch_reviewer=patch_reviewer,
        ),
    )
    graph.add_node(
        SYNTHESIZE,
        make_fan_in_node(
            settings,
            llm=llm,
            sandbox=sandbox,
            root=root,
            artifacts=settings.orchestrator_fanout_artifacts,
        ),
    )

    graph.add_edge(START, "survey")
    graph.add_edge("survey", "select_workflow")
    # 规则/模型选择 direct 时直接结束子图；父图会继续原来的 planner 路径。
    graph.add_conditional_edges(
        "select_workflow",
        route_after_select,
        {PLAN_TASKS: PLAN_TASKS, "planner": END},
    )
    graph.add_edge(PLAN_TASKS, STEERING_PLAN)
    # 取消必须在这里截断：cancel 已被 steering_plan 消费，若继续进 scheduler，
    # 同批追加的任务仍会被派发（scheduler 只轮询队列，看不到已消费的 cancel）。
    graph.add_conditional_edges(
        STEERING_PLAN,
        route_after_steering_plan,
        {WRITE_GATE_NODE: WRITE_GATE, SYNTHESIZE: SYNTHESIZE},
    )
    graph.add_edge(WRITE_GATE, SCHEDULER)
    graph.add_edge(SCHEDULER, STEERING_DISPATCH)
    graph.add_conditional_edges(
        STEERING_DISPATCH,
        _route_after_scheduler_gate,
        {
            SCHEDULER: SCHEDULER,
            PLAN_TASKS: PLAN_TASKS,
            SYNTHESIZE: SYNTHESIZE,
        },
    )
    graph.add_edge(SYNTHESIZE, END)

    return graph.compile()
