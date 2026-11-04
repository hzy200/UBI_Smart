"""嵌套子编排器（MAX_DEPTH=1）。

子编排器是**同一套控制面契约的递归实例**，不是自治 while 循环：
- 子图走 Contract → Plan → Review → Estimation → Scheduler → Fan-in；
- 父只发 SubTask、收 SubResult；子不写父状态、不推前端卡片；
- 预算由父切片传入，子 `TokenBudget` 以切片为上限；
- 子任务限制为只读角色，审批因此不需要下沉（写审批仍只在 Root）。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel

from ubismart.config import Settings
from ubismart.orchestrator.contracts import (
    SubResult,
    SubSignal,
    SubTask,
    TaskContract,
)
from ubismart.orchestrator.workers import WorkerRunner
from ubismart.platform.concurrency import ConcurrencyGovernor
from ubismart.sandbox.wsl_exec import WslSandbox
from ubismart.session.steering import SteeringController


class SubOrchestratorRunner:
    def __init__(
        self,
        *,
        llm: BaseChatModel,
        planning_llm: BaseChatModel | None,
        settings: Settings,
        sandbox: WslSandbox | None,
        root: str,
        worker_runner: WorkerRunner,
        governor: ConcurrencyGovernor | None = None,
        graph_factory: Callable[..., Any],
    ) -> None:
        self.llm = llm
        self.planning_llm = planning_llm or llm
        self.settings = settings
        self.sandbox = sandbox
        self.root = root
        self.worker_runner = worker_runner
        self.governor = governor
        self.graph_factory = graph_factory

    async def run(
        self,
        task: dict[str, Any],
        *,
        parent_thread_id: str,
        cwd: str,
        repo_summary: dict[str, Any],
        budget_tokens: int,
        wall_ms: int,
        plan_version: int,
        depth: int,
    ) -> SubResult:
        sub_id = f"sub:{task.get('id', 'task')}"
        raw_contract = task.get("sub_contract") or {}
        try:
            contract = TaskContract.model_validate(raw_contract)
        except Exception as exc:  # noqa: BLE001 - 子契约不合法时局部失败
            return SubResult(
                sub_id=sub_id,
                parent_task_id=str(task.get("id", "")),
                depth=depth,
                status="failed",
                fault_reason=f"子契约不合法：{exc}",
                suggested_action="修正 sub_contract 的 goal/acceptance 后重试",
            )
        if not contract.goal:
            contract.goal = str(task.get("goal", "")).strip()
        budget = int(budget_tokens or 0)
        if budget <= 0:
            # 父没有限制时才退回全局配置；父有预算时由 scheduler 传正数。
            budget = int(self.settings.orchestrator_total_tokens or 0)
        deadline = (
            time.monotonic() + wall_ms / 1000.0 if wall_ms > 0 else 0.0
        )
        sub_task = SubTask(
            sub_id=sub_id,
            parent_task_id=str(task.get("id", "")),
            depth=depth,
            contract_slice=contract,
            budget_slice={
                "max_tokens": budget,
                "max_wall_ms": int(wall_ms or 0),
            },
            plan_version=int(plan_version or 0),
            max_depth=int(self.settings.orchestrator_max_depth or 1),
            max_fanout=int(self.settings.orchestrator_max_fanout or 1),
        )
        child_thread = f"{parent_thread_id}:{sub_id}"
        child_state: dict[str, Any] = {
            "request": contract.goal or str(task.get("goal", "")),
            "execution_mode": "orchestrated",
            "workflow": "parallel_readonly",
            "force_workflow": "parallel_readonly",
            "contract": contract.model_dump(),
            "prefetched_tasks": [],
            "prefetched_contract": {},
            "repo_summary": dict(repo_summary or {}),
            "repo_profile": dict(repo_summary or {}),
            "cwd": cwd,
            "thread_id": child_thread,
            "sub_depth": depth,
            "suppress_events": True,
            "token_budget_limit": budget,
            "wall_limit_ms": int(wall_ms or 0),
            "scheduler_deadline": deadline,
            "plan_version": int(plan_version or 0),
        }
        try:
            graph = self.graph_factory(
                llm=self.llm,
                settings=self.settings,
                sandbox=self.sandbox,  # type: ignore[arg-type]
                root=self.root,
                worker_runner=self.worker_runner,
                steering=SteeringController(),
                allow_write=False,
                governor=self.governor,
                planning_llm=self.planning_llm,
            )
            final = await graph.ainvoke(
                child_state,
                {"configurable": {"thread_id": child_thread}},
            )
        except Exception as exc:  # noqa: BLE001 - 子失败局部化
            return SubResult(
                sub_id=sub_id,
                parent_task_id=str(task.get("id", "")),
                depth=depth,
                status="failed",
                fault_reason=f"{type(exc).__name__}: {exc}",
                suggested_action="检查子任务契约或缩小范围后重试",
            )

        final = final if isinstance(final, dict) else {}
        results = final.get("worker_results") or []
        input_tokens = sum(
            int(item.get("input_tokens") or 0) for item in results
        )
        output_tokens = sum(
            int(item.get("output_tokens") or 0) for item in results
        )
        wall_used = sum(
            int(item.get("duration_ms") or 0) for item in results
        )
        statuses = [
            str(item.get("status", "")) for item in results
        ]
        failed = any(
            value in {"failed", "blocked", "cancelled"} for value in statuses
        )
        succeeded = any(
            value in {"success", "partial"} for value in statuses
        )
        if final.get("contract_unknowns") or final.get("plan_review_failed"):
            status = "failed"
        elif failed:
            status = "partial" if succeeded else "failed"
        else:
            status = "success"
        checkpoints = [
            item
            for item in (final.get("control_checkpoints") or [])
            if isinstance(item, dict)
        ]
        local_ckpt = (
            str(checkpoints[-1].get("checkpoint_id", ""))
            if checkpoints
            else ""
        )
        summary = str(final.get("orchestrator_summary", ""))
        fault_reason = summary if status == "failed" else ""
        signals = [
            SubSignal(
                sub_id=sub_id,
                parent_task_id=str(task.get("id", "")),
                depth=depth,
                kind="stage_done" if local_ckpt else "progress",
                severity="info" if status == "success" else "warn",
                progress_digest={
                    "summary": summary[:500],
                    "completed": int(
                        final.get("orchestrator_completed", 0) or 0
                    ),
                    "failed": int(final.get("orchestrator_failed", 0) or 0),
                },
                dedupe_key=f"{sub_id}:final",
            ).model_dump()
        ]
        return SubResult(
            sub_id=sub_task.sub_id,
            parent_task_id=sub_task.parent_task_id,
            depth=depth,
            status=status,  # type: ignore[arg-type]
            artifact={
                "summary": summary,
                "worker_brief": str(final.get("worker_brief", "")),
                "handoff": dict(final.get("orchestration_handoff") or {}),
                "signals": signals,
            },
            local_ckpt_digest=local_ckpt,
            actual_cost={
                "steps": len(results),
                "tool_calls": sum(
                    len(item.get("tool_calls") or [])
                    for item in results
                ),
                "tokens": input_tokens + output_tokens,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "wall_ms": wall_used,
            },
            rollback_level=(
                "L2"
                if any(
                    item.get("status") == "failed" for item in checkpoints
                )
                else ""
            ),
            fault_reason=fault_reason,
            suggested_action=(
                "根据子任务失败摘要缩小范围或改用阶段门"
                if fault_reason
                else ""
            ),
        )
