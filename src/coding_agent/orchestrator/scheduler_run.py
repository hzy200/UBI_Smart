"""scheduler 的运行态：把原闭包拆成可测试的 SchedulerRun 类。

行为与旧 `make_scheduler_node` 内部实现保持一致：ready 堆、in-flight、
限流退避、预算、合并队列、重试与动态提议都不改语义。
"""

from __future__ import annotations

import asyncio
import heapq
import time
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from coding_agent.config import Settings
from coding_agent.llm.ratelimit import GLOBAL_LIMITER
from coding_agent.orchestrator.contracts import WorkerResult
from coding_agent.orchestrator.graph import mark_blocked, validate_proposals
from coding_agent.orchestrator.merge_journal import MergeJournal
from coding_agent.orchestrator.merge_queue import MergeQueue
from coding_agent.orchestrator.scheduler_plan import (
    deps_met,
    fallback_result,
    retry_context,
    task_depth,
    wire_implement_dependents,
)
from coding_agent.orchestrator.scheduling import (
    RateLimitState,
    TokenBudget,
    aged_priority,
    ewma_usage,
    task_priority,
    task_token_reserve,
)
from coding_agent.orchestrator.workers import WorkerRunner
from coding_agent.platform.concurrency import (
    AsyncLeasePool,
    ConcurrencyGovernor,
    ResourceTimeout,
)
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.session.steering import SteeringController


class SchedulerRun:
    def __init__(
        self,
        state: dict[str, Any],
        config: RunnableConfig,
        *,
        worker_runner: WorkerRunner,
        settings: Settings,
        sandbox: WslSandbox | None = None,
        root: str = "",
        steering: SteeringController | None = None,
        governor: ConcurrencyGovernor | None = None,
    ) -> None:
        self.state = state
        self.config = config
        self.worker_runner = worker_runner
        self.settings = settings
        self.sandbox = sandbox
        self.root = root
        self.steering = steering
        self.governor = governor

        self.thread_id = str(
            (((config or {}).get("configurable")) or {}).get(
                "thread_id", ""
            )
        )
        self.request = str(state.get("request") or "")
        self.session_brief = str(state.get("session_brief") or "")
        recovery_notice = str(
            state.get("recovery_notice") or ""
        ).strip()
        if recovery_notice:
            self.session_brief = (
                f"{recovery_notice}\n\n{self.session_brief}"
                if self.session_brief
                else recovery_notice
            )
        self.cwd = str(state.get("cwd", ""))

        self.tasks = [dict(task) for task in (state.get("tasks") or [])]
        self.status = dict(state.get("task_status") or {})
        self.attempts = dict(state.get("task_attempts") or {})
        self.results = list(state.get("worker_results") or [])
        self.round_no = int(state.get("orchestrator_round", 0))
        self.seen = int(state.get("task_extensions_seen", 0))
        self.applied: list[dict[str, Any]] = []
        self.extension_errors: list[str] = []
        self.dirty = bool(state.get("dirty"))
        self.run_dirty = bool(state.get("run_dirty"))
        self.pending_verification = bool(
            state.get("pending_verification")
        )
        self.scheduler_deadline = float(
            state.get("scheduler_deadline") or 0.0
        )
        if self.scheduler_deadline <= 0:
            seconds = int(settings.orchestrator_run_deadline_s or 0)
            self.scheduler_deadline = (
                time.monotonic() + seconds if seconds > 0 else 0.0
            )

        wire_implement_dependents(self.tasks)
        self.by_id = {str(task["id"]): task for task in self.tasks}

        for task in self.tasks:
            task_id = str(task["id"])
            if (
                self.status.get(task_id) == "failed"
                and self.attempts.get(task_id, 0)
                <= settings.orchestrator_task_retries
            ):
                self.status[task_id] = "pending"
        self.status = mark_blocked(self.tasks, self.status)

        self.worker_pool = (
            governor.worker
            if governor is not None
            else AsyncLeasePool(
                "worker",
                max(
                    1,
                    int(settings.orchestrator_max_concurrent_workers),
                ),
            )
        )
        self.max_limit = max(1, int(self.worker_pool.limit))
        self.worker_timeout = float(
            getattr(
                settings, "concurrency_worker_timeout_s", 60.0
            )
            or 60.0
        )
        self.rate_limit = RateLimitState(
            max_limit=self.max_limit,
            backoff_base=settings.orchestrator_rate_limit_backoff_s,
        )
        self.rate_limit.restore(state.get("scheduler_rate_limit"))
        self.budget = TokenBudget(
            settings.orchestrator_total_tokens, self.results
        )
        self.merge_journal = MergeJournal(sandbox, root)
        self.merge_queue = MergeQueue(self.merge_journal)
        self.token_budget_exhausted = False
        self.ready: list[tuple[int, int, str]] = []
        self.ready_at: dict[str, float] = {}
        self.queued: set[str] = set()
        self.seq = 0
        self.in_flight: dict[str, asyncio.Task[WorkerResult]] = {}
        self.future_task: dict[asyncio.Task[WorkerResult], str] = {}
        self.queued_at: dict[str, float] = {}
        self.started_keys: set[str] = set()
        self.stop = False
        self.completed_since_boundary = 0
        self.checkpoint_every = max(
            1, int(settings.orchestrator_checkpoint_every or 1)
        )
        self.max_pending_merges = max(
            1, int(settings.orchestrator_max_pending_merges or 1)
        )

        for result in self.results[self.seen :]:
            self.consume_proposals(result)
        self.seen = len(self.results)

    # ------------------------------------------------------------------
    # 基础
    # ------------------------------------------------------------------
    def cancelled(self) -> bool:
        return bool(
            self.steering is not None
            and self.steering.cancel_requested(self.thread_id)
        )

    def emit(self, payload: dict[str, Any]) -> None:
        try:
            writer = get_stream_writer()
        except Exception:  # noqa: BLE001 - 单测直接调用节点时没有 writer
            return
        try:
            writer(payload)
        except Exception:  # noqa: BLE001 - 前端断开不拖垮调度
            pass

    def consume_proposals(self, result: dict[str, Any]) -> None:
        if result.get("status") not in {"success", "partial"}:
            return
        proposals = (result.get("proposed_tasks") or [])[:2]
        if not proposals:
            return
        new_tasks, errors = validate_proposals(
            proposals,
            existing=self.tasks,
            max_total=self.settings.orchestrator_max_tasks,
        )
        self.extension_errors.extend(errors)
        for task in new_tasks:
            task["depth"] = task_depth(
                self.tasks, str(task["id"])
            )
            self.tasks.append(task)
            self.by_id[str(task["id"])] = task
            self.status[str(task["id"])] = "pending"
            self.applied.append(task)

    # ------------------------------------------------------------------
    # ready / 预算
    # ------------------------------------------------------------------
    def refresh_ready(self) -> None:
        for task in self.tasks:
            task_id = str(task["id"])
            if self.status.get(task_id, "pending") != "pending":
                continue
            if task_id in self.in_flight or task_id in self.queued:
                continue
            if not deps_met(task, self.status):
                continue
            now = time.monotonic()
            self.ready_at.setdefault(task_id, now)
            heapq.heappush(
                self.ready, (-task_priority(task), self.seq, task_id)
            )
            self.seq += 1
            self.queued.add(task_id)

    def age_ready(self, now: float) -> None:
        if not self.ready:
            return
        rebuilt: list[tuple[int, int, str]] = []
        for _neg, order, task_id in self.ready:
            task = self.by_id.get(task_id)
            if task is None:
                continue
            waited = now - self.ready_at.get(task_id, now)
            rebuilt.append(
                (
                    -aged_priority(task_priority(task), waited),
                    order,
                    task_id,
                )
            )
        self.ready[:] = rebuilt
        heapq.heapify(self.ready)

    def estimate_tokens(self, task: dict[str, Any]) -> int:
        explicit = task.get("max_tokens")
        if explicit is not None:
            try:
                value = int(explicit)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                return value
        observed = ewma_usage(self.results, str(task.get("role", "")))
        if observed > 0:
            estimate = int(observed * 1.2)
        else:
            estimate = task_token_reserve(task)
        return max(500, min(estimate, 16_000))

    def implement_backpressure(self) -> bool:
        active = sum(
            1
            for task_id in self.in_flight
            if str(self.by_id.get(task_id, {}).get("role", ""))
            == "implement"
        )
        return (
            len(self.merge_queue.pending) + active
            >= self.max_pending_merges
        )

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------
    async def run_one(self, task: dict[str, Any]) -> WorkerResult:
        task_id = str(task["id"])
        role = str(task.get("role", "explore"))
        worker_id = f"{role}:{task_id}"
        attempt = self.attempts.get(task_id, 1)
        started_wait = self.queued_at.get(
            task_id, time.monotonic()
        )
        try:
            lease = await self.worker_pool.acquire(
                priority=task_priority(task),
                timeout=self.worker_timeout,
            )
        except ResourceTimeout as exc:
            result = WorkerResult(
                task_id=task_id,
                role=role,
                worker_id=worker_id,
                goal=str(task.get("goal", "")),
                status="failed",
                summary=f"等待 worker 槽位超时：{exc}",
                error=str(exc),
            )
            result.attempt = attempt
            result.queue_wait_ms = 0
            result.priority = task_priority(task)
            result.depth = int(task.get("depth", 0))
            return result
        async with lease:
            queue_wait_ms = int(
                (time.monotonic() - started_wait) * 1000
            )
            self.emit(
                {
                    "type": "worker_started",
                    "worker_id": worker_id,
                    "task_id": task_id,
                    "role": role,
                    "goal": str(task.get("goal", "")),
                    "attempt": attempt,
                    "queue_wait_ms": queue_wait_ms,
                }
            )
            self.started_keys.add(f"{worker_id}:{attempt}")
            try:
                result = await self.worker_runner(
                    task,
                    parent_request=self.request,
                    session_brief=self.session_brief,
                    cwd=self.cwd,
                    config=self.config,
                    cancel=self.cancelled,
                )
            except Exception as exc:  # noqa: BLE001
                result = WorkerResult(
                    task_id=task_id,
                    role=role,
                    worker_id=worker_id,
                    goal=str(task.get("goal", "")),
                    status="failed",
                    summary=(
                        f"worker 异常：{type(exc).__name__}: {exc}"
                    ),
                    error=str(exc),
                )
            result.attempt = attempt
            result.queue_wait_ms = queue_wait_ms
            result.priority = task_priority(task)
            result.depth = int(task.get("depth", 0))
            return result

    async def abandon_inflight(
        self, *, reason: str, timed_out: bool
    ) -> None:
        for future in list(self.in_flight.values()):
            future.cancel()
        if self.in_flight:
            await asyncio.gather(
                *self.in_flight.values(), return_exceptions=True
            )
        for task_id in list(self.in_flight):
            self.budget.release(task_id)
            task = self.by_id.get(task_id, {})
            result = WorkerResult(
                task_id=task_id,
                role=str(task.get("role", "explore")),
                worker_id=f"{task.get('role', 'explore')}:{task_id}",
                goal=str(task.get("goal", "")),
                status="cancelled" if not timed_out else "failed",
                summary=reason,
                timed_out=timed_out,
            )
            result.attempt = self.attempts.get(task_id, 1)
            record = result.model_dump()
            self.results.append(record)
            self.status[task_id] = str(record["status"])
            key = f"{result.worker_id}:{result.attempt}"
            if key in self.started_keys:
                self.emit(
                    {"type": "worker_finished", "result": record}
                )
        self.in_flight.clear()

    def finalize(self, record: dict[str, Any]) -> None:
        task_id = str(record.get("task_id", ""))
        self.emit({"type": "worker_finished", "result": record})
        attempt = int(record.get("attempt", 1))
        if (
            record.get("status") == "failed"
            and attempt <= self.settings.orchestrator_task_retries
        ):
            self.status[task_id] = "pending"
            task = self.by_id.get(task_id)
            if task is not None:
                task["retry_context"] = retry_context(record)
        else:
            self.status[task_id] = str(
                record.get("status", "failed")
            )

        delay = self.rate_limit.on_result(record)
        if delay is not None:
            GLOBAL_LIMITER.block_all(delay)
            self.emit(
                {
                    "type": "worker_throttled",
                    "task_id": task_id,
                    "delay_s": delay,
                    "current_limit": self.rate_limit.current_limit,
                }
            )

        self.consume_proposals(record)
        self.seen = len(self.results)
        self.status.update(mark_blocked(self.tasks, self.status))

    def implement_active(self) -> bool:
        return self.merge_queue.has_active_implement(
            in_flight=set(self.in_flight),
            status=self.status,
            by_id=self.by_id,
        )

    async def flush_merges(self) -> None:
        merged_any = await self.merge_queue.flush(
            sandbox=self.sandbox,
            root=self.root,
            allow_overlap=self.settings.orchestrator_allow_overlap,
            finalize=self.finalize,
        )
        if merged_any:
            self.dirty = True
            self.run_dirty = True
            self.pending_verification = True

    def discard_merges(self, reason: str) -> None:
        self.merge_queue.discard(reason, self.finalize)

    async def abort_pending(
        self,
        *,
        pending_status: str,
        discard_reason: str,
        abandon_reason: str,
        timed_out: bool,
    ) -> None:
        for task in self.tasks:
            if (
                self.status.get(str(task["id"]), "pending")
                == "pending"
            ):
                self.status[str(task["id"])] = pending_status
        self.discard_merges(discard_reason)
        await self.abandon_inflight(
            reason=abandon_reason, timed_out=timed_out
        )

    def dispatch_ready(self, now: float) -> None:
        while (
            self.ready
            and not self.stop
            and len(self.in_flight) < self.rate_limit.current_limit
            and self.rate_limit.can_launch(now)
        ):
            chosen: (
                tuple[tuple[int, int, str], dict[str, Any], int]
                | None
            ) = None
            deferred: list[tuple[int, int, str]] = []
            while self.ready:
                entry = heapq.heappop(self.ready)
                task_id = entry[2]
                if (
                    self.status.get(task_id, "pending") != "pending"
                    or task_id in self.in_flight
                ):
                    self.queued.discard(task_id)
                    self.ready_at.pop(task_id, None)
                    continue
                task = self.by_id.get(task_id)
                if task is None:
                    self.queued.discard(task_id)
                    self.ready_at.pop(task_id, None)
                    continue
                if (
                    str(task.get("role", "")) == "implement"
                    and self.implement_backpressure()
                ):
                    deferred.append(entry)
                    continue
                reserve = self.estimate_tokens(task)
                if self.budget.can_reserve(reserve):
                    self.queued.discard(task_id)
                    self.ready_at.pop(task_id, None)
                    chosen = (entry, task, reserve)
                    break
                deferred.append(entry)
            for entry in deferred:
                heapq.heappush(self.ready, entry)

            if chosen is None:
                if self.in_flight or self.implement_backpressure():
                    return
                for pending_task in self.tasks:
                    pending_id = str(pending_task["id"])
                    if (
                        self.status.get(pending_id, "pending")
                        == "pending"
                    ):
                        self.status[pending_id] = "blocked"
                self.ready.clear()
                self.queued.clear()
                self.ready_at.clear()
                self.token_budget_exhausted = True
                return

            entry, task, reserve = chosen
            task_id = str(task["id"])
            if not self.budget.reserve(task_id, reserve):
                heapq.heappush(self.ready, entry)
                self.queued.add(task_id)
                return
            self.attempts[task_id] = (
                self.attempts.get(task_id, 0) + 1
            )
            self.queued_at[task_id] = time.monotonic()
            future = asyncio.create_task(self.run_one(task))
            self.in_flight[task_id] = future
            self.future_task[future] = task_id

    def drain_completed(
        self, done: set[asyncio.Task[WorkerResult]]
    ) -> None:
        for future in done:
            task_id = self.future_task.pop(future)
            self.in_flight.pop(task_id, None)
            try:
                result = future.result()
            except asyncio.CancelledError:
                result = fallback_result(
                    self.by_id,
                    task_id,
                    "cancelled",
                    "调度器取消或 deadline 到期",
                    timed_out=True,
                )
            except Exception as exc:  # noqa: BLE001
                result = fallback_result(
                    self.by_id,
                    task_id,
                    "failed",
                    f"worker 异常：{type(exc).__name__}: {exc}",
                    error=str(exc),
                )
            result.attempt = self.attempts.get(task_id, 1)
            record = result.model_dump()
            self.budget.settle(task_id, record)
            if self.cancelled() and record.get("role") == "implement":
                if record.get("status") in {"success", "partial"}:
                    record["status"] = "cancelled"
                    record["merged"] = False
                    record["merge_error"] = "用户取消，未合并"
            self.results.append(record)

            if (
                record.get("role") == "implement"
                and self.sandbox is not None
                and self.root
                and not self.cancelled()
                and record.get("status")
                not in {"cancelled", "failed"}
            ):
                self.status[str(record.get("task_id", ""))] = (
                    "merging"
                )
                self.merge_queue.add(record)
            else:
                self.finalize(record)
            self.completed_since_boundary += 1
            if (
                self.completed_since_boundary
                >= self.checkpoint_every
            ):
                self.stop = True

    # ------------------------------------------------------------------
    # 主循环与结果
    # ------------------------------------------------------------------
    async def run(self) -> dict[str, Any]:
        if not self.tasks:
            return self._no_tasks_result()
        # 上次运行被 kill 时 worktree 清理没机会执行；此刻还没有 implement
        # 在飞（`prune_orphan_worktrees` 会 rm -rf 整个目录），可以安全清残留。
        if (
            self.settings.orchestrator_write_enabled
            and self.sandbox is not None
            and self.root
        ):
            from coding_agent.orchestrator.implement import (
                prune_orphan_worktrees,
            )

            await asyncio.to_thread(
                prune_orphan_worktrees, self.sandbox, self.root
            )
        if self.state.get("steering_cancelled"):
            for task in self.tasks:
                if (
                    self.status.get(str(task["id"]), "pending")
                    == "pending"
                ):
                    self.status[str(task["id"])] = "cancelled"
            return self._cancelled_result()

        while True:
            if self.cancelled():
                await self.abort_pending(
                    pending_status="cancelled",
                    discard_reason="用户取消，未合并",
                    abandon_reason="已按用户要求取消",
                    timed_out=False,
                )
                break

            if (
                self.scheduler_deadline
                and time.monotonic() >= self.scheduler_deadline
            ):
                await self.abort_pending(
                    pending_status="blocked",
                    discard_reason="编排 deadline 到期，未合并",
                    abandon_reason="编排 deadline 到期",
                    timed_out=True,
                )
                break

            self.refresh_ready()
            now = time.monotonic()
            self.rate_limit.note_global_wait(
                GLOBAL_LIMITER.global_remaining(), now=now
            )
            self.age_ready(now)
            self.dispatch_ready(now)

            if not self.in_flight:
                now = time.monotonic()
                remaining = self.rate_limit.wait_remaining(now)
                if remaining > 0:
                    await asyncio.sleep(min(remaining, 1.0))
                    continue
                break

            timeout = None
            timeouts: list[float] = []
            if self.scheduler_deadline:
                timeouts.append(
                    max(
                        0.0,
                        self.scheduler_deadline - time.monotonic(),
                    )
                )
            cooldown = self.rate_limit.wait_remaining()
            if cooldown > 0:
                timeouts.append(cooldown)
            if timeouts:
                timeout = min(timeouts)
            done, _pending = await asyncio.wait(
                set(self.in_flight.values()),
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not done:
                if (
                    self.scheduler_deadline
                    and time.monotonic()
                    >= self.scheduler_deadline
                ):
                    await self.abort_pending(
                        pending_status="blocked",
                        discard_reason="编排 deadline 到期，未合并",
                        abandon_reason="编排 deadline 到期",
                        timed_out=True,
                    )
                    break
                continue

            self.drain_completed(done)

            if not self.implement_active():
                await self.flush_merges()
            if self.stop and not self.in_flight:
                break

        if self.merge_queue:
            if self.cancelled() or self.implement_active():
                self.discard_merges("同批 implement 未完成，未合并")
            else:
                await self.flush_merges()

        self.status = mark_blocked(self.tasks, self.status)
        self.refresh_ready()
        if not self.in_flight:
            pending_left = [
                str(task["id"])
                for task in self.tasks
                if self.status.get(str(task["id"]), "pending")
                == "pending"
            ]
            if pending_left and not self.ready:
                for task_id in pending_left:
                    self.status[task_id] = "blocked"

        pending_any = any(
            self.status.get(str(task["id"]), "pending") == "pending"
            for task in self.tasks
        )
        scheduler_continue = bool(
            pending_any and not self.cancelled()
        )
        if (
            self.scheduler_deadline
            and time.monotonic() >= self.scheduler_deadline
        ):
            scheduler_continue = False

        return {
            "tasks": self.tasks,
            "task_status": self.status,
            "task_attempts": self.attempts,
            "worker_results": self.results,
            "orchestrator_round": self.round_no + 1,
            "task_extensions_seen": self.seen,
            "task_extensions_applied": self.applied,
            "task_extension_errors": self.extension_errors,
            "dirty": self.dirty,
            "run_dirty": self.run_dirty,
            "pending_verification": self.pending_verification,
            "scheduler_deadline": self.scheduler_deadline,
            "scheduler_rate_limit": self.rate_limit.snapshot(),
            "token_budget_limit": self.budget.limit,
            "token_budget_spent": self.budget.spent,
            "token_budget_reserved": self.budget.reserved,
            "token_budget_exhausted": self.token_budget_exhausted,
            "scheduler_done": not scheduler_continue,
            "scheduler_continue": scheduler_continue,
        }

    def _no_tasks_result(self) -> dict[str, Any]:
        return {
            "scheduler_done": True,
            "scheduler_continue": False,
            "scheduler_deadline": self.scheduler_deadline,
            "task_extensions_applied": self.applied,
            "task_extension_errors": self.extension_errors,
            "task_extensions_seen": self.seen,
            "scheduler_rate_limit": self.rate_limit.snapshot(),
        }

    def _cancelled_result(self) -> dict[str, Any]:
        return {
            "scheduler_done": True,
            "scheduler_continue": False,
            "scheduler_deadline": self.scheduler_deadline,
            "task_extensions_applied": self.applied,
            "task_extension_errors": self.extension_errors,
            "task_extensions_seen": self.seen,
            "scheduler_rate_limit": self.rate_limit.snapshot(),
            "task_status": self.status,
        }

