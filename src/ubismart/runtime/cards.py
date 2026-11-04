"""通知面：把领域事件投影成出站卡片。

投影层只读事件与 `_StreamCtx` 上的状态，不查 Worker、不持有任务状态。
卡片按 `card_id` 幂等 upsert；`plan_version` 用于让旧审批卡失效。
"""

from __future__ import annotations

import time
from typing import Any
from uuid import uuid4

from ubismart.events import (
    ApprovalRequested,
    BudgetWarning,
    CardEvent,
    EstimationReady,
    Event,
    LoopDetected,
    OrchestrationFinished,
    PlanCreated,
    PlanRevised,
    RunFailed,
    RunFinished,
    RunStarted,
    StageDone,
    SteeringApplied,
    TaskGraphCreated,
    Verification,
    WorkerFinished,
    WorkflowRevised,
)


class CardProjector:
    def __init__(
        self,
        *,
        thread_id: str,
        ctx: Any,
        max_silence_s: float = 120.0,
        min_interval_s: float = 10.0,
        approval_ack_timeout_s: int = 300,
        approval_default_action: str = "pause",
    ) -> None:
        self.thread_id = thread_id
        self.ctx = ctx
        self.max_silence_s = max(1.0, float(max_silence_s or 120.0))
        self.min_interval_s = max(0.0, float(min_interval_s or 0.0))
        self.approval_ack_timeout_s = max(0, int(approval_ack_timeout_s))
        self.approval_default_action = str(
            approval_default_action or "pause"
        )
        self.plan_version = int(
            getattr(ctx, "progress", {}).get("plan_version", 0) or 0
        )
        self.worker_finished = 0
        self.worker_failed = 0
        self.last_event_at = time.monotonic()
        self._last_progress_at = 0.0
        self._pending_approvals: set[str] = set()
        self._faults: set[str] = set()

    # ------------------------------------------------------------------
    # 卡片构造
    # ------------------------------------------------------------------
    def _card(
        self,
        *,
        kind: str,
        card_id: str,
        title: str,
        body: str,
        severity: str = "info",
        payload: dict[str, Any] | None = None,
        requires_ack: bool = False,
        ack_timeout_ms: int = 0,
        default_action: str = "",
    ) -> CardEvent:
        now = time.monotonic()
        self.last_event_at = now
        if kind == "progress":
            self._last_progress_at = now
        return CardEvent(
            event_id=uuid4().hex,
            card_id=card_id,
            kind=kind,  # type: ignore[arg-type]
            severity=severity,  # type: ignore[arg-type]
            title=title,
            body=body,
            payload=dict(payload or {}),
            dedupe_key=card_id,
            plan_version=self.plan_version,
            requires_ack=requires_ack,
            ack_timeout_ms=ack_timeout_ms,
            default_action=default_action,
        )

    def _progress_allowed(self, now: float) -> bool:
        return now - self._last_progress_at >= self.min_interval_s

    # ------------------------------------------------------------------
    # 事件投影
    # ------------------------------------------------------------------
    def observe(self, event: Event) -> list[CardEvent]:
        now = time.monotonic()
        self.last_event_at = now
        if isinstance(event, RunStarted):
            # 首卡 T+0：先给一个锚，EstimationReady 到达后按同一 card_id 更新。
            return [
                self._card(
                    kind="progress",
                    card_id=f"estimate:{self.thread_id}",
                    title="已开始",
                    body="正在生成执行预估…",
                    payload={"next_report_point": "预估完成后"},
                )
            ]
        if isinstance(event, EstimationReady):
            self.plan_version = max(
                self.plan_version, int(event.plan_version or 0)
            )
            card = event.card or {}
            recommended = str(card.get("recommended", ""))
            severity = (
                "warn" if recommended in {"confirm", "split"} else "info"
            )
            body = (
                f"P50 {int(card.get('p50_wall_ms') or 0) / 1000:.0f}s / "
                f"{int(card.get('p50_tokens') or 0):,} tokens；"
                f"P90 {int(card.get('p90_wall_ms') or 0) / 1000:.0f}s / "
                f"{int(card.get('p90_tokens') or 0):,} tokens；"
                f"置信度 {card.get('confidence', '')}；"
                f"建议 {recommended or 'auto'}"
            )
            return [
                self._card(
                    kind="estimate",
                    card_id=f"estimate:{self.thread_id}",
                    title="执行预估",
                    body=body,
                    severity=severity,
                    payload={
                        "card": card,
                        "next_report_point": "第一个任务完成时",
                    },
                )
            ]
        if isinstance(
            event, (PlanCreated, PlanRevised, TaskGraphCreated, WorkflowRevised)
        ):
            version = int(getattr(event, "plan_version", 0) or 0)
            self.plan_version = max(self.plan_version, version)
            card_id = f"progress:{self.thread_id}"
            if not self._progress_allowed(now):
                return []
            items = (
                getattr(event, "tasks", [])
                or getattr(event, "steps", [])
                or []
            )
            return [
                self._card(
                    kind="progress",
                    card_id=card_id,
                    title="计划已更新",
                    body=f"plan v{self.plan_version}；{len(items)} 个步骤",
                    payload={"plan_version": self.plan_version},
                )
            ]
        if isinstance(event, ApprovalRequested):
            self.plan_version = max(
                self.plan_version, int(event.plan_version or 0)
            )
            self._pending_approvals.add(event.request_id)
            pending = len(self._pending_approvals)
            return [
                self._card(
                    kind="approval",
                    card_id=f"approval:{event.request_id}",
                    title="需要审批",
                    body=event.command or event.tool or "等待确认",
                    severity="warn",
                    payload={
                        "request_id": event.request_id,
                        "tool": event.tool,
                        "command": event.command,
                        "level": event.level,
                        "plan_version": self.plan_version,
                        "pending_count": pending,
                        "options": ["approve", "deny"],
                        "default_action": self.approval_default_action,
                    },
                    requires_ack=True,
                    ack_timeout_ms=self.approval_ack_timeout_s * 1000,
                    default_action=self.approval_default_action,
                )
            ]
        if isinstance(event, WorkerFinished):
            self.worker_finished += 1
            failed = event.status in {"failed", "blocked", "cancelled"}
            if failed:
                self.worker_failed += 1
            cards: list[CardEvent] = []
            if failed:
                fault_id = f"fault:worker:{event.task_id}"
                if fault_id not in self._faults:
                    self._faults.add(fault_id)
                    cards.append(
                        self._card(
                            kind="fault",
                            card_id=fault_id,
                            title="任务失败",
                            body=(
                                f"{event.role}/{event.task_id}："
                                f"{event.summary or event.status}"
                            ),
                            severity="error",
                            payload={
                                "task_id": event.task_id,
                                "role": event.role,
                                "suggestion": "检查失败摘要；连续失败会回退换路径",
                            },
                        )
                    )
                if self.worker_failed >= 2 and "fault:workers" not in self._faults:
                    self._faults.add("fault:workers")
                    cards.append(
                        self._card(
                            kind="fault",
                            card_id="fault:workers",
                            title="连续任务失败",
                            body=f"已连续 {self.worker_failed} 个任务失败",
                            severity="error",
                            payload={
                                "failed": self.worker_failed,
                                "suggestion": "停止继续重试；回检查点换路径或缩小范围",
                            },
                        )
                    )
            if self._progress_allowed(now):
                cards.append(
                    self._card(
                        kind="progress",
                        card_id=f"progress:{self.thread_id}",
                        title="执行进度",
                        body=(
                            f"已完成 {self.worker_finished} 个任务"
                            + (
                                f"，失败 {self.worker_failed}"
                                if self.worker_failed
                                else ""
                            )
                        ),
                        payload={
                            "finished": self.worker_finished,
                            "failed": self.worker_failed,
                            "last_task": event.task_id,
                            "last_role": event.role,
                            "next_report_point": "下一个任务完成时",
                        },
                    )
                )
            return cards
        if isinstance(event, Verification):
            if event.status not in {"failed", "error"}:
                return []
            fault_id = f"fault:verify:{event.command or self.thread_id}"
            self._faults.add(fault_id)
            return [
                self._card(
                    kind="fault",
                    card_id=fault_id,
                    title="验证失败",
                    body=event.summary or event.command or event.status,
                    severity="error" if event.status == "failed" else "warn",
                    payload={
                        "status": event.status,
                        "issues": list(event.issues or [])[:10],
                        "suggestion": (
                            "回到最近检查点换路径"
                            if event.status == "failed"
                            else "检查执行环境；这不代表验证通过"
                        ),
                    },
                )
            ]
        if isinstance(event, BudgetWarning):
            fault_id = f"fault:budget:{event.dimension}:{event.level}"
            return [
                self._card(
                    kind="fault",
                    card_id=fault_id,
                    title="预算告警",
                    body=event.message
                    or f"{event.dimension} 已用 {event.ratio:.0%}",
                    severity="error" if event.level == "exhausted" else "warn",
                    payload={
                        "dimension": event.dimension,
                        "used": event.used,
                        "limit": event.limit,
                        "suggestion": (
                            "停止派发；检查已完成部分与卡点"
                            if event.level == "exhausted"
                            else "缩小范围或准备阶段交付"
                        ),
                    },
                )
            ]
        if isinstance(event, LoopDetected):
            return [
                self._card(
                    kind="fault",
                    card_id=f"fault:loop:{event.tool}",
                    title="重复动作熔断",
                    body=event.reason
                    or f"{event.tool} 重复 {event.count} 次",
                    severity="error",
                    payload={
                        "task_id": event.task_id,
                        "tool": event.tool,
                        "count": event.count,
                        "suggestion": "换路径或缩小检索范围，不要继续同一动作",
                    },
                )
            ]
        if isinstance(event, StageDone):
            return [
                self._card(
                    kind="stage_done",
                    card_id=(
                        f"stage:{self.thread_id}:"
                        f"{event.plan_version}:{event.stage}"
                    ),
                    title=(
                        f"阶段 {event.stage} 完成"
                        if event.status == "passed"
                        else f"阶段 {event.stage} 未通过"
                    ),
                    body=(
                        f"成功 {event.completed}，失败 {event.failed}"
                        + (f"；{event.reason}" if event.reason else "")
                    ),
                    severity="info"
                    if event.status == "passed"
                    else "error",
                    payload={
                        "stage": event.stage,
                        "status": event.status,
                        "checkpoint_id": event.checkpoint_id,
                        "next_report_point": "下一阶段完成或运行结束时",
                    },
                )
            ]
        if isinstance(event, OrchestrationFinished):
            return [
                self._card(
                    kind="stage_done",
                    card_id=f"stage:{self.thread_id}:{self.plan_version}",
                    title="编排阶段完成",
                    body=(
                        f"成功 {event.completed}，失败/阻塞 {event.failed}"
                    ),
                    payload={
                        "completed": event.completed,
                        "failed": event.failed,
                        "next_report_point": "主 agent 验证/交付时",
                    },
                )
            ]
        if isinstance(event, SteeringApplied):
            if event.action in {"pause", "cancel", "abort", "rollback"}:
                return [
                    self._card(
                        kind="progress",
                        card_id=f"control:{self.thread_id}",
                        title=f"控制动作：{event.action}",
                        body=event.detail or event.action,
                        severity="warn"
                        if event.action in {"abort", "rollback"}
                        else "info",
                        payload={"action": event.action},
                    )
                ]
            return []
        if isinstance(event, RunFailed):
            return [
                self._card(
                    kind="fault",
                    card_id=f"fault:run:{self.thread_id}",
                    title="运行失败",
                    body=event.message,
                    severity="error",
                    payload={
                        "suggestion": "查看失败原因；已完成状态在 checkpoint 中"
                    },
                )
            ]
        if isinstance(event, RunFinished):
            return [
                self._card(
                    kind="done",
                    card_id=f"done:{self.thread_id}",
                    title="运行完成",
                    body="本轮运行已结束",
                    payload={"answer_chars": len(event.answer or "")},
                )
            ]
        return []

    # ------------------------------------------------------------------
    # 最长静默兜底
    # ------------------------------------------------------------------
    def heartbeat(self) -> list[CardEvent]:
        now = time.monotonic()
        if now - self.last_event_at < self.max_silence_s:
            return []
        plan = list(getattr(self.ctx, "plan", []) or [])
        step_idx = int(getattr(self.ctx, "step_idx", 0) or 0)
        current = plan[step_idx] if 0 <= step_idx < len(plan) else ""
        card = self._card(
            kind="heartbeat",
            card_id=f"heartbeat:{self.thread_id}",
            title="仍在执行",
            body=(
                f"当前步骤 {step_idx + 1}/{max(len(plan), 1)}"
                + (f"：{current}" if current else "")
            ),
            payload={
                "step_idx": step_idx,
                "total": len(plan),
                "plan_version": self.plan_version,
                "next_report_point": "下一个任务完成或检查点提交时",
            },
        )
        self.last_event_at = now
        return [card]
