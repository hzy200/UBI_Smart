"""事件流、收尾与成本汇总。

`_stream` 是 `run/resume/steer` 共用的驱动循环；这里只负责把图产出的原始
chunk 交给 event_map 翻译，并在结束时发收尾/错误事件。编排与资源生命周期
仍在 core。
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from coding_agent.audit import AuditRecord
from coding_agent.audit import models as audit_models
from coding_agent.audit.logger import now_iso, truncate
from coding_agent.events import (
    CostReport,
    Event,
    RunFailed,
    RunFinished,
    RunStarted,
    ToolCallStarted,
)
from coding_agent.graph.build import estimate_recursion_limit
from coding_agent.llm.billing import fetch_billing
from coding_agent.llm.ratelimit import GLOBAL_LIMITER
from coding_agent.protocol.messages import unanswered_tool_calls

from .common import _swallow_audit


@dataclass
class _StreamCtx:
    """一次 `_stream` 内跨节点共享的可变状态。

    `progress` 按 thread 留存（挂起/恢复是两次 `_stream`）；其余字段是本次流
    的游标，事件处理器直接读写它们，避免把十几个 `nonlocal` 散在函数里。
    """

    # 用 Any 而不是 AgentRuntime：stream 不能反向 import core，否则模块级依赖成环。
    runtime: Any
    thread_id: str
    progress: dict[str, Any]
    pending: dict[str, ToolCallStarted]
    streamed: list[str]
    plan: list[str]
    plan_kinds: list[str]
    step_idx: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    run_epoch: str = ""
    answer: str = ""
    last_verification: dict[str, Any] = field(default_factory=dict)
    last_review: dict[str, Any] = field(default_factory=dict)
    step_open: bool = False

@dataclass
class _CostSummary:
    """收尾时的成本、账单与告警。"""

    usage: dict[str, Any]
    budget: float
    ratio: float
    alerts: list[str]
    billed_usd: float
    billing_delta: float
    billing_error: str
    billing_providers: dict[str, dict[str, float]]


class StreamMixin:
    """`AgentRuntime` 的流式输出职责。"""

    async def _cost_summary(
        self, thread_id: str, run_started_at: float
    ) -> _CostSummary:
        settings = self.settings
        usage = GLOBAL_LIMITER.run_usage(thread_id)
        budget = float(settings.run_cost_budget_usd or 0.0)
        ratio = (usage["cost_usd"] / budget) if budget > 0 else 0.0
        alerts: list[str] = []
        if budget > 0:
            if ratio >= 1.0:
                alerts.append("已超出本次运行成本预算")
            elif ratio >= float(settings.cost_alert_ratio):
                alerts.append("接近本次运行成本预算")

        billed_usd = 0.0
        billing_error = ""
        billing_providers: dict[str, dict[str, float]] = {}
        if settings.billing_enabled:
            started = float(usage.get("started_at") or run_started_at)
            try:
                billing = await asyncio.to_thread(
                    fetch_billing, settings, started, time.time()
                )
                for item in billing:
                    billed_usd += float(item.cost_usd)
                    billing_providers[item.provider] = {
                        "cost_usd": float(item.cost_usd),
                        "input_tokens": float(item.input_tokens),
                        "output_tokens": float(item.output_tokens),
                    }
                    if item.error:
                        billing_error = (
                            f"{item.provider}: {item.error}"
                            if not billing_error
                            else f"{billing_error}; {item.provider}: {item.error}"
                        )
            except Exception as exc:  # noqa: BLE001 - 对账失败不拖垮 run
                billing_error = f"{type(exc).__name__}: {exc}"
        billing_delta = billed_usd - float(usage["cost_usd"])
        if billed_usd > 0 and billing_delta > max(
            0.01, float(usage["cost_usd"]) * 0.2
        ):
            alerts.append("provider 账单金额明显高于本地估算")
        return _CostSummary(
            usage=usage,
            budget=budget,
            ratio=ratio,
            alerts=alerts,
            billed_usd=billed_usd,
            billing_delta=billing_delta,
            billing_error=billing_error,
            billing_providers=billing_providers,
        )

    async def _finish_events(
        self, ctx: _StreamCtx, run_started_at: float
    ) -> AsyncIterator[Event]:
        final = ctx.answer or "".join(ctx.streamed)
        summary = await self._cost_summary(ctx.thread_id, run_started_at)
        usage = summary.usage
        retry_count = int(ctx.progress.get("transient_retries", 0))
        run_detail = final
        if retry_count:
            run_detail += f"\n（本次运行因瞬时错误重试 {retry_count} 次）"
        concurrency = self.concurrency.snapshot()
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.CONCURRENCY,
                thread_id=ctx.thread_id,
                detail=truncate(json.dumps(concurrency, ensure_ascii=False)),
            )
        )
        worker_in = (
            int(ctx.progress.get("worker_input_tokens", 0))
            if ctx.progress.get("worker_tokens_reported")
            else None
        )
        worker_out = (
            int(ctx.progress.get("worker_output_tokens", 0))
            if ctx.progress.get("worker_tokens_reported")
            else None
        )
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.RUN_END,
                thread_id=ctx.thread_id,
                run_epoch=ctx.run_epoch,
                provider=self.provider_name,
                model=self.model_override,
                detail=truncate(run_detail),
                # provider 未回报用量时保持 None（exclude_none 会省略该字段）
                input_tokens=ctx.input_tokens or None,
                output_tokens=ctx.output_tokens or None,
                worker_input_tokens=worker_in,
                worker_output_tokens=worker_out,
                cost_usd=usage["cost_usd"] or None,
                billed_usd=summary.billed_usd or None,
                cache_hit_tokens=int(usage.get("cache_hit_tokens", 0)) or None,
                cache_miss_tokens=int(usage.get("cache_miss_tokens", 0)) or None,
            )
        )
        # 收尾兜底：正常路径下 act 已经收口（空操作），但若某条路径让 act 没能
        # 跑（计划被砍、异常恢复等），这里补上，保证 StepStarted 一定有配对
        for event in self._close_step(ctx, cancelled=True):
            yield event
        yield CostReport(
            run_id=ctx.thread_id,
            input_tokens=int(usage["input_tokens"]),
            output_tokens=int(usage["output_tokens"]),
            cost_usd=float(usage["cost_usd"]),
            budget_usd=summary.budget,
            ratio=summary.ratio,
            billed_usd=summary.billed_usd,
            billing_delta_usd=summary.billing_delta,
            billing_error=summary.billing_error,
            alerts=summary.alerts,
            cache_hit_tokens=int(usage.get("cache_hit_tokens", 0)),
            cache_miss_tokens=int(usage.get("cache_miss_tokens", 0)),
            billing_providers=summary.billing_providers,
            providers={
                str(key): {
                    "input_tokens": float(value.get("input_tokens", 0) or 0),
                    "output_tokens": float(value.get("output_tokens", 0) or 0),
                    "cost_usd": float(value.get("cost_usd", 0) or 0),
                    "cache_hit_tokens": float(
                        value.get("cache_hit_tokens", 0) or 0
                    ),
                    "cache_miss_tokens": float(
                        value.get("cache_miss_tokens", 0) or 0
                    ),
                }
                for key, value in (usage.get("providers") or {}).items()
            },
        )
        await self._prune_checkpoints(ctx.thread_id)
        self._forget_thread(ctx.thread_id)
        yield RunFinished(
            thread_id=ctx.thread_id,
            answer=final,
            input_tokens=ctx.input_tokens or None,
            output_tokens=ctx.output_tokens or None,
            worker_input_tokens=worker_in,
            worker_output_tokens=worker_out,
            concurrency=concurrency,
        )

    async def _error_events(
        self,
        ctx: _StreamCtx,
        exc: Exception,
        *,
        deadline_at: float,
        deadline_seconds: int,
    ) -> AsyncIterator[Event]:
        deadline_hit = (
            isinstance(exc, TimeoutError)
            and deadline_at > 0
            and time.monotonic() >= deadline_at - 1.0
        )
        detail = (
            f"运行超过总时限 {deadline_seconds}s，已停止。"
            "已完成的状态在 checkpoint 里，可用同一 thread 继续。"
            if deadline_hit
            else f"{type(exc).__name__}: {exc}"
        )
        # 尽力记一笔，但**不能让它顶掉真正的错误**：审计本身坏了的时候
        # （比如磁盘满、目录被占），这里再抛一次就把原因盖掉了。
        try:
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.RUN_ERROR,
                    thread_id=ctx.thread_id,
                    run_epoch=ctx.run_epoch,
                    provider=self.provider_name,
                    model=self.model_override,
                    detail=detail,
                    input_tokens=ctx.input_tokens or None,
                    output_tokens=ctx.output_tokens or None,
                    worker_input_tokens=(
                        int(ctx.progress.get("worker_input_tokens", 0))
                        if ctx.progress.get("worker_tokens_reported")
                        else None
                    ),
                    worker_output_tokens=(
                        int(ctx.progress.get("worker_output_tokens", 0))
                        if ctx.progress.get("worker_tokens_reported")
                        else None
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 已在错误路径上，不再二次抛出
            _swallow_audit("audit_error_path", exc)
        # 异常可能发生在任何节点上：此刻还开着的步骤再也不会被 act 收口，
        # 补发一个，免得前端把它显示成「仍在运行」
        for event in self._close_step(ctx, cancelled=True):
            yield event
        worker_in = (
            int(ctx.progress.get("worker_input_tokens", 0))
            if ctx.progress.get("worker_tokens_reported")
            else None
        )
        worker_out = (
            int(ctx.progress.get("worker_output_tokens", 0))
            if ctx.progress.get("worker_tokens_reported")
            else None
        )
        await self._prune_checkpoints(ctx.thread_id)
        self._forget_thread(ctx.thread_id)
        yield RunFailed(
            message=detail,
            input_tokens=ctx.input_tokens or None,
            output_tokens=ctx.output_tokens or None,
            worker_input_tokens=worker_in,
            worker_output_tokens=worker_out,
        )

    async def _stream(
        self, payload: Any, *, thread_id: str, fresh_run: bool, prompt: str = ""
    ) -> AsyncIterator[Event]:
        settings = self.settings
        run_started_at = time.time()
        # 同一 thread 会被多轮 run 复用：全新 run 必须清零，resume 必须保留
        # 挂起前那一段的用量。
        GLOBAL_LIMITER.start_run(thread_id, reset=fresh_run)
        config = {
            "configurable": {"thread_id": thread_id},
            # 按图拓扑推导，别再把系数算歪（见 estimate_recursion_limit）
            "recursion_limit": estimate_recursion_limit(
                settings.max_plan_steps,
                settings.max_tool_rounds,
                settings.max_repair_rounds,
                settings.max_replans,
                orchestrator_enabled=settings.orchestrator_enabled,
                orchestrator_max_tasks=settings.orchestrator_max_tasks,
                orchestrator_checkpoint_every=(
                    settings.orchestrator_checkpoint_every
                ),
            ),
            # 这些会随 trace 一起上报，LangSmith 里可按会话/工作区/权限筛选
            "run_name": f"agent:{thread_id}",
            "tags": ["coding-agent", f"mode:{self.policy.approval_mode}"],
            "metadata": self._trace_metadata(thread_id),
        }

        # 跨 run/resume 的状态；每次全新 run 清空，resume 时保留（见 __init__ 说明）
        pending = self._pending_calls.setdefault(thread_id, {})
        progress = self._progress.setdefault(thread_id, {})
        if fresh_run:
            pending.clear()
            progress.clear()
            self._resume_counts[thread_id] = 0
        else:
            # 进程重启后 `_pending_calls` 是空的：从 checkpoint 的尾部
            # tool_calls 恢复调用参数，否则恢复后的审计会丢掉 L2/L3 的参数。
            # 同进程 resume 时 pending 已有记录，这里不会重复补发。
            prior = await self._prior_state(thread_id)
            for call in unanswered_tool_calls(prior.get("messages") or []):
                call_id = str(call.get("id", ""))
                if not call_id or call_id in pending:
                    continue
                started = self._tool_started(
                    str(call.get("name", "")),
                    dict(call.get("args") or {}),
                    call_id,
                )
                pending[call_id] = started
                yield started
        # 模型回报的用量、计划、验证/审查结果与 step 配对状态都存 progress：
        # 挂起与恢复是两次 `_stream`，存局部会把挂起前的那一段整个丢掉。
        # provider 未提供用量时保持 0，收尾落成 None（0 与「未回报」必须能区分）。
        if fresh_run:
            # 计量幂等键：resume 复用同一 epoch，新的用户消息生成新 epoch。
            progress["run_epoch"] = uuid4().hex
        ctx = _StreamCtx(
            runtime=self,
            thread_id=thread_id,
            progress=progress,
            pending=pending,
            streamed=[],
            plan=progress.setdefault("plan", []),
            plan_kinds=progress.setdefault("plan_kinds", []),
            step_idx=int(progress.get("step_idx", 0)),
            input_tokens=int(progress.get("input_tokens", 0)),
            output_tokens=int(progress.get("output_tokens", 0)),
            run_epoch=str(progress.get("run_epoch") or ""),
            last_verification=progress.setdefault("last_verification", {}),
            last_review=progress.setdefault("last_review", {}),
            step_open=bool(progress.get("step_open", False)),
        )
        interrupted = False

        # 总墙钟时限：跨 resume 累计，不能用“每次 _stream 重新计时”。
        deadline_seconds = int(settings.run_deadline_s or 0)
        if fresh_run:
            progress["deadline_at"] = (
                time.monotonic() + deadline_seconds
                if deadline_seconds > 0
                else 0.0
            )
        deadline_at = float(progress.get("deadline_at") or 0.0)

        def _remaining_seconds() -> float | None:
            if deadline_at <= 0:
                return None
            return max(0.0, deadline_at - time.monotonic())

        async def _timed_chunks(graph: Any):
            """把图的 astream 包进总时限；超时抛 TimeoutError。"""
            async with asyncio.timeout(_remaining_seconds()):
                async for item in graph.astream(
                    payload,
                    config,
                    stream_mode=["messages", "updates", "custom"],
                    # 独立子图的内层节点更新/自定义事件只有打开它才可见。
                    subgraphs=True,
                ):
                    yield item

        try:
            if fresh_run:
                self._audit(
                    AuditRecord(
                        ts=now_iso(),
                        kind=audit_models.RUN_START,
                        thread_id=thread_id,
                        run_epoch=ctx.run_epoch,
                        provider=self.provider_name,
                        model=self.model_override,
                        workspace=self.workspace,
                        detail=truncate(prompt),
                    )
                )
                # 事件流起点。所有前端都能据此复位本轮状态（TUI 就靠它重置
                # 「最终答复」标记与流式缓冲）。只有全新 run 算新的一轮，
                # resume 不重复发 —— 那是同一次运行的延续。
                yield RunStarted(thread_id=thread_id)

            graph = await self._ensure_graph()
            # 只在一个 stream 调用内去重：子图中断会同时在带 namespace 的
            # 更新和父图更新里出现一次，但跨 resume 的重复暂停不能被吞掉。
            seen_interrupts: set[str] = set()
            async for chunk in _timed_chunks(graph):
                if len(chunk) == 3:
                    _namespace, mode, data = chunk
                else:
                    mode, data = chunk
                if mode == "custom":
                    for event in self._custom_events(ctx, data):
                        yield event
                    continue

                if mode == "messages":
                    for event in self._message_events(ctx, data):
                        yield event
                    continue

                suspension = data.get("__interrupt__")
                if suspension:
                    interrupted = True
                    for event in self._interrupt_events(
                        suspension, seen_interrupts
                    ):
                        yield event
                    continue

                for node, update in data.items():
                    if not isinstance(update, dict):
                        continue
                    for event in self._update_events(ctx, node, update):
                        yield event


        except Exception as exc:  # noqa: BLE001 - 编排层异常也要以事件形式报给前端
            async for event in self._error_events(
                ctx,
                exc,
                deadline_at=deadline_at,
                deadline_seconds=deadline_seconds,
            ):
                yield event
            return

        if interrupted:
            # 图已挂起：不发 RunFinished，让前端明确知道要 resume 而不是收工
            return

        async for event in self._finish_events(ctx, run_started_at):
            yield event
