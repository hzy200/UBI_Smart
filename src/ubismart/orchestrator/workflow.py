"""动态工作流选择（Phase 3A）。

规则优先，规则不确定时才问模型；两者都失败时退回 `direct`。模型只能在
预置模板里选，不能自由发明工作流。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from pydantic import BaseModel, Field

from ubismart.config import Settings
from ubismart.graph.state import AgentState
from ubismart.llm.deepseek import with_json_output
from ubismart.orchestrator.contracts import TaskSpec, WorkflowDecision
from ubismart.orchestrator.parallelism import (
    dedupe_readonly_tasks,
    worth_parallelizing,
)
from ubismart.orchestrator.state import request_of
from ubismart.orchestrator.survey import compact_repo_summary
from ubismart.orchestrator.workflow_builder import build_dynamic_plan
from ubismart.session.recovery import retry_async


class WorkflowPlan(BaseModel):
    """工作流选择 + 任务图一次返回，避免模糊请求付两次 LLM 往返。"""

    workflow: Literal[
        "direct",
        "parallel_readonly",
        "orchestrated_implement",
        "document_pipeline",
        "architecture_change",
    ] = "direct"
    reason: str = ""
    tasks: list[TaskSpec] = Field(default_factory=list)
    degraded: bool = False

# 明确的「多交付物/多区域」信号：命中时不必再问模型。
_MULTI_HINTS = (
    "同时",
    "分别",
    "各自",
    "多个",
    "两个",
    "两件",
    "三个",
    "两条",
    "一起",
    "多条",
    "另外",
    "各加",
    "各修",
    "都要",
)

_DOCUMENT_HINTS = (
    "pdf",
    "word",
    "docx",
    "excel",
    "xlsx",
    "xls",
    "ppt",
    "pptx",
    "markdown",
    "文档",
    "报告",
    "提炼",
    "总结",
    "归纳",
    "图片",
    "图像",
    "截图",
    "扫描件",
    "表格",
)

WORKFLOW_PROMPT = """\
你是编码智能体的编排器。根据用户请求和仓库画像，在四种工作流中选择：

- `direct`：单 agent 直接执行。适合单点修改、单一功能线、小范围任务。
- `parallel_readonly`：先生成 2–4 个只读子任务（explore/test/review），并发收集
  事实，再把汇总交给主 agent 实现。适合跨多个模块、需要同时定位多处、或有多个
  独立交付物的任务。
- `orchestrated_implement`：生成多个**互相独立**的 implement 任务，各自在 git
  worktree 中并发修改，最后由宿主合并补丁。仅在写编排已开启、工作区是干净的
  git 仓库、且任务能拆成**写范围不重叠**的独立块时选择。
- `document_pipeline`：阅读 PDF/Office/图片，按文档并行提取，再用 author 写报告。
  适用于上传了文档/图片、或请求明确要求提炼/总结/撰写报告。写权限不可用时
  仍可运行，只是不生成报告文件。

判断要保守：如果一条功能线是连贯的、改几个文件就能完成，选 `direct`；
只有确实能拆成互不依赖的区域时才选并行模板。写任务之间不能有依赖，
write_scope 不能重叠。

只输出 JSON：
{"workflow": "direct|parallel_readonly|orchestrated_implement|document_pipeline",
 "reason": "一句话"}
"""


def rule_workflow(
    request: str,
    summary: dict[str, Any],
    settings: Settings,
    *,
    write_available: bool = False,
) -> WorkflowDecision | None:
    """返回确定性的选择；None 表示规则不确定，交给模型。"""
    lowered = request.lower()
    uploads = [str(item) for item in (summary.get("uploads") or [])]
    pipeline_enabled = bool(
        getattr(settings, "document_pipeline_enabled", True)
    )
    if pipeline_enabled and (
        uploads or any(hint in lowered for hint in _DOCUMENT_HINTS)
    ):
        return WorkflowDecision(
            workflow="document_pipeline",
            reason=(
                "请求涉及文档/图片阅读或报告撰写"
                + (f"（本会话有 {len(uploads)} 个上传）" if uploads else "")
            ),
        )
    file_count = int(summary.get("file_count") or 0)
    if not file_count or file_count < settings.orchestrator_min_files:
        return WorkflowDecision(
            workflow="direct",
            reason=f"仓库只有 {file_count or '未知'} 个文件，直接执行更便宜",
        )
    if any(hint in request for hint in _MULTI_HINTS):
        if write_available:
            return WorkflowDecision(
                workflow="orchestrated_implement",
                reason="请求里有多个独立交付物，且写编排可用",
            )
        return WorkflowDecision(
            workflow="parallel_readonly",
            reason="请求里出现多个独立交付物/区域",
        )
    return None


def make_select_workflow_node(
    llm: BaseChatModel, settings: Settings, *, allow_write: bool = False
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    # 重试统一由外层 retry_async 负责；内层再 with_retry 会乘出
    # 2 × retry_attempts 次调用，既不省时也不省钱。
    structured = with_json_output(llm, WorkflowPlan)

    async def select_workflow(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        request = request_of(state)
        summary = state.get("repo_summary") or {}
        session_brief = str(state.get("session_brief") or "").strip()
        recovery_notice = str(state.get("recovery_notice") or "").strip()
        fix_mode = bool(state.get("orchestrator_fix_mode"))
        write_available = bool(
            settings.orchestrator_write_enabled
            and allow_write
            and summary.get("git")
            and summary.get("git_root")
            and (summary.get("git_clean") or fix_mode)
        )
        forced = str(state.get("force_workflow") or "").strip()
        if forced in {
            "direct",
            "parallel_readonly",
            "orchestrated_implement",
            "document_pipeline",
            "architecture_change",
        }:
            if forced == "orchestrated_implement" and not write_available:
                forced = "parallel_readonly"
            return {
                "workflow": forced,
                "workflow_reason": "修复轮强制工作流",
                "prefetched_tasks": [],
                "prefetched_degraded": False,
                "force_workflow": "",
                "transient_retries": 0,
            }
        built = build_dynamic_plan(
            request,
            summary,
            settings,
            write_available=write_available,
        )
        if built is not None:
            return {
                "workflow": built["workflow"],
                "workflow_reason": built["reason"],
                "prefetched_tasks": built["tasks"],
                "prefetched_degraded": False,
                "force_workflow": "",
                "transient_retries": 0,
            }

        decision = rule_workflow(
            request, summary, settings, write_available=write_available
        )
        prefetched: list[dict[str, Any]] = []
        prefetched_degraded = False
        retry_events: list[dict[str, Any]] = []

        if decision is None:
            # 规则不确定时一次调用同时拿到 workflow 和任务图，省掉
            # select → plan 的第二次 LLM 往返。
            from ubismart.orchestrator.plan import (
                _IMPLEMENT_ROLE_NOTES,
                _IMPLEMENT_RULES,
                _NO_SEQUENTIAL_WRITE_RULES,
                _READ_ONLY_ROLE_NOTES,
                _READ_ONLY_RULES,
                _SEQUENTIAL_WRITE_RULES,
                PLAN_TASKS_PROMPT,
            )

            write_rules = _READ_ONLY_RULES
            if write_available:
                write_rules = _IMPLEMENT_RULES + "\n" + (
                    _SEQUENTIAL_WRITE_RULES
                    if settings.orchestrator_sequential_writes
                    else _NO_SEQUENTIAL_WRITE_RULES
                )
            task_prompt = PLAN_TASKS_PROMPT.format(
                max_tasks=settings.orchestrator_max_tasks,
                role_notes=(
                    _IMPLEMENT_ROLE_NOTES
                    if write_available
                    else _READ_ONLY_ROLE_NOTES
                ),
                write_rules=write_rules,
            )
            workflow_prompt = WORKFLOW_PROMPT
            if settings.orchestrator_sequential_writes:
                workflow_prompt += (
                    "\n写任务允许按 stage 分波：后一波 implement 可以依赖"
                    " stage 更小的 implement；同一 stage 仍必须写范围不重叠"
                    "且不能互相依赖。"
                )
            body = (
                f"用户请求：{request}\n\n"
                + (
                    "之前轮次的会话约束（继续遵守）：\n"
                    f"{session_brief}\n\n"
                    if session_brief
                    else ""
                )
                + (
                    f"上次运行中断的恢复提示：\n{recovery_notice}\n\n"
                    if recovery_notice
                    else ""
                )
                + f"仓库画像：{compact_repo_summary(summary)}\n\n"
                "请先选择 workflow，再给出任务图。"
                "输出 JSON："
                '{"workflow": "direct|parallel_readonly|orchestrated_implement|document_pipeline",'
                ' "reason": "一句话", "tasks": [...]}'
            )
            try:
                plan = await retry_async(
                    lambda: structured.ainvoke(
                        [
                            SystemMessage(
                                content=workflow_prompt + "\n\n" + task_prompt
                            ),
                            HumanMessage(content=body),
                        ],
                        config,
                    ),
                    attempts=settings.retry_attempts,
                    base_delay=settings.retry_base_delay_s,
                    max_delay=settings.retry_max_delay_s,
                    max_total_wait=settings.retry_max_total_wait_s,
                    on_retry=retry_events.append,
                    label="select_workflow",
                )
            except Exception:  # noqa: BLE001 - 选择失败不能拖垮图
                plan = None

            if isinstance(plan, WorkflowPlan):
                decision = WorkflowDecision(
                    workflow=plan.workflow, reason=plan.reason
                )
                prefetched = [task.model_dump() for task in plan.tasks]
                prefetched_degraded = bool(plan.degraded)
            elif isinstance(plan, WorkflowDecision):
                decision = plan

        if decision is None:
            decision = WorkflowDecision(
                workflow="direct", reason="工作流选择失败，回退 direct"
            )

        # 预取任务先去重；完全相同的只读任务没有必要各跑一个 worker。
        if prefetched:
            prefetched, deduped = dedupe_readonly_tasks(prefetched)
            if deduped:
                decision = WorkflowDecision(
                    workflow=decision.workflow,
                    reason=(
                        f"{decision.reason}；去重只读任务 {len(deduped)} 个"
                    ).strip("；"),
                )
        # 链式任务的并行收益接近 1：走 direct 更便宜，也不会更慢。
        if (
            decision.workflow == "parallel_readonly"
            and prefetched
            and not worth_parallelizing(
                prefetched, settings.orchestrator_min_parallel_gain
            )
        ):
            decision = WorkflowDecision(
                workflow="direct",
                reason=(
                    f"并行收益不足（{len(prefetched)} 个任务的关键路径"
                    f"接近串行），回退 direct"
                ),
            )
            prefetched = []

        # 模型可能无视条件选择写模板；这里做硬性兜底。
        if decision.workflow == "orchestrated_implement" and not write_available:
            decision = WorkflowDecision(
                workflow="parallel_readonly",
                reason="写编排不可用，回退为只读并行",
            )
            prefetched = []

        return {
            "workflow": decision.workflow,
            "workflow_reason": decision.reason,
            "prefetched_tasks": prefetched,
            "prefetched_degraded": prefetched_degraded,
            "transient_retries": len(retry_events),
        }

    return select_workflow
