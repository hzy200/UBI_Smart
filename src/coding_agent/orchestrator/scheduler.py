"""事件驱动的动态任务图调度器。

与旧 `dispatch` 的区别：

- 旧实现一次取一批 ready 任务，`gather` 等整批结束再进入下一轮；
- 这里维护 ready 堆与 in-flight 集合，任一任务完成就立即解锁依赖方；
- 失败任务在额度内带上下文重试，额度用尽立即阻塞下游；
- worker 提议的只读任务在完成点即时校验并加入队列。

checkpoint/干预仍然通过**边界**实现：scheduler 运行到队列暂空或完成数达到
`orchestrator_checkpoint_every` 时，先 drain 在飞任务再返回；随后由
`steering_dispatch` 节点消费 pause/add/replan/cancel。这样不会在节点中途
`interrupt()` 而丢掉未提交的 worker 结果。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig

from coding_agent.concurrency import (
    ConcurrencyGovernor,
)
from coding_agent.config import Settings
from coding_agent.orchestrator.contracts import WorkerResult
from coding_agent.orchestrator.workers import WorkerRunner
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.steering import SteeringController

SCHEDULER = "scheduler"


def _deps_met(task: dict[str, Any], status: dict[str, str]) -> bool:
    for dep in task.get("depends_on") or []:
        value = status.get(str(dep))
        if value == "success":
            continue
        if value == "partial" and task.get("depends_mode") == "any":
            continue
        return False
    return True


def _wire_implement_dependents(tasks: list[dict[str, Any]]) -> None:
    """把 test/review 自动挂到 implement 之后。

    否则它们会和写 worker 同批启动，看到的是合并前状态，产出天然是
    partial/blocked。`any` 允许消费 partial 的写产出。
    """
    implement_ids = [
        str(task["id"]) for task in tasks if task.get("role") == "implement"
    ]
    if not implement_ids:
        return
    for task in tasks:
        if task.get("role") not in {"test", "review"}:
            continue
        if task.get("depends_on"):
            continue
        task["depends_on"] = list(implement_ids)
        task["depends_mode"] = "any"


def _task_depth(tasks: list[dict[str, Any]], task_id: str) -> int:
    by_id = {str(task["id"]): task for task in tasks}
    seen: set[str] = set()

    def walk(node: str) -> int:
        if node in seen:
            return 0
        seen.add(node)
        deps = by_id.get(node, {}).get("depends_on") or []
        return 1 + max((walk(str(dep)) for dep in deps), default=0)

    return walk(task_id)


def _retry_context(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "attempt": int(result.get("attempt", 1)),
        "status": str(result.get("status", "")),
        "summary": str(result.get("summary", ""))[:1_000],
        "error": str(result.get("error", ""))[:1_000],
        "findings": [str(item) for item in (result.get("findings") or [])[:8]],
        "verification": result.get("verification") or {},
    }


def _fallback_result(
    by_id: dict[str, dict[str, Any]],
    task_id: str,
    status: str,
    summary: str,
    *,
    error: str = "",
    timed_out: bool = False,
) -> WorkerResult:
    """worker 任务异常/取消时补一条结构化结果，保证事件与状态不缺记录。"""
    task = by_id.get(task_id, {})
    role = str(task.get("role", "explore"))
    return WorkerResult(
        task_id=task_id,
        role=role,
        worker_id=f"{role}:{task_id}",
        goal=str(task.get("goal", "")),
        status=status,
        summary=summary,
        error=error,
        timed_out=timed_out,
    )


def make_scheduler_node(
    *,
    worker_runner: WorkerRunner,
    settings: Settings,
    sandbox: WslSandbox | None = None,
    root: str = "",
    steering: SteeringController | None = None,
    governor: ConcurrencyGovernor | None = None,
) -> Callable[[dict[str, Any], RunnableConfig], Any]:
    async def scheduler(
        state: dict[str, Any], config: RunnableConfig
    ) -> dict[str, Any]:
        from coding_agent.orchestrator.scheduler_run import SchedulerRun

        run = SchedulerRun(
            state,
            config,
            worker_runner=worker_runner,
            settings=settings,
            sandbox=sandbox,
            root=root,
            steering=steering,
            governor=governor,
        )
        return await run.run()

    return scheduler
