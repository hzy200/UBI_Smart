"""LangGraph update → 领域事件的翻译层。

这里的所有方法都只做一件事：把节点返回的原始 update 变成前端可消费的
Event。它们不推进图、不执行工具、不写审计（审计仍在 core 里，由 mixin
调用）。拆出来的目的是让 `core.py` 只剩编排与生命周期。
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from ubismart.audit import AuditRecord
from ubismart.audit import models as audit_models
from ubismart.audit.logger import now_iso, sanitize_args, truncate
from ubismart.events import (
    ApprovalRequested,
    AssistantToken,
    ContextTrimmed,
    Event,
    FileChanged,
    OrchestrationFinished,
    PlanCreated,
    PlanRevised,
    ReasoningToken,
    RepairStarted,
    ReviewFinished,
    SteeringApplied,
    StepFinished,
    StepStarted,
    SubAgentFinished,
    SubAgentStarted,
    TaskBlocked,
    TaskGraphCreated,
    TaskGraphExtended,
    ToolCallFinished,
    ToolCallStarted,
    Verification,
    WorkerFinished,
    WorkerStarted,
    WorkflowRevised,
    WorkflowSelected,
)
from ubismart.graph.nodes.approve import tool_level
from ubismart.protocol.messages import reasoning_of, text_of
from ubismart.sandbox.policy import classify
from ubismart.sandbox.snapshots import ACTION_RESTORE
from ubismart.tools.artifacts import (
    CallArtifact,
    FileArtifact,
    InterruptedArtifact,
    ShellArtifact,
    SubAgentArtifact,
    parse_artifact,
)
from ubismart.tools.delegate import DELEGATE_EXPLORE
from ubismart.tools.shell import SHELL_TOOL_NAME

if TYPE_CHECKING:
    from ubismart.runtime.stream import _StreamCtx

MUTATING_FILE_ACTIONS = frozenset(
    {
        "create",
        "overwrite",
        "edit",
        "move",
        "copy",
        "delete",
        "mkdir",
        ACTION_RESTORE,
    }
)

STREAMING_NODES = frozenset({"act", "respond"})

_PREVIEW_CHARS = 240

_FAILURE_MARKERS = ("工具执行异常：", "错误：")

_SUMMARY_KEYS = ("command", "path", "pattern", "query")

_PLAN_DEGRADED_DETAIL = "规划未解析，已退化为单步执行"

_VERIFICATION_BLOCKING_STATUS = "failed"

def _verification_passed(raw: dict[str, Any]) -> bool:
    return str(raw.get("status", "skipped")) != _VERIFICATION_BLOCKING_STATUS

def _verification_verdict(raw: dict[str, Any]) -> bool | None:
    """**审计**里的验证结论：True 通过 / False 失败 / **None 没验证**。

    与 `_verification_passed`（路由口径）刻意分开：路由只需要"要不要拦"，所以
    `not_configured` / `skipped` 都算"不拦"；但审计要回答的是"这次到底验证了没有"，
    把"没做"记成 `ok=True` 会让**验证有缺口在账上看不出来** —— 而审计本该是可信
    来源，这正是本项目最不能接受的失败模式之一（原 IMPROVEMENT_PLAN 的 P0-2，
    去向见 `docs/archive/README.md`）。
    """
    status = str(raw.get("status", "skipped"))
    if status == _VERIFICATION_BLOCKING_STATUS:
        return False
    if status == "ok":
        return True
    # error / not_configured / skipped：没验证成功，既不是通过也不是测试失败
    return None

_REVIEW_BLOCKING_STATUS = "blocked"

def _review_blocked(raw: dict[str, Any]) -> bool:
    return str(raw.get("status", "skipped")) == _REVIEW_BLOCKING_STATUS

def _last_message(update: dict[str, Any]) -> Any:
    messages = update.get("messages") or []
    return messages[-1] if messages else None

def _worker_key(result: dict[str, Any]) -> str:
    """去重键：同一任务的重试是不同的 worker 运行。"""
    return (
        f"{result.get('worker_id', '')}:"
        f"{int(result.get('attempt', 1) or 1)}"
    )

def _preview(text: str, limit: int = _PREVIEW_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[:limit]}…"

def _summarize_args(args: dict[str, Any]) -> str:
    for key in _SUMMARY_KEYS:
        if args.get(key):
            return str(args[key])
    return ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])

def _add_usage(message: Any, input_tokens: int, output_tokens: int) -> tuple[int, int]:
    """把模型回报的用量累加进来（provider 没给就是 0）。

    用 provider 的 `usage_metadata` 而不是 tokenizer：无需额外依赖，
    口径也就是实际计费口径。
    """
    meta = getattr(message, "usage_metadata", None) or {}
    return (
        input_tokens + int(meta.get("input_tokens") or 0),
        output_tokens + int(meta.get("output_tokens") or 0),
    )


class EventMapMixin:
    """`AgentRuntime` 的事件翻译职责。"""

    _NODE_HANDLERS: dict[str, str] = {
        "planner": "_on_planner",
        "select_workflow": "_on_select_workflow",
        "plan_tasks": "_on_plan_tasks",
        "scheduler": "_on_scheduler",
        "synthesize": "_on_synthesize",
        "advance": "_on_advance",
        "replan": "_on_replan",
        "act": "_on_act",
        "tools": "_on_tools",
        "verify": "_on_verify",
        "review": "_on_review",
        "nudge": "_on_nudge",
        "repair": "_on_repair",
        "respond": "_on_respond",
    }

    def _open_step(
        self, ctx: _StreamCtx, index: int, total: int, text: str
    ) -> Iterator[Event]:
        """发 StepStarted。上一步若还开着，先补一个收口再开新的。"""
        if ctx.step_open:
            ctx.step_open = False
            yield StepFinished(index=ctx.step_idx, cancelled=True, text="")
        ctx.step_open = True
        ctx.progress["step_open"] = True
        yield StepStarted(index=index, total=total, text=text)

    def _close_step(
        self, ctx: _StreamCtx, *, cancelled: bool = False
    ) -> Iterator[Event]:
        """收口当前步骤。已经关了就是空操作，不会多发一个事件。"""
        if not ctx.step_open:
            return
        ctx.step_open = False
        ctx.progress["step_open"] = False
        yield StepFinished(index=ctx.step_idx, cancelled=cancelled, text="")

    def _update_events(
        self, ctx: _StreamCtx, node: str, update: dict[str, Any]
    ) -> Iterator[Event]:
        """把单个节点的 update 翻成事件；未知节点原样忽略。"""
        retries = int(update.get("transient_retries") or 0)
        if retries:
            ctx.progress["transient_retries"] = (
                int(ctx.progress.get("transient_retries", 0)) + retries
            )

        trim = update.get("context_trim")
        if isinstance(trim, dict):
            yield ContextTrimmed(
                node=str(trim.get("node") or node),
                trimmed=int(trim.get("trimmed", 0) or 0),
                before_chars=int(trim.get("before_chars", 0) or 0),
                after_chars=int(trim.get("after_chars", 0) or 0),
                before_tokens=int(trim.get("before_tokens", 0) or 0),
                after_tokens=int(trim.get("after_tokens", 0) or 0),
                escalated=bool(trim.get("escalated")),
            )

        # Phase 3D：四个 steering 消费点共用同一套事件载荷。
        if node.startswith("steering"):
            yield from self._on_steering(ctx, update)
            return

        handler = self._NODE_HANDLERS.get(node)
        if handler is not None:
            events = getattr(self, handler)(ctx, update)
            if events is not None:
                yield from events

    def _on_steering(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        for applied in update.get("steering_applied") or []:
            yield SteeringApplied(
                action=str(applied.get("action", "")),
                task_id=str(applied.get("task_id", "")),
                detail=str(applied.get("detail", "")),
            )
        if update.get("steering_cancelled"):
            yield from self._close_step(ctx, cancelled=True)

    def _on_planner(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        ctx.plan = [s for s in (update.get("plan") or []) if s]
        ctx.plan_kinds = [str(k) for k in (update.get("plan_kinds") or [])]
        ctx.step_idx = 0
        ctx.progress["plan"] = ctx.plan
        ctx.progress["plan_kinds"] = ctx.plan_kinds
        ctx.progress["step_idx"] = 0
        if ctx.plan:
            # 降级必须同时进事件与审计：只在事件里说的话，
            # 事后复盘仍然查不到「这次规划其实是失败的」。
            degraded = bool(update.get("plan_degraded"))
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.PLAN,
                    thread_id=ctx.thread_id,
                    steps=ctx.plan,
                    kinds=ctx.plan_kinds or None,
                    detail=_PLAN_DEGRADED_DETAIL if degraded else "",
                )
            )
            yield PlanCreated(
                steps=ctx.plan, kinds=ctx.plan_kinds, degraded=degraded
            )
        yield from self._open_step(
            ctx, 0, max(len(ctx.plan), 1), ctx.plan[0] if ctx.plan else ""
        )

    def _on_select_workflow(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        if "workflow" not in update:
            return
        workflow = str(update.get("workflow", ""))
        reason = str(update.get("workflow_reason", ""))
        ctx.progress["workflow"] = workflow
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.WORKFLOW,
                thread_id=ctx.thread_id,
                workflow=workflow,
                detail=truncate(reason),
            )
        )
        yield WorkflowSelected(workflow=workflow, reason=reason)

    def _on_plan_tasks(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        tasks = update.get("tasks") or []
        if "tasks" not in update:
            return
        degraded = bool(update.get("orchestrator_degraded"))
        degradation_note = str(update.get("orchestrator_summary", "")).strip()
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.TASK_GRAPH,
                thread_id=ctx.thread_id,
                workflow=ctx.progress.get("workflow"),
                steps=[str(task.get("goal", "")) for task in tasks],
                detail=(
                    "任务图已降级"
                    + (f"：{degradation_note}" if degradation_note else "")
                    if degraded
                    else ""
                ),
            )
        )
        yield TaskGraphCreated(tasks=tasks, degraded=degraded)
        revised_reason = str(update.get("workflow_revised_reason") or "")
        if revised_reason:
            yield WorkflowRevised(reason=revised_reason, tasks=tasks)

    def _on_scheduler(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        results = update.get("worker_results") or []
        emitted = int(ctx.progress.get("workers_emitted", 0))
        fresh = results[emitted:]
        # custom 流已经实时发过的事件不再重复；直接调用节点
        # （单测）没有 custom writer 时，这里负责补齐。
        started_seen = ctx.progress.setdefault("worker_started_keys", set())
        finished_seen = ctx.progress.setdefault("worker_finished_keys", set())
        for result in fresh:
            key = _worker_key(result)
            if key in started_seen:
                continue
            started_seen.add(key)
            yield WorkerStarted(
                worker_id=str(result.get("worker_id", "")),
                task_id=str(result.get("task_id", "")),
                role=str(result.get("role", "")),
                goal=str(result.get("goal", "")),
                attempt=int(result.get("attempt", 1)),
                queue_wait_ms=int(result.get("queue_wait_ms", 0)),
            )
        for result in fresh:
            key = _worker_key(result)
            if key in finished_seen:
                continue
            finished_seen.add(key)
            yield from self._worker_events(result, ctx.thread_id, ctx.progress)
        ctx.progress["workers_emitted"] = len(results)
        # custom 流没覆盖到的阻塞（例如直接调用节点、或收尾时批量置位）
        # 在 updates 分支补齐；按 task_id 去重，避免边界重入重复发。
        status = update.get("task_status") or {}
        tasks_by_id = {
            str(task.get("id", "")): task
            for task in (update.get("tasks") or [])
            if isinstance(task, dict)
        }
        blocked_seen = ctx.progress.setdefault("task_blocked_keys", set())
        if isinstance(status, dict):
            for task_id, value in status.items():
                if value != "blocked" or task_id in blocked_seen:
                    continue
                blocked_seen.add(task_id)
                task = tasks_by_id.get(task_id) or {}
                yield TaskBlocked(
                    task_id=str(task_id),
                    role=str(task.get("role", "")),
                    reason=str(
                        task.get("blocked_reason")
                        or "依赖未满足或调度限制"
                    ),
                )
        # 事件驱动 scheduler 在同一个节点里完成动态扩展，
        # 没有独立的 extend_tasks 更新可翻译。
        added = update.get("task_extensions_applied") or []
        rejected = update.get("task_extension_errors") or []
        if added or rejected:
            if added:
                self._audit(
                    AuditRecord(
                        ts=now_iso(),
                        kind=audit_models.TASK_GRAPH,
                        thread_id=ctx.thread_id,
                        workflow=ctx.progress.get("workflow"),
                        steps=[str(task.get("goal", "")) for task in added],
                        detail=(
                            f"运行中扩展 {len(added)} 个只读任务"
                            + (f"；拒绝 {len(rejected)} 个" if rejected else "")
                        ),
                    )
                )
            yield TaskGraphExtended(added=added, rejected=rejected)

    def _on_synthesize(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        completed = int(update.get("orchestrator_completed", 0))
        failed = int(update.get("orchestrator_failed", 0))
        summary = str(update.get("orchestrator_summary", ""))
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.ORCHESTRATION,
                thread_id=ctx.thread_id,
                workflow=ctx.progress.get("workflow"),
                ok=failed == 0,
                detail=truncate(summary),
            )
        )
        yield OrchestrationFinished(
            completed=completed, failed=failed, summary=summary
        )

    def _on_advance(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        # 先收口再推进：补发的 StepFinished 必须带**旧**的 index，
        # 而 step_idx 下一行就要被改掉
        yield from self._close_step(ctx)
        ctx.step_idx = int(update.get("step_idx", ctx.step_idx + 1))
        ctx.progress["step_idx"] = ctx.step_idx
        yield from self._open_step(
            ctx,
            ctx.step_idx,
            len(ctx.plan),
            ctx.plan[ctx.step_idx] if ctx.step_idx < len(ctx.plan) else "",
        )

    def _on_replan(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        # 节点不修订时返回空更新，只有真改了才发事件
        if "plan" not in update:
            return
        revised = [s for s in (update.get("plan") or []) if s]
        plan_kinds = [str(k) for k in (update.get("plan_kinds") or [])]
        detail = (
            f"剩余 {max(len(ctx.plan) - ctx.step_idx, 0)} 步"
            f" → {max(len(revised) - ctx.step_idx, 0)} 步"
        )
        ctx.plan = revised
        ctx.progress["plan"] = ctx.plan
        ctx.progress["plan_kinds"] = plan_kinds
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.REPLAN,
                thread_id=ctx.thread_id,
                steps=ctx.plan,
                kinds=plan_kinds or None,
                detail=detail,
            )
        )
        yield PlanRevised(
            steps=ctx.plan, kinds=plan_kinds, step_idx=ctx.step_idx
        )
        # 计划被砍到当前这步之前（含当前这步）时，路由会直接去收尾，
        # act 再也不跑 —— 这一步必须在这里收口，否则前端的进度条
        # 永远停在「进行中」
        if ctx.step_idx >= len(ctx.plan):
            yield from self._close_step(ctx, cancelled=True)

    def _on_act(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        last = _last_message(update)
        ctx.input_tokens, ctx.output_tokens = _add_usage(
            last, ctx.input_tokens, ctx.output_tokens
        )
        ctx.progress["input_tokens"] = ctx.input_tokens
        ctx.progress["output_tokens"] = ctx.output_tokens
        calls = getattr(last, "tool_calls", None) or []
        for call in calls:
            started = self._tool_started(
                str(call.get("name", "")),
                dict(call.get("args") or {}),
                str(call.get("id", "")),
            )
            ctx.pending[started.call_id] = started
            yield started
            # 与 ToolCallStarted 并列：delegate 本身也是一次工具调用，
            # 但前端需要知道「这是一次委派、目标是什么」。
            if started.name == DELEGATE_EXPLORE:
                yield SubAgentStarted(
                    agent_id=started.call_id,
                    parent_call_id=started.call_id,
                    role="explore",
                    goal=str(dict(call.get("args") or {}).get("goal", "")),
                )
        if not calls:
            ctx.step_open = False
            ctx.progress["step_open"] = False
            yield StepFinished(
                index=ctx.step_idx,
                budget_exhausted=bool(update.get("budget_exhausted")),
                text=text_of(last),
            )

    def _on_tools(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        for message in update.get("messages") or []:
            finished = self._tool_finished(message)
            started = ctx.pending.pop(finished.call_id, None)
            self._audit_tool_call(started, finished, ctx.thread_id)
            yield finished

            if started is not None and started.name == DELEGATE_EXPLORE:
                artifact = parse_artifact(getattr(message, "artifact", None))
                if isinstance(artifact, SubAgentArtifact):
                    # 子智能体的 token 也算进本轮总量：不记的话，
                    # 开启委派后成本会系统性偏低。
                    ctx.input_tokens += artifact.input_tokens
                    ctx.output_tokens += artifact.output_tokens
                    ctx.progress["input_tokens"] = ctx.input_tokens
                    ctx.progress["output_tokens"] = ctx.output_tokens
                    self._audit_subagent(artifact, ctx.thread_id)
                    yield self._subagent_finished(artifact)
                else:
                    # 被拒 / 预算耗尽 / 子智能体异常：没有结构化产物，
                    # 但 started 已经发过，必须配对收口。
                    # `rejected` 与 `failed` 必须分开：前者是宿主
                    # 没让它跑（预算/审批），后者是跑了但没成。
                    yield SubAgentFinished(
                        agent_id=started.call_id,
                        status=(
                            "rejected" if finished.rejected else "failed"
                        ),
                        summary=_preview(
                            str(getattr(message, "content", ""))
                        ),
                    )

            for changed in self._file_changes(message):
                self._audit_file_change(changed, ctx.thread_id)
                yield changed

    def _on_verify(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        raw = update.get("verification") or {}
        # 新的验证结果让上一次的审查结论作废：审查总是紧跟在验证
        # 之后，所以"当前卡在哪一关"只需要看最新的一对。
        ctx.last_review = {}
        ctx.progress["last_review"] = {}
        if raw:
            ctx.last_verification = raw
            ctx.progress["last_verification"] = raw
            event = self._verification(raw)
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.VERIFY,
                    thread_id=ctx.thread_id,
                    # 没做验证时记 None（未验证），不记 True（通过）
                    ok=_verification_verdict(raw),
                    detail=truncate(
                        f"[{raw.get('kind', 'none')}] "
                        f"{raw.get('command', '')} → {raw.get('summary', '')}"
                    ),
                )
            )
            yield event

    def _on_review(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        raw = update.get("review") or {}
        if raw:
            ctx.last_review = raw
            ctx.progress["last_review"] = raw
            blocked = _review_blocked(raw)
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.REVIEW,
                    thread_id=ctx.thread_id,
                    ok=not blocked,
                    detail=truncate(
                        f"{raw.get('checked_files', 0)} 个改动文件 → "
                        f"{raw.get('summary', '')}"
                    ),
                )
            )
            yield self._review(raw)

    def _on_nudge(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> None:
        # 一步没产生任何改动被打回重做。**刻意不发事件**：
        # 它不是给用户看的结果，而是"这一步还没做完"的内部重试 ——
        # 前端时间线上会看到这一步继续在动。但审计必须留痕：
        # 事后要能回答"这次运行里有几步是被打回来的"。
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.NUDGE,
                thread_id=ctx.thread_id,
                detail=truncate(
                    f"第 {ctx.step_idx + 1} 步未产生改动，已要求重做"
                ),
            )
        )

    def _on_repair(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        attempt = int(update.get("retry", 0))
        limit = self.settings.max_repair_rounds
        # 摘要按**阻断来源**取：修的是谁就说什么。一味看
        # last_verification，在"审查阻断"时会带出验证的 ok 摘要。
        if _review_blocked(ctx.last_review):
            summary = str(ctx.last_review.get("summary", ""))
            issues = self._review(ctx.last_review).findings
        else:
            summary = str(ctx.last_verification.get("summary", ""))
            issues = self._verification(ctx.last_verification).issues
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.REPAIR,
                thread_id=ctx.thread_id,
                detail=truncate(f"第 {attempt}/{limit} 次修复（{summary}）"),
            )
        )
        yield RepairStarted(
            attempt=attempt,
            limit=limit,
            summary=summary,
            issues=issues,
        )

    def _on_respond(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> None:
        last = _last_message(update)
        ctx.input_tokens, ctx.output_tokens = _add_usage(
            last, ctx.input_tokens, ctx.output_tokens
        )
        ctx.progress["input_tokens"] = ctx.input_tokens
        ctx.progress["output_tokens"] = ctx.output_tokens
        ctx.answer = text_of(last)

    def _custom_events(
        self, ctx: _StreamCtx, data: Any
    ) -> Iterator[Event]:
        # Phase 4：dispatch 通过 get_stream_writer 实时推送 worker
        # 开始/结束；updates 分支只补齐没被流式发过的事件。
        payload_data = data if isinstance(data, dict) else {}
        kind = str(payload_data.get("type", ""))
        if kind == "worker_started":
            key = _worker_key(payload_data)
            seen = ctx.progress.setdefault("worker_started_keys", set())
            if key not in seen:
                seen.add(key)
                yield WorkerStarted(
                    worker_id=str(payload_data.get("worker_id", "")),
                    task_id=str(payload_data.get("task_id", "")),
                    role=str(payload_data.get("role", "")),
                    goal=str(payload_data.get("goal", "")),
                    attempt=int(payload_data.get("attempt", 1)),
                    queue_wait_ms=int(payload_data.get("queue_wait_ms") or 0),
                )
        elif kind == "worker_finished":
            result = payload_data.get("result") or {}
            key = _worker_key(result)
            seen = ctx.progress.setdefault("worker_finished_keys", set())
            if key not in seen:
                seen.add(key)
                yield from self._worker_events(
                    result, ctx.thread_id, ctx.progress
                )
        elif kind == "task_blocked":
            task_id = str(payload_data.get("task_id", ""))
            seen = ctx.progress.setdefault("task_blocked_keys", set())
            if task_id and task_id not in seen:
                seen.add(task_id)
                yield TaskBlocked(
                    task_id=task_id,
                    role=str(payload_data.get("role", "")),
                    reason=str(payload_data.get("reason", "")),
                )

    def _message_events(
        self, ctx: _StreamCtx, data: Any
    ) -> Iterator[Event]:
        chunk, meta = data
        node = meta.get("langgraph_node", "")
        if node not in STREAMING_NODES:
            return
        reasoning = reasoning_of(chunk)
        if reasoning:
            yield ReasoningToken(node=node, text=reasoning)
        text = text_of(chunk)
        if not text:
            return
        if node == "respond":
            ctx.streamed.append(text)
        yield AssistantToken(node=node, text=text)

    def _interrupt_events(
        self, suspension: Any, seen: set[str]
    ) -> Iterator[Event]:
        for item in suspension:
            key = str(getattr(item, "id", "") or "")
            if key:
                if key in seen:
                    continue
                seen.add(key)
            payload = getattr(item, "value", None) or {}
            if payload.get("steering") == "pause":
                yield SteeringApplied(
                    action="pause",
                    detail=str(payload.get("message", "")),
                )
            else:
                yield from self._interrupt_requests([item])

    @staticmethod
    def _interrupt_requests(suspension: Any) -> list[ApprovalRequested]:
        """把 approval_gate 挂起时抛出的 payload 翻译成审批事件。"""
        requests: list[ApprovalRequested] = []
        for item in suspension or ():
            payload = getattr(item, "value", None) or {}
            for raw in payload.get("requests", []) or []:
                requests.append(
                    ApprovalRequested(
                        request_id=str(raw.get("call_id", "")),
                        tool=str(raw.get("tool", "")),
                        command=str(raw.get("command", "")),
                        level=str(raw.get("level", "")),
                        level_int=int(raw.get("level_int", 0) or 0),
                        reason=str(raw.get("reason", "")),
                    )
                )
        return requests

    def _worker_events(
        self,
        result: dict[str, Any],
        thread_id: str,
        progress: dict[str, Any],
    ) -> Iterator[Event]:
        """一个 worker 结果 → WorkerFinished + 审计（custom/updates 共用）。

        只在结果首次被消费时调用；调用方用 `_worker_key` 去重。
        """
        status = str(result.get("status", ""))
        input_tokens = int(result.get("input_tokens") or 0)
        output_tokens = int(result.get("output_tokens") or 0)
        if input_tokens or output_tokens:
            progress["worker_input_tokens"] = (
                int(progress.get("worker_input_tokens", 0)) + input_tokens
            )
            progress["worker_output_tokens"] = (
                int(progress.get("worker_output_tokens", 0)) + output_tokens
            )
            progress["worker_tokens_reported"] = True

        yield WorkerFinished(
            worker_id=str(result.get("worker_id", "")),
            task_id=str(result.get("task_id", "")),
            role=str(result.get("role", "")),
            status=status,
            summary=str(result.get("summary", "")),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=int(result.get("duration_ms") or 0),
            attempt=int(result.get("attempt", 1)),
            merged=bool(result.get("merged")),
            merge_error=str(result.get("merge_error", "")),
            tool_calls=len(result.get("tool_calls") or []),
            queue_wait_ms=int(result.get("queue_wait_ms") or 0),
            llm_ms=int(result.get("llm_ms") or 0),
            tool_ms=int(result.get("tool_ms") or 0),
            timed_out=bool(result.get("timed_out")),
            artifact_id=str(result.get("artifact_id", "")),
            depth=int(result.get("depth") or 0),
            priority=int(result.get("priority") or 0),
            scope_violations=[
                str(item) for item in (result.get("scope_violations") or [])
            ],
        )
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.WORKER,
                thread_id=thread_id,
                task_id=str(result.get("task_id", "")),
                role=str(result.get("role", "")),
                worker_id=str(result.get("worker_id", "")),
                attempt=int(result.get("attempt", 1)),
                merged=bool(result.get("merged")),
                artifact_id=str(result.get("artifact_id") or "") or None,
                timed_out=bool(result.get("timed_out")),
                queue_wait_ms=int(result.get("queue_wait_ms") or 0) or None,
                llm_ms=int(result.get("llm_ms") or 0) or None,
                tool_ms=int(result.get("tool_ms") or 0) or None,
                depth=int(result.get("depth") or 0) or None,
                priority=int(result.get("priority") or 0) or None,
                scope_violations=[
                    str(item)
                    for item in (result.get("scope_violations") or [])
                ],
                ok=(
                    True
                    if status == "success"
                    else False
                    if status == "failed"
                    else None
                ),
                detail=truncate(
                    str(result.get("summary", ""))
                    + (
                        f" | error={result.get('error')}"
                        if result.get("error")
                        else ""
                    )
                    + (
                        f" | merge_error={result.get('merge_error')}"
                        if result.get("merge_error")
                        else ""
                    )
                    + (
                        " | snapshots="
                        + ",".join(
                            str(item)
                            for item in (result.get("snapshot_ids") or [])
                        )
                        if result.get("snapshot_ids")
                        else ""
                    )
                ),
                input_tokens=input_tokens or None,
                output_tokens=output_tokens or None,
                duration_ms=int(result.get("duration_ms") or 0),
            )
        )
        # worker 内部的工具调用逐条落账；否则子智能体改了哪些文件只存在于
        # 摘要里，审计无法追溯。
        for call in result.get("tool_calls") or []:
            call_name = str(call.get("name", ""))
            call_args = dict(call.get("args") or {})
            level = tool_level(call_name, call_args)
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.TOOL_CALL,
                    thread_id=thread_id,
                    call_id=str(call.get("call_id", "")),
                    tool=call_name,
                    args=sanitize_args(call_args),
                    level=level.label,
                    decision=audit_models.DECISION_AUTO,
                    ok=bool(call.get("ok")),
                    duration_ms=call.get("duration_ms"),
                    agent_id=str(result.get("worker_id", "")) or None,
                    task_id=str(result.get("task_id", "")) or None,
                )
            )

    def _tool_started(self, name: str, args: dict[str, Any], call_id: str = "") -> ToolCallStarted:
        # 这里重新判一次等级纯粹是为了给前端着色；真正的放行/拒绝在工具内部，
        # 不存在"UI 判定通过就执行"的路径。
        level = None
        if name == SHELL_TOOL_NAME:
            level = classify(
                str(args.get("command", "")),
                read_deny_globs=self.settings.read_deny_globs,
            ).level.label
        return ToolCallStarted(
            call_id=call_id,
            name=name,
            args=args,
            summary=_summarize_args(args),
            level=level,
        )

    def _tool_finished(self, message: Any) -> ToolCallFinished:
        name = str(getattr(message, "name", "") or "")
        call_id = str(getattr(message, "tool_call_id", "") or "")
        content = str(getattr(message, "content", ""))
        preview = _preview(content)
        artifact = parse_artifact(getattr(message, "artifact", None))

        if isinstance(artifact, ShellArtifact):
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=artifact.ok,
                rejected=artifact.rejected,
                decision=artifact.decision,
                exit_code=artifact.exit_code,
                duration_ms=artifact.duration_ms,
                level=artifact.level_label or None,
                preview=preview,
            )

        if isinstance(artifact, FileArtifact):
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=artifact.ok,
                rejected=artifact.rejected,
                decision=artifact.decision,
                duration_ms=artifact.duration_ms,
                level="文件工具",
                preview=preview,
            )

        if isinstance(artifact, InterruptedArtifact):
            # 副作用执行到一半中断：既不能记成功，也不能断言失败。
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=False,
                interrupted=True,
                decision=artifact.decision,
                duration_ms=artifact.duration_ms,
                preview=preview,
            )

        if isinstance(artifact, CallArtifact):
            # 非 shell / 非文件的工具（git_commit、deps_install、run_tests…）。
            # 被拒时也走这里，level 用工具自身的等级，而不是「文件工具」。
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=artifact.ok,
                rejected=artifact.rejected,
                skipped=artifact.skipped,
                decision=artifact.decision,
                duration_ms=artifact.duration_ms,
                level=artifact.level_label or None,
                preview=preview,
            )

        if isinstance(artifact, SubAgentArtifact):
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                # 只有 success 才算真正完成；partial 仍会把摘要给模型，
                # 但审计与事件都不该把它记成「通过」。
                ok=artifact.status == "success",
                rejected=False,
                decision=artifact.decision,
                duration_ms=artifact.duration_ms,
                level="探索子智能体",
                preview=preview,
            )

        # 未提供 artifact 的工具：只能从文本粗判成败。新工具都应带 artifact。
        return ToolCallFinished(
            call_id=call_id,
            name=name,
            ok=not content.startswith(_FAILURE_MARKERS),
            preview=preview,
        )

    @staticmethod
    def _subagent_finished(artifact: SubAgentArtifact) -> SubAgentFinished:
        return SubAgentFinished(
            agent_id=artifact.agent_id,
            status=artifact.status,
            summary=artifact.summary,
            relevant_files=list(artifact.relevant_files),
            findings=list(artifact.findings),
            input_tokens=artifact.input_tokens,
            output_tokens=artifact.output_tokens,
            duration_ms=artifact.duration_ms,
        )

    @staticmethod
    def _review(raw: dict[str, Any]) -> ReviewFinished:
        findings = [
            " ".join(
                f"[{f.get('severity', '')}] {f.get('location', '')} {f.get('message', '')}".split()
            )
            for f in (raw.get("findings") or [])
        ]
        return ReviewFinished(
            status=str(raw.get("status", "skipped")),
            blocked=_review_blocked(raw),
            summary=str(raw.get("summary", "")),
            findings=findings,
        )

    @staticmethod
    def _verification(raw: dict[str, Any]) -> Verification:
        issues = [
            " ".join(f"{i.get('location', '')} {i.get('message', '')}".split())
            for i in (raw.get("issues") or [])
        ]
        return Verification(
            status=str(raw.get("status", "skipped")),
            command=str(raw.get("command", "")),
            # 与审计同源：都用三态口径，两边的结论必须一致
            ok=_verification_verdict(raw),
            summary=str(raw.get("summary", "")),
            issues=issues,
            failed_tests=[str(t) for t in (raw.get("failed_tests") or [])],
        )

    @staticmethod
    def _file_changed(message: Any) -> FileChanged | None:
        artifact = parse_artifact(getattr(message, "artifact", None))
        if not isinstance(artifact, FileArtifact):
            return None
        if not artifact.ok or artifact.action not in MUTATING_FILE_ACTIONS:
            return None
        return FileChanged(
            path=artifact.path,
            action=artifact.action,
            added=artifact.added,
            removed=artifact.removed,
            diff=artifact.diff,
            snapshot_id=artifact.snapshot_id,
        )

    def _file_changes(self, message: Any) -> list[FileChanged]:
        """一个工具结果涉及的所有文件改动。

        单文件工具返回 FileArtifact；`file_edit_many` 返回带 `file_changes`
        的 CallArtifact —— 否则批量编辑只有一条 tool_call，前端看不到逐文件
        的 ✎ 行，审计里也没有逐文件的 file_change。
        """
        artifact = parse_artifact(getattr(message, "artifact", None))
        if isinstance(artifact, FileArtifact):
            changed = self._file_changed(message)
            return [changed] if changed is not None else []
        if isinstance(artifact, CallArtifact) and artifact.ok:
            changes: list[FileChanged] = []
            for raw in artifact.file_changes:
                if not isinstance(raw, dict):
                    continue
                action = str(raw.get("action", ""))
                path = str(raw.get("path", ""))
                if not path or action not in MUTATING_FILE_ACTIONS:
                    continue
                changes.append(
                    FileChanged(
                        path=path,
                        action=action,
                        added=int(raw.get("added", 0) or 0),
                        removed=int(raw.get("removed", 0) or 0),
                        diff=str(raw.get("diff", "") or ""),
                        snapshot_id=str(raw.get("snapshot_id") or "") or None,
                    )
                )
            return changes
        return []
