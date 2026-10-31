"""RFC-0008：verify 失败后的有界编排修复轮。

只做状态准备，不直接执行工具。真正的 implement worker 仍由 orchestrator
子图调度；本节点负责：

- 递增修复轮计数、生成"只修失败项"的请求；
- 清空上一轮任务图/状态，但保留 worker_results（token 预算与审计需要）；
- 强制 workflow=orchestrated_implement，让 select_workflow 跳过规则/模型选择。
"""

from __future__ import annotations

from typing import Any

from ubismart.graph.state import AgentState
from ubismart.protocol.messages import last_human_text


def prepare_orchestrator_fix(state: AgentState) -> dict[str, Any]:
    verification = state.get("verification") or {}
    summary = str(verification.get("summary") or "").strip()
    failed_tests = [
        str(item)
        for item in (verification.get("failed_tests") or [])
        if str(item).strip()
    ][:20]
    issues = [
        f"{issue.get('location', '')} {issue.get('message', '')}".strip()
        for issue in (verification.get("issues") or [])
        if isinstance(issue, dict)
    ][:10]
    original = last_human_text(state.get("messages") or [])
    used = int(state.get("orchestrator_fix_rounds_used", 0) or 0)

    lines = [
        "自动验证未通过。请只修复失败项，不要重做已经完成的功能。",
    ]
    if original:
        lines.append(f"原始用户请求：{original}")
    if summary:
        lines.append(f"失败摘要：{summary[:1_500]}")
    if failed_tests:
        lines.append("失败测试：" + "、".join(failed_tests))
    if issues:
        lines.append("关键问题：\n" + "\n".join(f"- {item}" for item in issues))
    request = "\n".join(lines)

    results = list(state.get("worker_results") or [])
    return {
        "orchestrator_fix_mode": True,
        "orchestrator_fix_eligible": False,
        "orchestrator_fix_rounds_used": used + 1,
        "orchestrator_fix_request": request,
        # 回到 orchestrator 入口，但强制写工作流，跳过 survey 后的重新选择。
        "execution_mode": "orchestrated",
        "force_workflow": "orchestrated_implement",
        "request": request,
        "prefetched_tasks": [],
        "prefetched_degraded": False,
        "tasks": [],
        "task_status": {},
        "orchestrator_round": 0,
        "orchestrator_degraded": False,
        "orchestrator_summary": "",
        "orchestrator_completed": 0,
        "orchestrator_failed": 0,
        "worker_context": "",
        "worker_brief": "",
        "orchestration_handoff": {},
        "task_extensions_applied": [],
        "task_extension_errors": [],
        # 旧 results 只为预算/审计保留，不能再被当作新提议重复消费。
        "task_extensions_seen": len(results),
        # 新一轮修复使用新的墙钟预算。
        "scheduler_deadline": 0.0,
        "scheduler_done": False,
        "scheduler_continue": False,
        "steering_replan": False,
        "workflow": "",
        "workflow_reason": "",
        "workflow_revised_reason": "",
        "pending_verification": False,
    }
