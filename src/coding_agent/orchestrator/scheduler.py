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

本模块只负责**装配**节点；任务图上的纯判断（依赖是否满足、重试上下文、
失败兜底结果）在 `scheduler_plan.py`，运行态在 `scheduler_run.py` —— 这样
`orchestrator.scheduler ⇄ orchestrator.scheduler_run` 那条环就不存在了。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig

from coding_agent.concurrency import (
    ConcurrencyGovernor,
)
from coding_agent.config import Settings
from coding_agent.orchestrator.workers import WorkerRunner
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.steering import SteeringController

SCHEDULER = "scheduler"


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
