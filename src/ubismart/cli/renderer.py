"""CLI 领域事件渲染器（表驱动）。

事件类型 → handler 的映射放在这里，`cli/app.py` 不再维护长 if/elif。
输出文本与旧的 `_make_renderer` 保持一致。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from rich.console import Console
from rich.markup import escape

from ubismart.events import (
    AssistantToken,
    CostReport,
    FileChanged,
    OrchestrationFinished,
    PlanCreated,
    PlanRevised,
    RepairStarted,
    ReviewFinished,
    RunFailed,
    RunFinished,
    StepFinished,
    StepStarted,
    SubAgentFinished,
    SubAgentStarted,
    TaskGraphCreated,
    ToolCallFinished,
    ToolCallStarted,
    Verification,
    WorkerFinished,
    WorkerStarted,
    WorkflowSelected,
)
from ubismart.protocol.terminal_text import sanitize_terminal_text
from ubismart.session.costs import CurrencyConfig


def _safe(value: object) -> str:
    return escape(sanitize_terminal_text(value))


class CliRenderer:
    def __init__(
        self,
        console: Console,
        emit: Callable[[str], None],
        render_plan: Callable[[list[str], int], None],
        currency: CurrencyConfig | None = None,
    ) -> None:
        self.console = console
        self.emit = emit
        self.render_plan = render_plan
        self.currency = currency or CurrencyConfig()
        self.state: dict[str, Any] = {
            "plan": [],
            "total": 1,
            "answering": False,
        }
        self._handlers: dict[type, Callable[[Any], None]] = {
            PlanCreated: self._plan_created,
            PlanRevised: self._plan_revised,
            StepStarted: self._step_started,
            StepFinished: self._step_finished,
            AssistantToken: self._assistant_token,
            ToolCallStarted: self._tool_started,
            SubAgentStarted: self._subagent_started,
            SubAgentFinished: self._subagent_finished,
            WorkflowSelected: self._workflow_selected,
            TaskGraphCreated: self._task_graph_created,
            WorkerStarted: self._worker_started,
            WorkerFinished: self._worker_finished,
            OrchestrationFinished: self._orchestration_finished,
            RepairStarted: self._repair_started,
            ToolCallFinished: self._tool_finished,
            FileChanged: self._file_changed,
            Verification: self._verification,
            ReviewFinished: self._review_finished,
            RunFailed: self._run_failed,
            CostReport: self._cost_report,
            RunFinished: self._run_finished,
        }

    def render(self, event: Any) -> None:
        handler = self._handlers.get(type(event))
        if handler is not None:
            handler(event)

    def _plan_created(self, event: PlanCreated) -> None:
        self.state["plan"] = list(event.steps)
        self.state["total"] = max(len(event.steps), 1)
        if event.steps:
            self.render_plan(event.steps, 0)
            self.console.print("\n[dim]开始执行…[/]")
        if event.degraded:
            # 不能静默：多步任务退化成单步，用户会以为它真的做了规划
            self.console.print(
                "[yellow]! 规划未解析，已退化为单步执行"
                "（模型未返回可解析的步骤，请检查模型输出）[/]"
            )

    def _plan_revised(self, event: PlanRevised) -> None:
        # 执行中重建了剩余计划：先把新计划摆出来，再继续往下走
        self.state["plan"] = list(event.steps)
        self.state["total"] = max(len(event.steps), 1)
        self.console.print("\n[yellow]↻ 计划已调整[/]")
        if event.steps:
            self.render_plan(event.steps, event.step_idx)

    def _step_started(self, event: StepStarted) -> None:
        # 单步计划下标题只是重复用户问题，省略
        if event.total > 1:
            head = f"第 {event.index + 1}/{event.total} 步"
            self.console.print(f"\n[cyan]{head}[/] {_safe(event.text)}")
        self.console.print("[bold green]助手 › [/]", end="")

    def _step_finished(self, event: StepFinished) -> None:
        if event.budget_exhausted:
            suffix = "[dim]（工具预算耗尽）[/]"
        elif event.cancelled:
            # 被重规划放弃的步骤：不能说成「预算耗尽」——它根本没试过
            suffix = "[dim]（本步骤未执行完，已放弃）[/]"
        else:
            suffix = ""
        self.emit(f"\n{suffix}\n")

    def _assistant_token(self, event: AssistantToken) -> None:
        # respond 与 act 说的内容常有重叠（架构上就是「每步小结 + 最终汇总」）。
        # 给最终答复一个明确的起点，读者才不会以为是重复输出。
        if event.node == "respond" and not self.state["answering"]:
            self.state["answering"] = True
            self.console.print("\n[dim]── 最终答复 ──[/]")
        self.emit(event.text)

    def _tool_started(self, event: ToolCallStarted) -> None:
        label = f"[{event.level}] " if event.level else ""
        self.console.print(
            f"\n[dim]→ {escape(label)}{_safe(event.name)}: "
            f"{_safe(event.summary)}[/]"
        )

    def _subagent_started(self, event: SubAgentStarted) -> None:
        self.console.print(
            f"[cyan]◈ 探索子智能体开始[/] [dim]{_safe(event.goal)}[/]"
        )

    def _subagent_finished(self, event: SubAgentFinished) -> None:
        style = "green" if event.status == "success" else "yellow"
        self.console.print(
            f"[{style}]◈ 探索子智能体 {_safe(event.status)}[/] "
            f"[dim]{_safe(event.summary)}[/]"
        )

    def _workflow_selected(self, event: WorkflowSelected) -> None:
        self.console.print(
            f"[cyan]◈ 工作流：{_safe(event.workflow)}[/] "
            f"[dim]{_safe(event.reason)}[/]"
        )

    def _task_graph_created(self, event: TaskGraphCreated) -> None:
        suffix = " [yellow]（已降级）[/]" if event.degraded else ""
        self.console.print(
            f"[cyan]◈ 任务图：{len(event.tasks)} 个只读任务[/]{suffix}"
        )
        for task in event.tasks:
            self.console.print(
                f"  [dim]{escape(str(task.get('id', '')))} "
                f"({escape(str(task.get('role', '')))}): "
                f"{escape(str(task.get('goal', '')))}[/]"
            )

    def _worker_started(self, event: WorkerStarted) -> None:
        self.console.print(
            f"[dim]  → worker {_safe(event.role)}/{_safe(event.task_id)}: "
            f"{_safe(event.goal)}[/]"
        )

    def _worker_finished(self, event: WorkerFinished) -> None:
        style = (
            "green"
            if event.status == "success"
            else "yellow"
            if event.status == "partial"
            else "red"
        )
        self.console.print(
            f"[{style}]  ← worker {_safe(event.role)}/{_safe(event.task_id)} "
            f"{_safe(event.status)}[/] [dim]{_safe(event.summary)}[/]"
        )

    def _orchestration_finished(
        self, event: OrchestrationFinished
    ) -> None:
        self.console.print(
            f"[cyan]◈ 编排完成[/] [dim]成功 {event.completed}，"
            f"失败/阻塞 {event.failed}：{_safe(event.summary)}[/]"
        )

    def _repair_started(self, event: RepairStarted) -> None:
        self.console.print(
            f"[yellow]↻ 第 {event.attempt}/{event.limit} 次修复[/] "
            f"[dim]{_safe(event.summary)}[/]"
        )

    def _tool_finished(self, event: ToolCallFinished) -> None:
        if event.decision == "denied":
            self.console.print("[red]← 已被用户拒绝[/]")
            return
        if event.rejected:
            self.console.print("[red]← 已被安全策略拒绝[/]")
            return
        if event.skipped:
            self.console.print("[dim]← 已跳过（环境未配置）[/]")
            return
        if event.interrupted:
            self.console.print("[yellow]← 结果未知（执行中断，未自动重放）[/]")
            return
        if event.ok:
            mark, style = "✓", "dim"
        elif event.exit_code is not None:
            mark, style = f"✗ exit={event.exit_code}", "yellow"
        else:
            mark, style = "✗", "yellow"
        elapsed = (
            f" {event.duration_ms / 1000:.2f}s"
            if event.duration_ms
            else ""
        )
        self.console.print(f"[{style}]← {mark}{elapsed}[/]")

    def _file_changed(self, event: FileChanged) -> None:
        stat = f"+{event.added} -{event.removed}"
        stamp = f" · {event.snapshot_id}" if event.snapshot_id else ""
        self.console.print(
            f"[magenta]✎ {_safe(event.path)}[/] [dim]{stat}{stamp}[/]"
        )

    def _verification(self, event: Verification) -> None:
        if event.status == "not_configured":
            self.console.print("[dim]验证：未检测到可用的测试命令[/]")
        elif event.status == "skipped":
            pass
        elif event.ok:
            self.console.print(
                f"[green]验证通过[/] [dim]{_safe(event.summary)}[/]"
            )
        else:
            self.console.print(
                f"[red]验证失败[/] [dim]{_safe(event.summary)}[/]"
            )
            for issue in event.issues[:5]:
                self.console.print(f"  [red]·[/] {escape(issue)}")

    def _review_finished(self, event: ReviewFinished) -> None:
        if event.status == "skipped":
            pass
        elif event.blocked:
            self.console.print(
                f"[red]代码审查未通过[/] [dim]{_safe(event.summary)}[/]"
            )
            for finding in event.findings[:5]:
                self.console.print(f"  [red]·[/] {escape(finding)}")
        elif event.status == "warned":
            self.console.print(
                f"[yellow]代码审查有告警[/] [dim]{_safe(event.summary)}[/]"
            )
        else:
            self.console.print(
                f"[green]代码审查通过[/] [dim]{_safe(event.summary)}[/]"
            )

    def _run_failed(self, event: RunFailed) -> None:
        self.console.print(f"\n[red]运行失败：{_safe(event.message)}[/]")

    def _cost_report(self, event: CostReport) -> None:
        line = f"成本 {self.currency.format(event.cost_usd)}"
        if event.budget_usd > 0:
            line += (
                f" / 预算 {self.currency.format(event.budget_usd, decimals=2)}"
                f"（{event.ratio:.0%}）"
            )
        if event.billed_usd:
            line += (
                f" / 账单 {self.currency.format(event.billed_usd)}"
                f"（差 {self.currency.format(event.billing_delta_usd)}）"
            )
        self.console.print(f"[dim]{escape(line)}[/]")
        if event.billing_error:
            self.console.print(
                f"[yellow]账单对账失败：{_safe(event.billing_error)}[/]"
            )
        for alert in event.alerts:
            self.console.print(f"[yellow]⚠ {escape(alert)}[/]")

    def _run_finished(self, event: RunFinished) -> None:
        self.emit("\n")
