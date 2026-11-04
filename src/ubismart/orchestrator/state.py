"""独立 orchestrator 子图的状态契约。

子图只声明自己需要的字段，**不复制父图的 messages/plan 等通道**：
父图把 `request`、`cwd`、`repo_profile` 传进来，子图返回编排结果，
由 LangGraph 的父/子状态合并写回 `AgentState`。

这样做的原因：
- 子图 checkpoint 不会携带整段对话历史；
- 事件层仍能按内层节点名翻译；
- 旧编排字段保持原名，runtime/前端不需要同步改协议。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from langchain_core.messages import BaseMessage

from ubismart.protocol.messages import last_human_text


class OrchestratorState(TypedDict, total=False):
    # ---- 父图输入 ----
    request: str
    # 上一轮及更早的会话约束摘要（有界文本），供 select/plan/worker 使用。
    session_brief: str
    # 上一轮未正常收尾时的恢复提示。
    recovery_notice: str
    cwd: str
    thread_id: str
    repo_profile: dict[str, int]
    execution_mode: str

    # ---- survey / select / plan ----
    repo_summary: dict[str, Any]
    # 知识产物 run 作用域（RFC-0009）；未开启时为空。
    knowledge_run_id: str
    workflow: str
    workflow_reason: str
    prefetched_tasks: list[dict[str, Any]]
    prefetched_contract: dict[str, Any]
    prefetched_degraded: bool
    tasks: list[dict[str, Any]]
    task_status: dict[str, str]
    task_attempts: dict[str, int]
    # ---- 契约与计划评审（S2/S4）----
    contract: dict[str, Any]
    contract_unknowns: list[str]
    contract_review: dict[str, Any]
    plan_review_failed: bool
    # S5 预估卡与 gate 决策。
    estimation_card: dict[str, Any]
    # ---- 四维预算（BudgetGuard）----
    budget_snapshot: dict[str, Any]
    budget_abort: dict[str, Any]
    orchestrator_degraded: bool
    orchestrator_round: int
    # 任务图的并行收益预估（只读/观测用；决策在 workflow/plan 阶段完成）。
    orchestrator_parallel_width: int
    orchestrator_parallel_gain: float
    # 跨模块变更契约与波次 gate 结果（RFC-0009 / ADR-0031）。
    change_contract: dict[str, Any]
    wave_gates: list[dict[str, Any]]
    # 轻量控制检查点索引（contract/plan/artifact 引用/预算/早停）。
    control_checkpoints: list[dict[str, Any]]
    # 重复动作熔断信息（tool/args 哈希达到上限）。
    loop_abort: dict[str, Any]
    # 嵌套子编排器：Root=0，子=1；子图内不得再出站卡片/事件。
    sub_depth: int
    suppress_events: bool
    wall_limit_ms: int
    # 瞬时错误重试次数；与父图同名，runtime 统一累加。
    transient_retries: int
    workflow_revised_reason: str

    # ---- dispatch / 动态扩展 ----
    # 与父图同名：implement 补丁合并后需要把本次运行标记为「改过文件」。
    dirty: bool
    run_dirty: bool
    # 合并过补丁、但主 agent 尚未执行自动验证；由 scheduler 置位，
    # 父图 planner/verify 接手后消费。
    pending_verification: bool
    worker_results: list[dict[str, Any]]
    task_extensions_seen: int
    task_extensions_applied: list[dict[str, Any]]
    task_extension_errors: list[str]

    # ---- 修复轮（RFC-0008）----
    orchestrator_fix_eligible: bool
    orchestrator_fix_mode: bool
    orchestrator_fix_rounds_used: int
    orchestrator_fix_request: str
    force_workflow: str

    # ---- fan-in ----
    worker_context: str
    worker_brief: str
    orchestration_artifacts: list[dict[str, Any]]
    # 任务级结构化交接，含 artifact 路径；文本版之外的稳定契约。
    orchestration_handoff: dict[str, Any]
    orchestrator_summary: str
    orchestrator_completed: int
    orchestrator_failed: int

    # ---- steering ----
    steering_cancelled: bool
    steering_aborted: bool
    steering_rollback_steps: int
    plan_version: int
    steering_replan: bool
    steering_replans: int
    steering_applied: list[dict[str, Any]]

    # ---- scheduler（P2）----
    scheduler_done: bool
    scheduler_continue: bool
    scheduler_deadline: float
    # 孤儿 worktree 清理是 run 级动作；checkpoint 边界重入 scheduler 时
    # 不再重复扫描/删除。
    worktrees_pruned: bool
    # 已成功合并到主工作区的文件路径；顺序写的 stage>0 worker 用它做
    # 基线注入与脏文件归属校验。
    merged_paths: list[str]
    # 本 run 的合并批次审计（最近 20 批）：任务 id、是否增量、合并结果。
    merge_batches: list[dict[str, Any]]
    # 自适应并发/退避状态（墙钟 + 计数），跨 checkpoint 边界保留。
    scheduler_rate_limit: dict[str, float]
    token_budget_limit: int
    token_budget_spent: int
    token_budget_reserved: int
    token_budget_exhausted: bool


def request_of(state: Mapping[str, Any]) -> str:
    """取用户请求：子图优先用显式 `request`，旧图回退到 messages。"""
    request = str(state.get("request") or "").strip()
    if request:
        return request
    messages = state.get("messages") or ()
    if isinstance(messages, Sequence):
        return last_human_text(messages)  # type: ignore[arg-type]
    return ""


def messages_of(state: Mapping[str, Any]) -> list[BaseMessage]:
    """兼容旧节点签名时用的最小消息视图。"""
    request = request_of(state)
    if not request:
        return []
    from langchain_core.messages import HumanMessage

    return [HumanMessage(content=request)]
