"""scheduler 的运行态：把原闭包拆成可测试的 SchedulerRun 类。

行为与旧 `make_scheduler_node` 内部实现保持一致：ready 堆、in-flight、
限流退避、预算、合并队列、重试与动态提议都不改语义。
"""

from __future__ import annotations

import asyncio
import hashlib
import heapq
import json
import posixpath
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.config import get_stream_writer

from ubismart.config import Settings
from ubismart.knowledge.store import knowledge_context
from ubismart.llm.ratelimit import GLOBAL_LIMITER
from ubismart.orchestrator.budget import BudgetGuard
from ubismart.orchestrator.contracts import WorkerResult
from ubismart.orchestrator.control_checkpoint import ControlCheckpointStore
from ubismart.orchestrator.estimator import (
    EstimationStore,
    role_models_from_settings,
    sample_from_result,
)
from ubismart.orchestrator.graph import mark_blocked, validate_proposals
from ubismart.orchestrator.merge import preflight_patch_paths
from ubismart.orchestrator.merge_journal import MergeJournal
from ubismart.orchestrator.merge_queue import MergeQueue
from ubismart.orchestrator.patch_review import validate_review_issues
from ubismart.orchestrator.scheduler_plan import (
    deps_met,
    fallback_result,
    retry_context,
    task_depth,
    wire_implement_dependents,
)
from ubismart.orchestrator.scheduling import (
    RateLimitState,
    TokenBudget,
    aged_priority,
    ewma_usage,
    is_retryable,
    task_priority,
    task_token_reserve,
)
from ubismart.orchestrator.workers import WorkerRunner
from ubismart.platform.concurrency import (
    AsyncLeasePool,
    ConcurrencyGovernor,
    ResourceTimeout,
)
from ubismart.platform.observability import inc
from ubismart.sandbox.wsl_exec import WslSandbox
from ubismart.session.steering import SteeringController
from ubismart.session.worker_journal import (
    WorkerJournal,
    parse_roles,
    request_fingerprint,
    task_fingerprint,
    workspace_fingerprint,
)
from ubismart.tools.testrun import run_verification

# 低于该额度不再启动新任务：把任务"缩"到几百 token 只会让它注定失败，
# 还会让预算耗尽判定失效。
_MIN_TASK_TOKENS = 1_000


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
        patch_reviewer: (
            Callable[
                [dict[str, Any], str, RunnableConfig | None],
                Awaitable[dict[str, Any]],
            ]
            | None
        ) = None,
        sub_runner: Any = None,
    ) -> None:
        self.state = state
        self.config = config
        self.worker_runner = worker_runner
        self.settings = settings
        self.sandbox = sandbox
        self.root = root
        self.steering = steering
        self.governor = governor
        self.patch_reviewer = patch_reviewer

        self.thread_id = str(
            (((config or {}).get("configurable")) or {}).get(
                "thread_id", ""
            )
        )
        self.request = str(state.get("request") or "")
        self.depth = int(state.get("sub_depth", 0) or 0)
        self.suppress_events = bool(state.get("suppress_events"))
        self.sub_runner = sub_runner
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
        self.knowledge_run_id = str(state.get("knowledge_run_id") or "")
        self.change_contract = dict(state.get("change_contract") or {})
        self.wave_gates = [
            dict(item)
            for item in (state.get("wave_gates") or [])
            if isinstance(item, dict)
        ]
        self.interface_first = bool(
            getattr(settings, "deep_workflows_enabled", False)
            and getattr(settings, "interface_first_writes", False)
            and len(self.change_contract.get("waves") or []) > 1
        )

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
        self.worktrees_pruned = bool(state.get("worktrees_pruned"))
        self.merged_paths = {
            str(path).strip()
            for path in (state.get("merged_paths") or [])
            if str(path).strip()
        }
        self.merge_batches = [
            dict(item)
            for item in (state.get("merge_batches") or [])
            if isinstance(item, dict)
        ][-20:]
        if self.scheduler_deadline <= 0:
            seconds = int(settings.orchestrator_run_deadline_s or 0)
            self.scheduler_deadline = (
                time.monotonic() + seconds if seconds > 0 else 0.0
            )

        wire_implement_dependents(self.tasks)
        self.by_id = {str(task["id"]): task for task in self.tasks}

        # 边界重入时只恢复可重试的失败；确定性失败（没有 write_scope、
        # 工作区不干净等）重跑同一任务没有意义。
        latest: dict[str, dict[str, Any]] = {}
        for result in self.results:
            result_task_id = str(result.get("task_id", ""))
            previous = latest.get(result_task_id)
            if previous is None or int(
                result.get("attempt", 1) or 1
            ) >= int(previous.get("attempt", 1) or 1):
                latest[result_task_id] = result
        for task in self.tasks:
            task_id = str(task["id"])
            if (
                self.status.get(task_id) == "failed"
                and self.attempts.get(task_id, 0)
                <= settings.orchestrator_task_retries
                and (
                    latest.get(task_id) is None
                    or is_retryable(latest[task_id])
                )
            ):
                self.status[task_id] = "pending"

        # worker journal：崩溃/resume 后复用上一轮已完成的只读结果。
        # 必须在 mark_blocked 之前恢复，否则被依赖的任务会被误标 blocked。
        self.reused_records: list[dict[str, Any]] = []
        summary = dict(state.get("repo_summary") or {})
        self._journal_roles = parse_roles(
            settings.worker_journal_roles,
            default=("explore", "review"),
        )
        self._journal_allow_statuses = {"success"} | (
            {"partial"}
            if settings.worker_journal_reuse_partial
            else set()
        )
        self._journal_request_hash = request_fingerprint(self.request)
        self._journal_workspace_hash = workspace_fingerprint(summary)
        journal_ready = bool(
            settings.worker_journal_enabled
            and (
                not settings.worker_journal_require_clean_git
                or (
                    summary.get("git")
                    and summary.get("git_root")
                    and summary.get("git_clean")
                    and summary.get("git_head")
                )
            )
        )
        self.worker_journal = WorkerJournal(
            Path(settings.resolved_audit_dir) / "worker_journal.jsonl",
            enabled=journal_ready,
            max_bytes=settings.worker_journal_max_bytes,
        )
        if self.worker_journal.enabled:
            self._restore_reused_results()
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
                max_waiters=int(
                    getattr(settings, "concurrency_max_waiters", 128) or 0
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
        budget_limit = int(
            state.get("token_budget_limit")
            or settings.orchestrator_total_tokens
        )
        self.budget = TokenBudget(budget_limit, self.results)
        self.budget_guard = BudgetGuard.from_settings(
            settings,
            results=self.results,
            scheduler_deadline=self.scheduler_deadline,
            token_spent=self.budget.spent,
            token_reserved=self.budget.reserved,
            token_limit=self.budget.limit,
            wall_limit_ms=(
                int(state.get("wall_limit_ms") or 0) or None
            ),
        )
        self.budget_warning_emitted: set[str] = set()
        self.budget_abort: dict[str, Any] = dict(
            state.get("budget_abort") or {}
        )
        self.estimation_card = dict(state.get("estimation_card") or {})
        self.gate = dict(self.estimation_card.get("gate") or {})
        self.control_store = ControlCheckpointStore(
            Path(settings.resolved_audit_dir) / "control_checkpoints",
            enabled=bool(
                getattr(
                    settings,
                    "orchestration_control_checkpoints",
                    True,
                )
            ),
        )
        self.control_checkpoints = [
            dict(item)
            for item in (state.get("control_checkpoints") or [])
            if isinstance(item, dict)
        ][-20:]
        self.phase_gate_done: set[int] = set()
        self.phase_gates = False
        self.stage_token_limit = 0
        self.action_counts: dict[str, int] = {}
        self.loop_abort: dict[str, Any] = dict(
            state.get("loop_abort") or {}
        )
        self.estimation_store = EstimationStore(
            Path(settings.resolved_audit_dir) / "estimates.jsonl",
            enabled=bool(
                getattr(settings, "orchestrator_estimation_enabled", True)
            ),
        )
        self.estimation_task_type = str(
            state.get("workflow") or "direct"
        )
        self.estimation_model = str(
            getattr(settings, "orchestrator_worker_model", "")
            or getattr(settings, "deepseek_model", "")
        )
        self.role_models = role_models_from_settings(settings)
        stages = self._stages()
        self.phase_gates = bool(
            self.gate.get("phase_gates")
        ) and len(stages) > 1
        if self.phase_gates and self.budget.limit > 0 and stages:
            self.stage_token_limit = max(
                1, self.budget.limit // len(stages)
            )
        self.merge_journal = MergeJournal(sandbox, root)
        self.merge_queue = MergeQueue(self.merge_journal)
        # 已完成 implement 的补丁真实路径；增量合并的判定依据。
        self.merge_paths: dict[str, list[str]] = {}
        self.token_budget_exhausted = False
        # 每个任务的硬 token 切片（来自全局预算预留）；worker 到顶即停。
        self.task_budget: dict[str, int] = {}
        self.ready: list[tuple[int, int, str]] = []
        self.ready_at: dict[str, float] = {}
        self.queued: set[str] = set()
        self.seq = 0
        self.in_flight: dict[str, asyncio.Task[WorkerResult]] = {}
        self.future_task: dict[asyncio.Task[WorkerResult], str] = {}
        self.queued_at: dict[str, float] = {}
        self.started_keys: set[str] = set()
        self.stop = False
        self.blocked_emitted: set[str] = set()
        self.completed_since_boundary = 0
        self.checkpoint_every = max(
            1, int(settings.orchestrator_checkpoint_every or 1)
        )
        if self.gate.get("checkpoint_granularity") == "fine":
            self.checkpoint_every = 1
        elif self.gate.get("checkpoint_granularity") == "normal":
            self.checkpoint_every = min(self.checkpoint_every, 2)
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
        # 子编排器只向父控制面交 SubResult；绝不能把子事件/卡片推到 Root 流。
        if self.suppress_events:
            return
        try:
            writer = get_stream_writer()
        except Exception:  # noqa: BLE001 - 单测直接调用节点时没有 writer
            return
        try:
            writer(payload)
        except Exception:  # noqa: BLE001 - 前端断开不拖垮调度
            pass

    def emit_blocked(self, task_id: str, reason: str) -> None:
        if not task_id or task_id in self.blocked_emitted:
            return
        self.blocked_emitted.add(task_id)
        task = self.by_id.get(task_id) or {}
        if not task.get("blocked_reason"):
            task["blocked_reason"] = reason
        self.emit(
            {
                "type": "task_blocked",
                "task_id": task_id,
                "role": str(task.get("role", "")),
                "reason": reason,
            }
        )

    def detect_repeat_actions(self, record: dict[str, Any]) -> None:
        """同一 (tool, args) 重复到上限即熔断，防止分形版重试黑洞。"""
        limit = int(
            getattr(
                self.settings,
                "orchestrator_repeat_action_limit",
                3,
            )
            or 0
        )
        if limit <= 0 or self.loop_abort:
            return
        for call in record.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or call.get("tool") or "")
            if not name:
                continue
            args = call.get("args") or {}
            try:
                material = json.dumps(
                    [name, args],
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                )
            except (TypeError, ValueError):
                material = f"{name}:{args!r}"
            digest = hashlib.sha1(
                material.encode("utf-8")
            ).hexdigest()[:16]
            count = self.action_counts.get(digest, 0) + 1
            self.action_counts[digest] = count
            if count < limit:
                continue
            self.loop_abort = {
                "reason": f"重复动作 {name} 达到 {count} 次",
                "task_id": str(record.get("task_id", "")),
                "tool": name,
                "count": count,
            }
            self.stop = True
            self._block_pending("重复动作熔断，未派发")
            self.emit(
                {
                    "type": "loop_detected",
                    "task_id": str(record.get("task_id", "")),
                    "tool": name,
                    "count": count,
                    "reason": self.loop_abort["reason"],
                }
            )
            return

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
        proposer = self.by_id.get(str(result.get("task_id", ""))) or {}
        try:
            proposer_stage = max(0, int(proposer.get("stage", 0) or 0))
        except (TypeError, ValueError):
            proposer_stage = 0
        for task in new_tasks:
            task["depth"] = task_depth(
                self.tasks, str(task["id"])
            )
            task["stage"] = proposer_stage
            self.tasks.append(task)
            self.by_id[str(task["id"])] = task
            self.status[str(task["id"])] = "pending"
            self.applied.append(task)

    # ------------------------------------------------------------------
    # ready / 预算
    # ------------------------------------------------------------------
    def budget_status(self):
        """按已结算的 tokens 刷新四维快照并返回状态。"""
        self.budget_guard.refresh_tokens(
            spent=self.budget.spent, reserved=self.budget.reserved
        )
        return self.budget_guard.check()

    def _emit_budget_alerts(self, status) -> None:
        snapshot = status.snapshot
        ratios = snapshot.ratios()
        used_by_dimension = {
            "steps": snapshot.steps,
            "tool_calls": snapshot.tool_calls,
            "tokens": snapshot.tokens,
            "wall_ms": snapshot.wall_ms,
        }
        limit_by_dimension = {
            "steps": snapshot.step_limit,
            "tool_calls": snapshot.tool_call_limit,
            "tokens": snapshot.token_limit,
            "wall_ms": snapshot.wall_limit_ms,
        }
        for level, dimensions, counter in (
            ("warning", status.warning, "agent_budget_warnings_total"),
            ("exhausted", status.exhausted, "agent_budget_exhausted_total"),
        ):
            for dimension in dimensions:
                key = f"{level}:{dimension}"
                if key in self.budget_warning_emitted:
                    continue
                self.budget_warning_emitted.add(key)
                inc(counter, {"dimension": dimension})
                ratio = ratios.get(dimension, 0.0)
                self.emit(
                    {
                        "type": "budget_warning",
                        "level": level,
                        "dimension": dimension,
                        "used": int(used_by_dimension.get(dimension, 0)),
                        "limit": int(limit_by_dimension.get(dimension, 0)),
                        "ratio": round(ratio, 4),
                        "message": (
                            f"{dimension} 预算已耗尽，停止派发"
                            if level == "exhausted"
                            else f"{dimension} 预算已用 {ratio:.0%}"
                        ),
                    }
                )

    def _record_budget_abort(self, reason: str) -> None:
        completed = [
            str(task["id"])
            for task in self.tasks
            if self.status.get(str(task["id"])) in {"success", "partial"}
        ]
        blocked = [
            str(task["id"])
            for task in self.tasks
            if self.status.get(str(task["id"])) in {"blocked", "pending"}
        ]
        self.budget_abort = self.budget_guard.abort_report(
            completed_tasks=completed,
            blocked_tasks=blocked,
        )
        self.budget_abort["reason"] = reason

    def _stop_for_budget(self, status) -> None:
        self.stop = True
        self.token_budget_exhausted = (
            self.token_budget_exhausted or "tokens" in status.exhausted
        )
        if "tokens" in status.exhausted:
            self._cancel_readonly_inflight()
        self._block_pending("四维预算耗尽，未派发")
        self._record_budget_abort(
            "预算耗尽：" + "、".join(status.exhausted or ["unknown"])
        )

    def check_budget(self) -> Any:
        status = self.budget_status()
        self._emit_budget_alerts(status)
        if status.exhausted and not self.stop:
            self._stop_for_budget(status)
        return status

    def _stages(self) -> list[int]:
        stages: set[int] = set()
        for task in self.tasks:
            try:
                stages.add(max(0, int(task.get("stage", 0) or 0)))
            except (TypeError, ValueError):
                stages.add(0)
        return sorted(stages)

    def _stage_tasks(self, stage: int) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for task in self.tasks:
            try:
                value = max(0, int(task.get("stage", 0) or 0))
            except (TypeError, ValueError):
                value = 0
            if value == stage:
                tasks.append(task)
        return tasks

    def _stage_spent(self, stage: int) -> int:
        spent = 0
        for result in self.results:
            task = self.by_id.get(str(result.get("task_id", ""))) or {}
            try:
                value = max(0, int(task.get("stage", 0) or 0))
            except (TypeError, ValueError):
                value = 0
            if value != stage:
                continue
            spent += int(result.get("input_tokens") or 0) + int(
                result.get("output_tokens") or 0
            )
        return spent

    def _artifact_refs(self) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for result in self.results:
            artifact_id = str(result.get("artifact_id") or "")
            if not artifact_id:
                continue
            refs.append(
                {
                    "task_id": str(result.get("task_id", "")),
                    "attempt": int(result.get("attempt", 1) or 1),
                    "artifact_id": artifact_id,
                }
            )
        return refs

    async def _save_control_checkpoint(
        self,
        *,
        stage: int,
        status: str,
        reason: str,
        completed: int,
        failed: int,
    ) -> str:
        plan_version = int(self.state.get("plan_version", 0) or 0)
        checkpoint_id = (
            f"stage-{stage}-v{plan_version}-{len(self.results)}"
        )
        payload = {
            "checkpoint_id": checkpoint_id,
            "thread_id": self.thread_id,
            "stage": stage,
            "status": status,
            "reason": reason,
            "plan_version": plan_version,
            "contract": dict(self.state.get("contract") or {}),
            "tasks": self.tasks,
            "task_status": self.status,
            "artifact_refs": self._artifact_refs(),
            "budget": self.budget_guard.snapshot().as_dict(),
            "early_stop": {
                "token_budget_exhausted": self.token_budget_exhausted,
                "budget_abort": self.budget_abort,
                "same_failure_count": int(
                    self.state.get("same_failure_count", 0) or 0
                ),
            },
            "wave_gates": self.wave_gates,
            "completed": completed,
            "failed": failed,
        }
        saved = await asyncio.to_thread(self.control_store.save, payload)
        if saved:
            self.control_checkpoints = (
                self.control_checkpoints
                + [
                    {
                        "checkpoint_id": saved,
                        "stage": stage,
                        "status": status,
                        "plan_version": plan_version,
                    }
                ]
            )[-20:]
        return saved

    async def _check_phase_gates(self) -> bool:
        """阶段门：阶段完成→控制检查点/中间交付；失败→只回退本阶段之后。"""
        if not self.phase_gates or self.interface_first:
            return True
        for stage in self._stages():
            if stage in self.phase_gate_done:
                continue
            tasks = self._stage_tasks(stage)
            if not tasks:
                continue
            statuses = [
                str(self.status.get(str(task["id"]), "pending"))
                for task in tasks
            ]
            if any(
                value in {"pending", "merging"} for value in statuses
            ) or any(str(task["id"]) in self.in_flight for task in tasks):
                continue
            completed = sum(
                1 for value in statuses if value in {"success", "partial"}
            )
            failed = sum(
                1
                for value in statuses
                if value in {"failed", "blocked", "cancelled"}
            )
            reason = ""
            if self.stage_token_limit > 0 and self._stage_spent(
                stage
            ) >= self.stage_token_limit:
                reason = (
                    f"阶段 {stage} 预算耗尽"
                    f"（>{self.stage_token_limit} tokens）"
                )
            elif failed:
                reason = f"阶段 {stage} 有 {failed} 个任务未成功"
            status = "failed" if reason else "passed"
            self.phase_gate_done.add(stage)
            if reason:
                self._record_gate(stage, "failed", reason)
                self._block_after_stage(
                    stage, f"{reason}，已停止后续阶段"
                )
            else:
                self._record_gate(stage, "passed", "")
            checkpoint_id = await self._save_control_checkpoint(
                stage=stage,
                status=status,
                reason=reason,
                completed=completed,
                failed=failed,
            )
            self.emit(
                {
                    "type": "stage_done",
                    "stage": stage,
                    "status": status,
                    "reason": reason,
                    "completed": completed,
                    "failed": failed,
                    "checkpoint_id": checkpoint_id,
                    "plan_version": int(
                        self.state.get("plan_version", 0) or 0
                    ),
                }
            )
            if reason:
                return False
        return True

    def stage_ready(self, task: dict[str, Any]) -> bool:
        """高 stage 的 join 任务必须等所有更低 stage 任务收敛。"""
        try:
            stage = int(task.get("stage", 0) or 0)
        except (TypeError, ValueError):
            stage = 0
        if stage <= 0:
            return True
        for other in self.tasks:
            other_id = str(other.get("id", ""))
            if other_id == str(task.get("id", "")):
                continue
            try:
                other_stage = int(other.get("stage", 0) or 0)
            except (TypeError, ValueError):
                other_stage = 0
            if other_stage >= stage:
                continue
            if (
                other_id in self.in_flight
                or self.status.get(other_id) == "pending"
            ):
                return False
        return True

    def dependency_context(self, task: dict[str, Any]) -> dict[str, Any]:
        latest: dict[str, dict[str, Any]] = {}
        for result in self.results:
            task_id = str(result.get("task_id", ""))
            previous = latest.get(task_id)
            if previous is None or int(result.get("attempt", 1)) >= int(
                previous.get("attempt", 1)
            ):
                latest[task_id] = result
        entries: list[dict[str, Any]] = []
        for dep in task.get("depends_on") or []:
            result = latest.get(str(dep))
            if not result:
                continue
            entries.append(
                {
                    "task_id": str(dep),
                    "role": str(result.get("role", "")),
                    "status": str(result.get("status", "")),
                    "summary": str(result.get("summary", ""))[:1_500],
                    "findings": [
                        str(item)
                        for item in (result.get("findings") or [])[:8]
                    ],
                    "files": [
                        str(item)
                        for item in (result.get("files") or [])[:20]
                    ],
                    "sources": [
                        dict(item)
                        for item in (result.get("sources") or [])[:50]
                        if isinstance(item, dict)
                    ],
                }
            )
        return {"tasks": entries}

    def refresh_ready(self) -> None:
        for task in self.tasks:
            task_id = str(task["id"])
            if self.status.get(task_id, "pending") != "pending":
                continue
            if task_id in self.in_flight or task_id in self.queued:
                continue
            if not deps_met(task, self.status):
                continue
            if not self.stage_ready(task):
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
                # 模型给的上限不能大于父预算剩余额度；否则一个夸张的
                # max_tokens 会让任务在准入阶段看起来永远放不进去。
                # 剩余额度低于最小可执行切片时不要"硬缩"到 1 token 挤进去，
                # 否则预算耗尽判定永远不会触发。
                remaining = self.budget_guard.remaining_tokens()
                if remaining is not None and remaining >= _MIN_TASK_TOKENS:
                    return max(_MIN_TASK_TOKENS, min(value, remaining))
                return value
        observed = ewma_usage(self.results, str(task.get("role", "")))
        if observed > 0:
            estimate = int(observed * 1.2)
        else:
            estimate = task_token_reserve(task)
        return max(500, min(estimate, 16_000))

    def is_document_boundary(self, record: dict[str, Any]) -> bool:
        """每个 document 任务完成都触发一次 steering/checkpoint 边界。"""
        return (
            bool(
                getattr(
                    self.settings,
                    "orchestrator_document_checkpoint",
                    True,
                )
            )
            and str(record.get("role", "")) == "document"
            and str(record.get("status", ""))
            in {"success", "partial"}
        )

    def role_limit(self, role: str) -> int:
        # author 是报告写入的全局单实例，不允许配置放大。
        if role == "author":
            return 1
        return {
            "document": int(
                getattr(
                    self.settings,
                    "orchestrator_document_concurrency",
                    2,
                )
            ),
            "vision": int(
                getattr(self.settings, "orchestrator_vision_concurrency", 2)
            ),
        }.get(role, 0)

    def role_token_limit(self, role: str) -> int:
        return int(
            {
                "document": getattr(
                    self.settings, "orchestrator_document_token_budget", 0
                ),
                "vision": getattr(
                    self.settings, "orchestrator_vision_token_budget", 0
                ),
                "author": getattr(
                    self.settings, "orchestrator_author_token_budget", 0
                ),
            }.get(role, 0)
        )

    def role_token_spent(self, role: str) -> int:
        return sum(
            int(result.get("input_tokens") or 0)
            + int(result.get("output_tokens") or 0)
            for result in self.results
            if str(result.get("role", "")) == role
        )

    def role_budget_exhausted(self, task: dict[str, Any]) -> bool:
        role = str(task.get("role", ""))
        limit = self.role_token_limit(role)
        return limit > 0 and self.role_token_spent(role) >= limit

    def role_available(self, task: dict[str, Any]) -> bool:
        """角色级并发：document/vision/author 各有独立上限。"""
        role = str(task.get("role", ""))
        limit = self.role_limit(role)
        if limit <= 0:
            return True
        active = sum(
            1
            for task_id in self.in_flight
            if str(self.by_id.get(task_id, {}).get("role", "")) == role
        )
        return active < limit

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

    def hard_timeout(self, role: str) -> float:
        """worker 外层兜底超时；0/None 表示不设（仍受 run deadline 约束）。

        runner 各自有内部超时，但任何一个漏配或卡在同步调用上，都只能等
        整个 run 的 deadline。外层再加一道，确保单任务不会无限占着槽位。
        """
        configured = float(
            getattr(self.settings, "orchestrator_worker_hard_timeout_s", 0.0)
            or 0.0
        )
        if configured > 0:
            return configured
        if role == "implement":
            base = float(self.settings.orchestrator_worker_timeout or 0)
        elif role in {"explore", "review"}:
            base = float(self.settings.explore_timeout or 0)
        else:
            base = max(
                600.0,
                float(self.settings.orchestrator_worker_timeout or 0),
            )
        timeout = max(base, 1.0) + 30.0
        watchdog_ms = int(self.gate.get("watchdog_ms") or 0)
        if watchdog_ms > 0:
            # 预估卡的 watchdog 是 P90 单步墙钟；外层兜底不能比它更宽松。
            timeout = min(timeout, max(1.0, watchdog_ms / 1000.0))
        return timeout

    def gate_timeout(self) -> float:
        """波次 gate 验证的墙钟上限；0 表示按 verify_timeout 推导。"""
        configured = float(
            getattr(self.settings, "orchestrator_gate_timeout_s", 0.0) or 0.0
        )
        if configured > 0:
            return configured
        verify = float(getattr(self.settings, "verify_timeout", 0) or 0)
        return max(1.0, verify * 2 + 30.0)

    def review_timeout(self) -> float:
        """单次补丁审查的墙钟上限；0 表示沿用 review 角色的 hard timeout。"""
        configured = float(
            getattr(
                self.settings,
                "orchestrator_patch_review_timeout_s",
                0.0,
            )
            or 0.0
        )
        if configured > 0:
            return configured
        return self.hard_timeout("review")

    async def _await_with_cancel(
        self,
        factory: Callable[[], Awaitable[Any]],
        *,
        timeout: float,
        poll: float = 0.2,
    ) -> tuple[str, Any]:
        """等待一个可取消的工作项，并在等待期间轮询用户取消与 deadline。

        返回 `(outcome, payload)`，outcome 为 ok / error / timeout /
        cancelled / deadline。`asyncio.wait_for` 只取消协程；对内部还包着
        `to_thread` 的验证命令，超时后线程可能继续跑，但调度器不再被它
        拖住，也不会把它当成成功。
        """
        if self.cancelled():
            return "cancelled", None
        try:
            task = asyncio.ensure_future(factory())
        except Exception as exc:  # noqa: BLE001 - 同步抛错也按工作项失败处理
            return "error", exc

        def _consume(done: asyncio.Future[Any]) -> None:
            if done.cancelled():
                return
            try:
                done.exception()
            except Exception:  # noqa: BLE001 - 仅消费异常，避免未取回告警
                pass

        task.add_done_callback(_consume)
        deadline = time.monotonic() + timeout if timeout > 0 else 0.0
        try:
            while True:
                wait_for = poll
                if deadline:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        task.cancel()
                        return "timeout", None
                    wait_for = min(poll, remaining)
                done, _ = await asyncio.wait({task}, timeout=wait_for)
                if done:
                    if task.cancelled():
                        return "cancelled", None
                    exc = task.exception()
                    if exc is not None:
                        return "error", exc
                    return "ok", task.result()
                if self.cancelled():
                    task.cancel()
                    return "cancelled", None
                if (
                    self.scheduler_deadline
                    and time.monotonic() >= self.scheduler_deadline
                ):
                    task.cancel()
                    return "deadline", None
        except BaseException:
            task.cancel()
            raise

    def _cancel_readonly_inflight(self) -> None:
        """预算耗尽时只取消只读在飞任务；implement 的补丁仍会合并。"""
        for task_id, future in list(self.in_flight.items()):
            role = str(self.by_id.get(task_id, {}).get("role", ""))
            if role != "implement":
                future.cancel()

    def _block_pending(self, reason: str) -> None:
        """把尚未派发、也不在飞的任务标 blocked（在飞 implement 保留）。"""
        for task in self.tasks:
            task_id = str(task["id"])
            if task_id in self.in_flight:
                continue
            if self.status.get(task_id, "pending") != "pending":
                continue
            self.status[task_id] = "blocked"
            task["blocked_reason"] = reason
            self.emit_blocked(task_id, reason)
        self.ready.clear()
        self.queued.clear()
        self.ready_at.clear()

    def _restore_reused_results(self) -> None:
        """把 journal 里匹配的只读结果直接回填为已完成状态。"""
        existing = {
            str(result.get("task_id", "")) for result in self.results
        }
        for task in self.tasks:
            task_id = str(task["id"])
            if self.status.get(task_id, "pending") != "pending":
                continue
            if task_id in existing or task.get("retry_context"):
                continue
            role = str(task.get("role", ""))
            if role not in self._journal_roles:
                continue
            entry = self.worker_journal.lookup(
                thread_id=self.thread_id,
                request_hash=self._journal_request_hash,
                task_hash=task_fingerprint(task),
                workspace_hash=self._journal_workspace_hash,
                allow_statuses=self._journal_allow_statuses,
            )
            if not entry:
                continue
            result = dict(entry.get("result") or {})
            if not result:
                continue
            result["task_id"] = task_id
            result["reused"] = True
            result["attempt"] = int(self.attempts.get(task_id, 1) or 1)
            result["input_tokens"] = 0
            result["output_tokens"] = 0
            result["duration_ms"] = 0
            result["llm_ms"] = 0
            result["tool_ms"] = 0
            result["tool_calls"] = []
            result["proposed_tasks"] = []
            result["budget_used"] = {}
            self.results.append(result)
            self.status[task_id] = str(
                result.get("status", "success")
            )
            self.attempts[task_id] = max(
                int(self.attempts.get(task_id, 0) or 0),
                int(entry.get("attempt", 1) or 1),
            )
            self.reused_records.append(result)

    async def _journal_completed(
        self, record: dict[str, Any]
    ) -> None:
        if not self.worker_journal.enabled:
            return
        role = str(record.get("role", ""))
        # 补丁/worktree 可能已经过期，implement 永不写入复用日志。
        if role == "implement" or role not in self._journal_roles:
            return
        if record.get("reused"):
            return
        status = str(record.get("status", ""))
        if status not in self._journal_allow_statuses:
            return
        task = self.by_id.get(str(record.get("task_id", "")))
        if task is None:
            return
        await asyncio.to_thread(
            self.worker_journal.record,
            thread_id=self.thread_id,
            request_hash=self._journal_request_hash,
            task_hash=task_fingerprint(task),
            workspace_hash=self._journal_workspace_hash,
            role=role,
            status=status,
            attempt=int(record.get("attempt", 1) or 1),
            result=record,
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
            # 重试时升级到主模型；首次用角色默认（explore/review 是便宜模型）。
            # attempt 必须下传：implement 的 worktree/patch 路径按 attempt 隔离，
            # 避免超时后旧线程仍在写、重试却复用了同一个目录。
            worker_task = {
                **task,
                "escalate": attempt > 1,
                "attempt": attempt,
            }
            try:
                reserve_tokens = int(self.task_budget.get(task_id, 0) or 0)
                explicit_tokens = int(task.get("max_tokens") or 0)
            except (TypeError, ValueError):
                reserve_tokens = explicit_tokens = 0
            self.budget_guard.refresh_tokens(
                spent=self.budget.spent, reserved=self.budget.reserved
            )
            # 模型给的预算字段一律夹到父预算：既不能超过本次派发切片，
            # 也不能超过父的剩余额度。切片是父分配的，模型无权放大。
            remaining_tokens = self.budget_guard.remaining_tokens()
            if explicit_tokens > 0:
                if reserve_tokens > 0:
                    explicit_tokens = min(explicit_tokens, reserve_tokens)
                if remaining_tokens is not None:
                    explicit_tokens = min(
                        explicit_tokens, remaining_tokens + reserve_tokens
                    )
            role_cap = self.role_token_limit(str(task.get("role", "")))
            # 硬切片优先级：任务显式预算 > 角色总预算（document/vision/author）
            # > 派发预留。document 的 6k 只是准入估计，不能当硬上限。
            if explicit_tokens > 0:
                hard_tokens = explicit_tokens
            elif role_cap > 0:
                hard_tokens = role_cap
            else:
                hard_tokens = reserve_tokens
            if hard_tokens > 0:
                if remaining_tokens is not None:
                    hard_tokens = max(
                        0, min(hard_tokens, remaining_tokens + reserve_tokens)
                    )
                if hard_tokens > 0:
                    worker_task["max_tokens"] = hard_tokens
            requested_timeout = task.get("timeout")
            if requested_timeout:
                try:
                    clamped_timeout = self.budget_guard.clamp_timeout(
                        float(requested_timeout)
                    )
                except (TypeError, ValueError):
                    clamped_timeout = 0.0
                if clamped_timeout > 0:
                    worker_task["timeout"] = clamped_timeout
            needs_baseline = int(task.get("stage", 0) or 0) > 0 or bool(
                self.state.get("orchestrator_fix_mode")
            )
            if role == "implement" and needs_baseline:
                worker_task = {
                    **worker_task,
                    "seed_from_workspace": True,
                    "baseline_paths": sorted(self.merged_paths),
                }
            timeout = self.hard_timeout(role)
            if role == "sub_orchestrator":
                result = await self._run_sub_orchestrator(
                    task,
                    worker_id=worker_id,
                    attempt=attempt,
                    queue_wait_ms=queue_wait_ms,
                    hard_tokens=hard_tokens,
                    reserve_tokens=reserve_tokens,
                    timeout=timeout,
                )
            else:
                try:
                    if timeout > 0:
                        with knowledge_context(self.knowledge_run_id):
                            result = await asyncio.wait_for(
                                self.worker_runner(
                                    worker_task,
                                    parent_request=self.request,
                                    session_brief=self.session_brief,
                                    cwd=self.cwd,
                                    config=self.config,
                                    cancel=self.cancelled,
                                    dependency_context=self.dependency_context(task),
                                ),
                                timeout=timeout,
                            )
                    else:
                        with knowledge_context(self.knowledge_run_id):
                            result = await self.worker_runner(
                                worker_task,
                                parent_request=self.request,
                                session_brief=self.session_brief,
                                cwd=self.cwd,
                                config=self.config,
                                cancel=self.cancelled,
                                dependency_context=self.dependency_context(task),
                            )
                except TimeoutError:
                    result = WorkerResult(
                        task_id=task_id,
                        role=role,
                        worker_id=worker_id,
                        goal=str(task.get("goal", "")),
                        status="failed",
                        summary=f"worker 外层超时（>{timeout:g}s）",
                        error=f"worker hard timeout after {timeout:g}s",
                        timed_out=True,
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

    async def _run_sub_orchestrator(
        self,
        task: dict[str, Any],
        *,
        worker_id: str,
        attempt: int,
        queue_wait_ms: int,
        hard_tokens: int,
        reserve_tokens: int,
        timeout: float,
    ) -> WorkerResult:
        task_id = str(task["id"])
        goal = str(task.get("goal", ""))
        max_depth = int(
            getattr(self.settings, "orchestrator_max_depth", 1) or 1
        )
        if self.sub_runner is None:
            return WorkerResult(
                task_id=task_id,
                role="sub_orchestrator",
                worker_id=worker_id,
                goal=goal,
                status="failed",
                summary="嵌套编排未启用或缺少规划模型",
                error="sub_orchestrator unavailable",
                attempt=attempt,
                queue_wait_ms=queue_wait_ms,
                depth=self.depth + 1,
            )
        if self.depth >= max_depth:
            return WorkerResult(
                task_id=task_id,
                role="sub_orchestrator",
                worker_id=worker_id,
                goal=goal,
                status="failed",
                summary=f"嵌套深度已达上限 {max_depth}",
                error="max depth reached",
                attempt=attempt,
                queue_wait_ms=queue_wait_ms,
                depth=self.depth + 1,
            )
        budget = int(hard_tokens or 0)
        if budget <= 0 and self.budget.enabled:
            budget = max(
                0,
                int(self.budget_guard.remaining_tokens() or 0)
                + int(reserve_tokens or 0),
            )
        if self.budget.enabled and budget <= 0:
            return WorkerResult(
                task_id=task_id,
                role="sub_orchestrator",
                worker_id=worker_id,
                goal=goal,
                status="failed",
                summary="父预算已无可用切片，拒绝派生子编排器",
                error="sub orchestrator budget exhausted",
                attempt=attempt,
                queue_wait_ms=queue_wait_ms,
                depth=self.depth + 1,
            )
        wall_ms = int(timeout * 1000) if timeout > 0 else 0
        remaining_wall = self.budget_guard.remaining_wall_ms()
        if remaining_wall is not None:
            wall_ms = min(wall_ms or remaining_wall, remaining_wall)
        try:
            sub = await asyncio.wait_for(
                self.sub_runner.run(
                    task,
                    parent_thread_id=self.thread_id,
                    cwd=self.cwd,
                    repo_summary=dict(self.state.get("repo_summary") or {}),
                    budget_tokens=budget,
                    wall_ms=wall_ms,
                    plan_version=int(
                        self.state.get("plan_version", 0) or 0
                    ),
                    depth=self.depth + 1,
                ),
                timeout=timeout if timeout > 0 else None,
            )
        except TimeoutError:
            return WorkerResult(
                task_id=task_id,
                role="sub_orchestrator",
                worker_id=worker_id,
                goal=goal,
                status="failed",
                summary=f"子编排器外层超时（>{timeout:g}s）",
                error=f"sub orchestrator timeout after {timeout:g}s",
                attempt=attempt,
                queue_wait_ms=queue_wait_ms,
                depth=self.depth + 1,
                timed_out=True,
            )
        except Exception as exc:  # noqa: BLE001 - 子失败局部化
            return WorkerResult(
                task_id=task_id,
                role="sub_orchestrator",
                worker_id=worker_id,
                goal=goal,
                status="failed",
                summary=f"子编排器异常：{type(exc).__name__}: {exc}",
                error=str(exc),
                attempt=attempt,
                queue_wait_ms=queue_wait_ms,
                depth=self.depth + 1,
            )
        artifact = dict(sub.artifact or {})
        handoff = dict(artifact.get("handoff") or {})
        files = [
            str(path)
            for task_entry in (handoff.get("tasks") or [])
            if isinstance(task_entry, dict)
            for path in (task_entry.get("files") or [])
            if str(path).strip()
        ]
        cost = dict(sub.actual_cost or {})
        return WorkerResult(
            task_id=task_id,
            role="sub_orchestrator",
            worker_id=worker_id,
            goal=goal,
            status=sub.status,
            summary=str(
                artifact.get("summary")
                or sub.fault_reason
                or sub.status
            ),
            files=files[:20],
            findings=(
                [str(sub.fault_reason)] if sub.fault_reason else []
            ),
            input_tokens=int(cost.get("input_tokens") or 0),
            output_tokens=int(cost.get("output_tokens") or 0),
            duration_ms=int(cost.get("wall_ms") or 0),
            attempt=attempt,
            queue_wait_ms=queue_wait_ms,
            depth=self.depth + 1,
            artifact_id=sub.local_ckpt_digest,
            error=sub.fault_reason,
            sub_result=sub.model_dump(),
            sub_signals=[
                dict(item)
                for item in (artifact.get("signals") or [])
                if isinstance(item, dict)
            ],
            parent_sub_id=task_id,
            fanout_index=int(task.get("fanout_index", 0) or 0),
            sub_spawn_mode=str(task.get("sub_spawn_mode") or "static"),
            sub_budget_used=int(cost.get("tokens") or 0),
        )

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
            self.task_budget.pop(task_id, None)
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
        finished_at = record.pop("finished_at", None)
        if (
            finished_at is not None
            and record.get("role") == "implement"
            and record.get("merged")
        ):
            record["merge_wait_ms"] = max(
                0,
                int(
                    (time.monotonic() - float(finished_at)) * 1000
                ),
            )
        self.emit({"type": "worker_finished", "result": record})
        attempt = int(record.get("attempt", 1))
        if (
            record.get("status") == "failed"
            and attempt <= self.settings.orchestrator_task_retries
            and is_retryable(record)
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
        before = dict(self.status)
        self.status.update(mark_blocked(self.tasks, self.status))
        for task_id, value in self.status.items():
            if value == "blocked" and before.get(task_id) != "blocked":
                self.emit_blocked(task_id, "依赖任务失败或被阻塞")

    def implement_active(self) -> bool:
        return self.merge_queue.has_active_implement(
            in_flight=set(self.in_flight),
            status=self.status,
            by_id=self.by_id,
        )

    def _inflight_paths(self) -> set[str]:
        """在飞 implement 的写范围与已知补丁路径。

        写范围可能是目录（`app/`），因此判定用前缀包含，而不是简单集合相交。
        """
        blocked: set[str] = set()
        for task_id in self.in_flight:
            task = self.by_id.get(task_id) or {}
            for scope in task.get("write_scope") or []:
                normalized = str(scope).strip().replace("\\", "/").rstrip("/")
                if normalized:
                    blocked.add(normalized)
            blocked.update(self.merge_paths.get(task_id, []))
        return blocked

    async def _ensure_merge_paths(self) -> None:
        """预解析待合并补丁的真实路径；失败的不缓存，触发保守回退。"""
        if self.sandbox is None or not self.root:
            return
        for record in self.merge_queue.pending:
            task_id = str(record.get("task_id", ""))
            if task_id in self.merge_paths:
                continue
            patch_path = str(record.get("patch_path") or "")
            if not patch_path:
                continue
            paths, error = await asyncio.to_thread(
                preflight_patch_paths, self.sandbox, patch_path, self.root
            )
            if not error and paths:
                self.merge_paths[task_id] = paths

    async def _read_patch_text(self, record: dict[str, Any]) -> str:
        if self.sandbox is None or not self.root:
            return ""
        patch_path = str(record.get("patch_path") or "")
        if not patch_path:
            return ""
        relative = posixpath.relpath(patch_path, self.root)
        if relative.startswith(".."):
            return ""
        max_chars = max(
            4_000,
            int(
                getattr(
                    self.settings,
                    "orchestrator_patch_review_max_chars",
                    60_000,
                )
                or 60_000
            ),
        )
        try:
            from ubismart.sandbox.fs import SandboxFs

            return await asyncio.to_thread(
                SandboxFs(self.sandbox, self.root).read_text,
                relative,
                max_bytes=max_chars * 4,
            )
        except Exception:  # noqa: BLE001 - 读不到补丁就跳过审查
            return ""

    async def _review_patches(
        self, records: list[dict[str, Any]]
    ) -> list[dict[str, Any]] | None:
        """合并前审查；返回允许合并的记录，拒绝项已 finalize。

        返回 `None` 表示等待审查期间被取消或 deadline 到期：调用方必须
        保留待合并队列，不能把整批记录当成“审查拒绝”丢弃。
        """
        if (
            self.patch_reviewer is None
            or self.sandbox is None
            or not self.root
        ):
            return records
        limit = max(
            1,
            int(
                getattr(
                    self.settings,
                    "orchestrator_patch_review_concurrency",
                    2,
                )
                or 1
            ),
        )
        semaphore = asyncio.Semaphore(limit)
        review_timeout = self.review_timeout()
        read_timeout = min(30.0, review_timeout)
        aborted = {"_abort": True}

        async def decide(
            record: dict[str, Any],
        ) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
            outcome, patch_text = await self._await_with_cancel(
                lambda: self._read_patch_text(record),
                timeout=read_timeout,
            )
            if outcome in {"cancelled", "deadline"}:
                return record, aborted, ""
            if outcome != "ok" or not patch_text:
                return record, None, ""
            async with semaphore:
                outcome, verdict = await self._await_with_cancel(
                    lambda: self.patch_reviewer(
                        record, patch_text, self.config
                    ),
                    timeout=review_timeout,
                )
            if outcome in {"cancelled", "deadline"}:
                return record, aborted, patch_text
            # 超时/异常与现有语义一致：审查没给出结论时不阻塞合并。
            if outcome != "ok" or not isinstance(verdict, dict):
                return record, None, patch_text
            return record, verdict, patch_text

        # 审查是 LLM 调用；并发执行避免把合并尾部拉长。finalize 仍在
        # 主协程里顺序执行，避免并发改写共享的 status / 事件。
        outcomes = await asyncio.gather(*(decide(record) for record in records))
        if any(
            isinstance(verdict, dict) and verdict.get("_abort")
            for _, verdict, _ in outcomes
        ):
            return None
        approved: list[dict[str, Any]] = []
        for record, verdict, patch_text in outcomes:
            if verdict is None:
                approved.append(record)
                continue
            # 即使 reviewer 实现没有做校验，这里也再过滤一次：路径/行号对不上
            # 补丁的意见不能触发重跑，否则一次幻觉就是一轮 implement 成本。
            issues, _dropped = validate_review_issues(
                [
                    item
                    for item in (verdict.get("issues") or [])
                    if isinstance(item, dict)
                ],
                record,
                patch_text,
            )
            blocking = [
                item
                for item in issues
                if str(item.get("severity", "")) == "blocking"
            ]
            if not blocking:
                approved.append(record)
                continue
            rendered = [
                (
                    f"[patch-review] {item.get('path', '')}:"
                    f"{int(item.get('line', 0) or 0)} "
                    f"{item.get('message', '')}"
                ).strip()
                for item in issues[:8]
            ]
            record["status"] = "failed"
            record["merged"] = False
            record["merge_error"] = (
                "补丁审查未通过："
                + ("；".join(rendered[:3]) or "存在阻塞问题")
            )
            record["findings"] = [
                *(record.get("findings") or []),
                *rendered,
            ]
            # 交给 finalize：在重试额度内会带 findings 重新派发。
            self.finalize(record)
        return approved

    async def _flush_approved(
        self, records: list[dict[str, Any]]
    ) -> bool:
        approved = await self._review_patches(records)
        if approved is None:
            # 取消/deadline：记录仍在 merge_queue，交给取消收尾路径处理。
            return False
        rejected = [
            record
            for record in records
            if all(
                str(record.get("task_id", ""))
                != str(item.get("task_id", ""))
                for item in approved
            )
        ]
        if rejected:
            self.merge_queue.drop(rejected)
        return await self.merge_queue.flush_records(
            approved,
            sandbox=self.sandbox,
            root=self.root,
            allow_overlap=self.settings.orchestrator_allow_overlap,
            finalize=self.finalize,
        )

    async def flush_merges(self, *, force_all: bool = False) -> None:
        incremental = bool(
            getattr(self.settings, "orchestrator_incremental_merge", False)
            and not self.settings.orchestrator_allow_overlap
        )
        if incremental:
            # 即使本轮走整批 flush，也把路径缓存下来供 merged_paths 使用。
            await self._ensure_merge_paths()
        incremental_batch = bool(
            incremental and not force_all and self.in_flight
        )
        records: list[dict[str, Any]] = []
        if not incremental:
            # 旧行为：没有 implement 在飞才 flush。
            if not force_all and self.implement_active():
                return
            records = list(self.merge_queue.pending)
            merged_any = await self._flush_approved(records)
        elif force_all or not self.in_flight:
            records = list(self.merge_queue.pending)
            merged_any = await self._flush_approved(records)
        else:
            records = self.merge_queue.take_mergeable(
                blocked_paths=self._inflight_paths(),
                paths_by_task=self.merge_paths,
            )
            if not records:
                return
            merged_any = await self._flush_approved(records)
        if records:
            merged_ids = sorted(
                str(record.get("task_id", ""))
                for record in records
                if record.get("merged")
            )
            self.merge_batches.append(
                {
                    "tasks": sorted(
                        str(record.get("task_id", ""))
                        for record in records
                    ),
                    "merged": merged_ids,
                    "incremental": incremental_batch,
                }
            )
            self.merge_batches = self.merge_batches[-20:]
        if merged_any:
            for record in self.results:
                if (
                    record.get("role") != "implement"
                    or not record.get("merged")
                ):
                    continue
                record_task_id = str(record.get("task_id", ""))
                self.merged_paths.update(
                    self.merge_paths.get(record_task_id, [])
                )
                self.merged_paths.update(
                    str(path)
                    for path in (record.get("files") or [])
                    if str(path)
                )
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
            task_id = str(task["id"])
            if self.status.get(task_id, "pending") == "pending":
                self.status[task_id] = pending_status
                if pending_status == "blocked":
                    task["blocked_reason"] = abandon_reason
                    self.emit_blocked(task_id, abandon_reason)
        self.discard_merges(discard_reason)
        await self.abandon_inflight(
            reason=abandon_reason, timed_out=timed_out
        )

    def _ready_is_stale(self, task_id: str) -> bool:
        return (
            self.status.get(task_id, "pending") != "pending"
            or task_id in self.in_flight
        )

    def _forget_ready(self, task_id: str) -> None:
        self.queued.discard(task_id)
        self.ready_at.pop(task_id, None)

    def _block_role_budget(self, task: dict[str, Any]) -> None:
        task_id = str(task["id"])
        role = str(task.get("role", ""))
        self.status[task_id] = "blocked"
        task["blocked_reason"] = f"{role} 角色 token 预算已耗尽"
        self.emit_blocked(task_id, str(task["blocked_reason"]))
        self._forget_ready(task_id)

    def _scan_ready(
        self,
    ) -> tuple[
        tuple[tuple[int, int, str], dict[str, Any], int] | None,
        list[tuple[int, int, str]],
        bool,
        bool,
    ]:
        self.budget_guard.refresh_tokens(
            spent=self.budget.spent, reserved=self.budget.reserved
        )
        chosen: (
            tuple[tuple[int, int, str], dict[str, Any], int] | None
        ) = None
        deferred: list[tuple[int, int, str]] = []
        stage_deferred = False
        budget_blocked = False
        while self.ready:
            entry = heapq.heappop(self.ready)
            task_id = entry[2]
            if self._ready_is_stale(task_id):
                self._forget_ready(task_id)
                continue
            task = self.by_id.get(task_id)
            if task is None:
                self._forget_ready(task_id)
                continue
            if not self.stage_ready(task):
                stage_deferred = True
                deferred.append(entry)
                continue
            if self.role_budget_exhausted(task):
                self._block_role_budget(task)
                continue
            if not self.role_available(task):
                deferred.append(entry)
                continue
            if (
                str(task.get("role", "")) == "implement"
                and self.implement_backpressure()
            ):
                deferred.append(entry)
                continue
            reserve = self.estimate_tokens(task)
            if self.budget.can_reserve(reserve):
                self._forget_ready(task_id)
                chosen = (entry, task, reserve)
                break
            budget_blocked = True
            deferred.append(entry)
        return chosen, deferred, stage_deferred, budget_blocked

    def _budget_really_exhausted(self) -> bool:
        # 真的耗尽 = 即便释放所有只读预留也放不下任何 ready 任务。
        # 否则先等在飞任务结算（实际用量常低于预留），避免过早止损。
        reserves = [
            self.estimate_tokens(self.by_id[task_id])
            for _entry, _order, task_id in self.ready
            if task_id in self.by_id
        ]
        min_reserve = min(reserves) if reserves else 0
        available = (
            max(0, self.budget.limit - self.budget.spent)
            if self.budget.enabled
            else 0
        )
        return not self.in_flight or min_reserve > available

    def _handle_no_candidate(
        self, *, stage_deferred: bool, budget_blocked: bool
    ) -> None:
        if budget_blocked and self._budget_really_exhausted():
            self.token_budget_exhausted = True
            # 停派发；只读在飞取消，implement 跑完并照常合并。
            self.stop = True
            self._cancel_readonly_inflight()
            self._block_pending("token 预算耗尽，未派发")
            self._record_budget_abort("token 预算不足以启动剩余任务")
            return
        if (
            self.in_flight
            or self.implement_backpressure()
            or stage_deferred
        ):
            return
        if self.budget.enabled:
            self.token_budget_exhausted = True
        self._block_pending("token 预算不足以启动任何剩余任务")
        if self.budget.enabled:
            self._record_budget_abort("token 预算不足以启动任何剩余任务")

    def _launch_candidate(
        self,
        chosen: tuple[tuple[int, int, str], dict[str, Any], int],
    ) -> None:
        entry, task, reserve = chosen
        task_id = str(task["id"])
        if not self.budget.reserve(task_id, reserve):
            heapq.heappush(self.ready, entry)
            self.queued.add(task_id)
            return
        # 预留是硬切片的兜底值；run_one 会优先用任务显式预算或角色总预算。
        self.task_budget[task_id] = reserve if self.budget.enabled else 0
        self.attempts[task_id] = self.attempts.get(task_id, 0) + 1
        self.queued_at[task_id] = time.monotonic()
        future = asyncio.create_task(self.run_one(task))
        self.in_flight[task_id] = future
        self.future_task[future] = task_id

    def dispatch_ready(self, now: float) -> None:
        while (
            self.ready
            and not self.stop
            and len(self.in_flight) < self.rate_limit.current_limit
            and self.rate_limit.can_launch(now)
        ):
            chosen, deferred, stage_deferred, budget_blocked = (
                self._scan_ready()
            )
            for entry in deferred:
                heapq.heappush(self.ready, entry)

            if chosen is None:
                self._handle_no_candidate(
                    stage_deferred=stage_deferred,
                    budget_blocked=budget_blocked,
                )
                return

            self._launch_candidate(chosen)

    async def drain_completed(
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
            # 仅实现任务需要算"完成 → 合并"的等待；finalize 负责消费并移除。
            record["finished_at"] = time.monotonic()
            self.budget.settle(task_id, record)
            self.budget_guard.consume(record)
            self.detect_repeat_actions(record)
            self.task_budget.pop(task_id, None)
            if record.get("status") in {"success", "partial"}:
                await asyncio.to_thread(
                    self.estimation_store.record,
                    sample_from_result(
                        record,
                        task_type=self.estimation_task_type,
                        model=self.role_models.get(
                            str(record.get("role", "")),
                            self.estimation_model,
                        ),
                    ),
                )
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
                # 重试可能换了补丁路径；旧缓存不能用于新记录。
                self.merge_paths.pop(
                    str(record.get("task_id", "")), None
                )
                self.merge_queue.add(record)
            else:
                self.finalize(record)
            await self._journal_completed(record)
            self.completed_since_boundary += 1
            document_boundary = self.is_document_boundary(record)
            if (
                document_boundary
                or self.completed_since_boundary
                >= self.checkpoint_every
            ):
                self.stop = True

    def _record_gate(
        self, stage: int, status: str, reason: str
    ) -> None:
        self.wave_gates = [
            item
            for item in self.wave_gates
            if int(item.get("stage", 0) or 0) != stage
        ]
        self.wave_gates.append(
            {"stage": stage, "status": status, "reason": reason}
        )
        self.emit(
            {
                "type": "wave_gate",
                "stage": stage,
                "status": status,
                "reason": reason,
            }
        )

    def _block_after_stage(self, stage: int, reason: str) -> None:
        for task in self.tasks:
            task_id = str(task["id"])
            try:
                task_stage = int(task.get("stage", 0) or 0)
            except (TypeError, ValueError):
                task_stage = 0
            if task_stage <= stage:
                continue
            if self.status.get(task_id, "pending") != "pending":
                continue
            self.status[task_id] = "blocked"
            task["blocked_reason"] = reason
            self.emit_blocked(task_id, reason)
        self.stop = True

    async def _check_wave_gates(self) -> bool:
        """接口优先波次的 gate：上一波全部成功并合并后，先验证再放开下一波。"""
        if not self.interface_first:
            return True
        waves = sorted(
            (
                wave
                for wave in self.change_contract.get("waves") or []
                if isinstance(wave, dict)
            ),
            key=lambda item: int(item.get("stage", 0) or 0),
        )
        last_stage = (
            int(waves[-1].get("stage", 0) or 0) if waves else -1
        )
        passed = {
            int(item.get("stage", 0) or 0)
            for item in self.wave_gates
            if item.get("status") in {"passed", "skipped"}
        }
        for wave in waves:
            stage = int(wave.get("stage", 0) or 0)
            if stage in passed:
                continue
            tasks = [
                task
                for task in self.tasks
                if str(task.get("role", "")) == "implement"
                and int(task.get("stage", 0) or 0) == stage
            ]
            if not tasks:
                self._record_gate(stage, "passed", "该波次没有 implement")
                continue
            statuses = [
                str(self.status.get(str(task["id"]), "pending"))
                for task in tasks
            ]
            if any(
                value in {"failed", "blocked", "cancelled"}
                for value in statuses
            ):
                self._record_gate(stage, "failed", "该波次未成功")
                self._block_after_stage(stage, "上一波次未成功，已停止后续波次")
                return False
            if any(value in {"pending", "merging"} for value in statuses):
                return True
            # 最后一波由主图完整 verify 兜底，不重复跑。
            if stage == last_stage:
                self._record_gate(
                    stage, "skipped", "最后一波由主图 verify"
                )
                continue
            tests = [
                str(item) for item in (wave.get("gate_tests") or [])
            ]
            if not tests:
                # 没有受影响测试时不要退化成全量套件。
                self._record_gate(stage, "skipped", "无 gate_tests")
                continue
            if self.sandbox is None or not self.root:
                self._record_gate(stage, "skipped", "无沙箱，跳过 gate")
                continue
            verify_timeout = int(
                getattr(self.settings, "verify_timeout", 0) or 0
            )
            outcome, payload = await self._await_with_cancel(
                lambda tests=tuple(tests), verify_timeout=verify_timeout: (
                    asyncio.to_thread(
                        run_verification,
                        self.sandbox,
                        self.root,
                        allow_manifest=False,
                        only=tests,
                        timeout=verify_timeout or None,
                    )
                ),
                timeout=self.gate_timeout(),
            )
            if outcome in {"cancelled", "deadline"}:
                reason = (
                    "用户取消"
                    if outcome == "cancelled"
                    else "编排 deadline 到期"
                )
                self._record_gate(
                    stage, "cancelled", f"波次 gate 等待期间{reason}"
                )
                self._block_after_stage(
                    stage, f"波次 {stage} gate 等待期间{reason}，已停止后续波次"
                )
                return False
            if outcome == "timeout":
                self._record_gate(stage, "failed", "波次 gate 验证超时")
                self._block_after_stage(
                    stage, f"波次 {stage} gate 验证超时，已停止后续波次"
                )
                return False
            if outcome == "error":
                exc = payload
                gate_status = "error"
                tests = [f"{type(exc).__name__}: {exc}"]
            else:
                result = payload
                gate_status = str(getattr(result, "status", ""))
            if gate_status == "ok" or gate_status in {
                "not_configured",
                "skipped",
            }:
                self._record_gate(
                    stage,
                    "passed",
                    "" if gate_status == "ok" else gate_status,
                )
                continue
            self._record_gate(
                stage, "failed", f"波次 gate 验证 {gate_status}"
            )
            self._block_after_stage(
                stage, f"波次 {stage} gate 未通过，已停止后续波次"
            )
            return False
        return True

    # ------------------------------------------------------------------
    # 主循环与结果
    # ------------------------------------------------------------------
    async def run(self) -> dict[str, Any]:
        if not self.tasks:
            return self._no_tasks_result()
        # 边界重入时，__init__ 可能已经把依赖失败的 pending 标成 blocked；
        # 补发事件（event_map 按 task_id 去重）。
        for task_id, value in self.status.items():
            if value == "blocked":
                task = self.by_id.get(task_id) or {}
                self.emit_blocked(
                    task_id,
                    str(
                        task.get("blocked_reason")
                        or "依赖任务失败或被阻塞"
                    ),
                )
        # 复用结果也要让事件层/审计层看到一次“完成”，否则 resume 后
        # 前端会少掉这些任务。token 已清零，不会污染本轮成本。
        for record in self.reused_records:
            self.emit({"type": "worker_finished", "result": record})
        # 上次运行被 kill 时 worktree 清理没机会执行；此刻还没有 implement
        # 在飞（`prune_orphan_worktrees` 会 rm -rf 整个目录），可以安全清残留。
        if (
            self.settings.orchestrator_write_enabled
            and self.sandbox is not None
            and self.root
            and not self.worktrees_pruned
        ):
            from ubismart.orchestrator.implement import (
                prune_orphan_worktrees,
            )

            await asyncio.to_thread(
                prune_orphan_worktrees, self.sandbox, self.root
            )
            self.worktrees_pruned = True
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

            # 预算耗尽只停止“新派发”；在飞任务仍按原有语义收尾：
            # 只读被取消，implement 跑完并合并。真正的 wall deadline 由下面
            # asyncio.wait 的超时分支统一 abort。
            self.check_budget()
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

            await self.drain_completed(done)
            self.check_budget()

            # 增量开启时这里会合并与在飞任务无交集的已完成补丁；
            # 关闭时内部仍退回"无在飞 implement 才 flush"。
            await self.flush_merges()
            if not await self._check_phase_gates():
                break
            if not await self._check_wave_gates():
                break
            if self.stop and not self.in_flight:
                break

        if self.merge_queue:
            if self.cancelled() or self.implement_active():
                self.discard_merges("同批 implement 未完成，未合并")
            else:
                await self.flush_merges(force_all=True)

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
                    task = self.by_id.get(task_id) or {}
                    task["blocked_reason"] = "依赖未满足或收尾时仍不可派发"
                    self.emit_blocked(
                        task_id, str(task["blocked_reason"])
                    )

        if self.stop and self.budget_abort:
            # 在飞任务可能在停止派发之后才完成；收尾时刷新一次，保证
            # “已完成部分”包含它们。
            self._record_budget_abort(
                str(self.budget_abort.get("reason") or "预算耗尽")
            )

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
            "worktrees_pruned": self.worktrees_pruned,
            "merged_paths": sorted(self.merged_paths),
            "merge_batches": self.merge_batches,
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
            "budget_snapshot": self.budget_status().snapshot.as_dict(),
            "budget_abort": dict(self.budget_abort),
            "change_contract": self.change_contract,
            "wave_gates": self.wave_gates,
            "control_checkpoints": self.control_checkpoints,
            "loop_abort": dict(self.loop_abort),
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

