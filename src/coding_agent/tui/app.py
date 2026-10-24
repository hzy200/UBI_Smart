"""Textual 终端界面。

只消费 AgentRuntime 产出的领域事件 —— 不认识 LangGraph，也不持有工具与沙箱
（见 tests/unit/test_architecture.py 的约束测试）。因此 TUI 无法绕过命令审批。
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable
from functools import partial
from typing import Any

from rich.markdown import Markdown
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Footer, Header, Input, RichLog, Static, TextArea

from coding_agent.audit import read_records_many
from coding_agent.config import Settings, get_settings
from coding_agent.events import (
    ApprovalRequested,
    AssistantToken,
    CostReport,
    Event,
    FileChanged,
    OrchestrationFinished,
    PlanCreated,
    PlanRevised,
    RepairStarted,
    ReviewFinished,
    RunFailed,
    RunFinished,
    RunStarted,
    SteeringApplied,
    StepFinished,
    StepStarted,
    SubAgentFinished,
    SubAgentStarted,
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
from coding_agent.runtime import AgentRuntime

HELP_TEXT = """\
/help              显示本帮助
/new               开始新会话（换 thread_id，清空时间线）
/clear             清空时间线
/workspace         显示当前工作区
/audit [n]         显示本会话最近 n 条审计记录（默认 10）
/snapshots [n]     列出文件快照（每次写入/编辑前的留底）
/diff [快照id|路径] 看某次改动之后文件变成了什么样
/undo [快照id|路径] 回滚到某次改动前（不带参数则回滚最近一次）
/sessions [n]      列出历史会话
/switch <会话id>   切换到某个历史会话（会载入它的对话记录）
/checkpoints [n]   列出当前会话的 checkpoint
/status            查看当前会话/运行状态
/cost              查看本轮成本与 token
/fork [检查点id]   从当前（或指定 checkpoint）分叉出新会话并切换过去
/rewind [n]        回退 n 个 checkpoint（默认 1；历史保留）
/memory            查看项目记忆
/remember <事实>   记下一条跨会话的项目事实
/forget <序号>     删除第 N 条记忆
/pause             在下一个节点边界暂停（不再派发新 worker）
/resume            从暂停处继续
/cancel            取消运行；未合并的改动不会落盘
/add [角色] <目标> 追加一个只读任务（explore/test/review）
/replan            要求重新规划剩余工作
/quit              退出

输入框支持 ↑/↓ 浏览历史、→ 接受斜杠命令补全；F2/Ctrl+J 打开多行输入。
快捷键  Ctrl+Q 退出 · Ctrl+N 新会话 · Ctrl+L 清屏
"""

# 快照 id 形如 20261003T024018123456-a1b2c3（时间戳精确到微秒）；不符合就当路径处理
_SNAPSHOT_ID_RE = re.compile(r"^\d{8}T\d{6,18}-[0-9a-f]{6}$")

# 输入补全只提示名字，参数仍由 /help 说明。
SLASH_COMMANDS = [
    "/help", "/new", "/clear", "/workspace", "/audit", "/snapshots",
    "/diff", "/undo", "/sessions", "/switch", "/fork", "/rewind",
    "/memory", "/remember", "/forget", "/pause", "/resume", "/cancel",
    "/add", "/replan", "/checkpoints", "/status", "/cost", "/quit",
]

# RichLog 的硬上限：长会话不必无限占内存；截断后仍能 /clear 重来。
MAX_TRANSCRIPT_LINES = 5_000


class PromptInput(Input):
    """带上下键历史的输入框。"""

    BINDINGS = [
        ("up", "history_previous", "上一条"),
        ("down", "history_next", "下一条"),
        ("f2,ctrl+j", "open_multiline", "多行编辑"),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._history: list[str] = []
        self._history_index = 0

    def remember(self, text: str) -> None:
        if not text or (self._history and self._history[-1] == text):
            self._history_index = len(self._history)
            return
        self._history.append(text)
        # 只保留最近 200 条，避免长会话无限增长。
        if len(self._history) > 200:
            del self._history[:-200]
        self._history_index = len(self._history)

    def action_history_previous(self) -> None:
        if not self._history:
            return
        self._history_index = max(0, self._history_index - 1)
        self.value = self._history[self._history_index]
        self.cursor_position = len(self.value)

    def action_history_next(self) -> None:
        if not self._history:
            return
        self._history_index = min(len(self._history), self._history_index + 1)
        self.value = (
            self._history[self._history_index]
            if self._history_index < len(self._history)
            else ""
        )
        self.cursor_position = len(self.value)

    def action_open_multiline(self) -> None:
        opener = getattr(self.app, "open_multiline_editor", None)
        if callable(opener):
            opener()


class ConfirmScreen(ModalScreen[bool]):
    """通用二次确认；Esc / n 都是取消。"""

    BINDINGS = [
        ("y", "answer(True)", "确认"),
        ("n", "answer(False)", "取消"),
        ("escape", "answer(False)", "取消"),
    ]

    CSS = """
    ConfirmScreen { align: center middle; }
    #confirm-box {
        width: 80%;
        max-width: 100;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    #confirm-message { height: auto; }
    .confirm-hint { color: $text-muted; }
    """

    def __init__(self, message: str, *, title: str = "请确认") -> None:
        super().__init__()
        self.message = message
        self.title_text = title

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(self.title_text, classes="confirm-hint")
            yield Static(self.message, id="confirm-message", markup=False)
            yield Static(
                "[y] 确认    [n] / Esc 取消", classes="confirm-hint"
            )

    def action_answer(self, confirmed: bool) -> None:
        self.dismiss(confirmed)


class MultilineScreen(ModalScreen[str | None]):
    """多行输入弹窗：Ctrl+Enter 提交，Esc 取消。"""

    BINDINGS = [
        ("ctrl+enter", "submit", "提交"),
        ("escape", "cancel", "取消"),
    ]

    CSS = """
    MultilineScreen { align: center middle; }
    #multiline-box {
        width: 90%;
        max-width: 120;
        height: 70%;
        border: thick $accent;
        background: $surface;
        padding: 1 2;
    }
    #multiline-hint { color: $text-muted; height: 1; }
    #multiline { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="multiline-box"):
            yield Static(
                "多行输入：Ctrl+Enter 提交，Esc 取消",
                id="multiline-hint",
                markup=False,
            )
            yield TextArea(id="multiline")

    def on_mount(self) -> None:
        self.query_one("#multiline", TextArea).focus()

    def action_submit(self) -> None:
        self.dismiss(self.query_one("#multiline", TextArea).text)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlanPanel(Static):
    """计划面板：显示子任务与当前进度。"""

    def render_plan(self, steps: list[str], current: int) -> None:
        if not steps:
            self.update(Text("（尚未规划）", style="dim"))
            return
        lines = Text()
        for i, step in enumerate(steps):
            if i < current:
                style, mark = "dim", "✓"
            elif i == current:
                style, mark = "bold cyan", "▶"
            else:
                style, mark = "dim", "·"
            lines.append(f" {mark} {i + 1}. {step}\n", style=style)
        self.update(lines)


class TaskPanel(Static):
    """任务图/worker 状态面板。"""

    _STATUS_STYLE = {
        "pending": "dim",
        "running": "bold cyan",
        "success": "green",
        "partial": "yellow",
        "failed": "red",
        "blocked": "red",
        "cancelled": "dim",
        "merging": "yellow",
    }

    def render_tasks(self, tasks: dict[str, dict[str, Any]]) -> None:
        if not tasks:
            self.update(Text("（尚未生成任务）", style="dim"))
            return
        lines = Text()
        for task_id, task in list(tasks.items())[:8]:
            status = str(task.get("status", "pending"))
            style = self._STATUS_STYLE.get(status, "")
            role = str(task.get("role", ""))
            attempt = int(task.get("attempt", 1) or 1)
            retry = f" #{attempt}" if attempt > 1 else ""
            lines.append(f" {task_id}", style="bold")
            lines.append(f" {role} ", style="dim")
            lines.append(f"{status}{retry}\n", style=style)
        if len(tasks) > 8:
            lines.append(f" … 还有 {len(tasks) - 8} 个\n", style="dim")
        self.update(lines)


class ApprovalScreen(ModalScreen[str]):
    """单条审批请求。

    返回 `approve` / `deny` / `approve_all` / `deny_all`。
    默认动作是拒绝（Esc 也是拒绝）—— 危险操作不该靠一次误触放行。
    """

    BINDINGS = [
        ("y", "answer('approve')", "批准"),
        ("n", "answer('deny')", "拒绝"),
        ("a", "answer('approve_all')", "全部批准"),
        ("d", "answer('deny_all')", "全部拒绝"),
        ("escape", "answer('deny')", "拒绝"),
    ]

    CSS = """
    ApprovalScreen { align: center middle; }
    #approval-box {
        width: 80%;
        max-width: 100;
        height: auto;
        border: thick $warning;
        background: $surface;
        padding: 1 2;
    }
    #approval-level { color: $warning; text-style: bold; }
    #approval-command { background: $boost; padding: 0 1; margin: 1 0; }
    #approval-hint { color: $text-muted; }
    """

    def __init__(
        self,
        request: ApprovalRequested,
        position: str = "",
        *,
        allow_all: bool = False,
    ) -> None:
        super().__init__()
        self.request = request
        self.position = position
        self.allow_all = allow_all

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-box"):
            yield Static(f"需要确认 {self.position}", id="approval-level")
            yield Static(self.request.command, id="approval-command")
            if self.request.reason:
                yield Static(f"理由：{self.request.reason}")
            yield Static(f"等级：{self.request.level}    工具：{self.request.tool}")
            if self.allow_all:
                hint = (
                    "[y] 批准  [n] / Esc 拒绝  "
                    "[a] 全部批准  [d] 全部拒绝"
                )
            else:
                hint = "[y] 批准    [n] / Esc 拒绝"
            yield Static(hint, id="approval-hint")

    def action_answer(self, decision: str) -> None:
        self.dismiss(decision)


class AgentTuiApp(App[None]):
    """终端原生编程智能体的 TUI。"""

    TITLE = "CodingAgent"

    CSS = """
    #body { height: 1fr; }
    #sidebar {
        width: 36;
        border-right: solid $panel;
        padding: 0 1;
    }
    .sidebar-title { text-style: bold; color: $accent; height: 1; }
    #session-info { color: $text-muted; height: auto; padding-bottom: 1; }
    #main { width: 1fr; }
    #transcript { height: 1fr; padding: 0 1; }
    #streaming-wrap { height: auto; max-height: 50%; padding: 0 1; }
    #streaming { height: auto; color: $text; }
    #prompt { dock: bottom; }
    #prompt.busy { border: tall $warning; }
    """

    BINDINGS = [
        ("ctrl+q", "quit", "退出"),
        ("ctrl+n", "new_session", "新会话"),
        ("ctrl+l", "clear_log", "清屏"),
    ]

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        workspace: str | None = None,
        allow_write: bool = False,
        runtime_factory: Callable[[], Any] | None = None,
    ) -> None:
        super().__init__()
        self._settings = settings
        self._workspace = workspace
        self._allow_write = allow_write
        self._runtime_factory = runtime_factory

        self._runtime: Any = None
        self._session_id = uuid.uuid4().hex[:8]
        self._plan: list[str] = []
        self._step_idx = 0
        self._total = 1
        self._stream: list[str] = []
        self._answering = False
        self._busy = False
        # steering pause 后图已挂起、busy 会变回 False；用独立标记阻止用户
        # 在暂停期间误开新任务。
        self._paused = False
        self._cost_usd = 0.0
        self._run_started: float | None = None
        self._workers_active = 0
        self._model = ""
        self._tasks: dict[str, dict[str, Any]] = {}
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_budget = 0.0
        self._cost_alerts: list[str] = []

    # ------------------------------------------------------------------
    # 组装
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("会话", classes="sidebar-title")
                yield Static("", id="session-info")
                yield Static("计划", classes="sidebar-title")
                yield PlanPanel(id="plan")
                yield Static("任务", classes="sidebar-title")
                yield TaskPanel(id="tasks")
            with Vertical(id="main"):
                yield RichLog(
                    id="transcript",
                    markup=False,
                    wrap=True,
                    highlight=False,
                    max_lines=MAX_TRANSCRIPT_LINES,
                )
                with VerticalScroll(id="streaming-wrap"):
                    yield Static(id="streaming")
        yield PromptInput(
            placeholder="输入指令，/help 查看命令…",
            id="prompt",
            suggester=SuggestFromList(SLASH_COMMANDS, case_sensitive=False),
        )
        yield Footer()

    def on_mount(self) -> None:
        self._plan_panel.render_plan([], 0)
        self._task_panel.render_tasks({})
        try:
            self._runtime = (
                self._runtime_factory()
                if self._runtime_factory
                else AgentRuntime(
                    self._settings or get_settings(),
                    workspace=self._workspace,
                    allow_write=self._allow_write,
                )
            )
            # production/ops profile 会在 runtime 里强制关闭写能力；界面必须
            # 显示实际值，不能继续按命令行传入的 --write 显示"可写"。
            self._allow_write = bool(
                getattr(self._runtime, "allow_write", self._allow_write)
            )
            workspace = self._runtime.workspace
        except Exception as exc:  # noqa: BLE001 - 环境问题要在界面里说清楚，而不是崩掉
            self._write(f"初始化失败：{type(exc).__name__}: {exc}", "red")
            self._write("请先运行 agent doctor 检查环境。", "dim")
            return

        settings = getattr(self._runtime, "settings", self._settings)
        self._model = str(
            getattr(settings, "deepseek_model", "") or ""
        )
        self.sub_title = f"会话 {self._session_id}"
        self._render_session_info(workspace)
        self._write("输入指令开始，/help 查看可用命令。", "dim")
        self.query_one("#prompt", Input).focus()
        self.set_interval(1.0, self._tick_status)

    async def on_unmount(self) -> None:
        """退出时释放 runtime 的 checkpoint/沙箱资源。"""
        close = getattr(self._runtime, "aclose", None)
        if callable(close):
            try:
                await close()
            except Exception:  # noqa: BLE001, S110 - 退出路径不再二次抛错
                pass

    def _tick_status(self) -> None:
        # 每秒只刷新「运行中」的耗时，避免无谓重绘。
        if self._run_started is not None:
            self._render_session_info()

    def _render_session_info(self, workspace: str | None = None) -> None:
        """把会话、工作区、权限放进侧栏。

        之前只写在 Header 的副标题里，容易被忽略 ——
        而"我现在到底有没有写权限"是每次操作前都该看得见的信息。
        """
        if workspace is None:
            workspace = getattr(self._runtime, "workspace", "") or ""
        # 与 `llm/prompts.py` 的权限叙述保持一致：只读会话**不是**什么都执行不了，
        # L2/L3 仍会拦下来问一次。写死「L0 只读」会让人以为变更类命令一律被拒。
        level = "L1 可写 · 变更需确认" if self._allow_write else "只读 · 变更需确认"
        self._session_workspace = workspace
        info = Text()
        info.append(f"{self._session_id}\n", style="")
        info.append(f"{workspace}\n", style="dim")
        info.append(level + "\n", style="bold yellow" if self._allow_write else "dim")
        info.append(f"模型 {self._model or '—'}\n", style="dim")
        if self._paused:
            state = "已暂停"
        elif self._busy:
            state = "执行中"
        else:
            state = "就绪"
        info.append(f"状态 {state}", style="yellow" if self._busy or self._paused else "dim")
        if self._run_started is not None:
            elapsed = max(0.0, time.monotonic() - self._run_started)
            info.append(f" · {elapsed:.0f}s", style="dim")
        info.append("\n")
        if self._cost_usd:
            info.append(f"本轮成本 ${self._cost_usd:.4f}\n", style="dim")
        if self._workers_active:
            info.append(f"活跃 worker {self._workers_active}\n", style="dim")
        self.query_one("#session-info", Static).update(info)

    def _refresh_subtitle(self) -> None:
        if self._busy:
            suffix = "（执行中…）"
        elif self._paused:
            suffix = "（已暂停）"
        else:
            suffix = ""
        self.sub_title = f"会话 {self._session_id}{suffix}"

    def _set_busy(self, value: bool) -> None:
        """忙碌状态要看得见：输入框描边变色，并在副标题上标出来。"""
        self._busy = value
        prompt = self.query_one("#prompt", Input)
        prompt.set_class(value, "busy")
        self._refresh_subtitle()
        self._render_session_info()

    @property
    def session_id(self) -> str:
        """当前会话的 thread_id。

        刻意不叫 thread_id —— Textual 的 App 自带同名属性（运行线程 id），
        覆盖它会同时踩坏两边。
        """
        return self._session_id

    @property
    def _plan_panel(self) -> PlanPanel:
        return self.query_one("#plan", PlanPanel)

    @property
    def _task_panel(self) -> TaskPanel:
        return self.query_one("#tasks", TaskPanel)

    @property
    def _transcript(self) -> RichLog:
        return self.query_one("#transcript", RichLog)

    # ------------------------------------------------------------------
    # 事件 → 界面
    # ------------------------------------------------------------------

    def _write(self, text: str, style: str = "") -> None:
        self._transcript.write(Text(text, style=style) if style else Text(text))

    def _render_stream(self) -> None:
        self.query_one("#streaming", Static).update(Text("".join(self._stream)))
        wrap = self.query_one("#streaming-wrap", VerticalScroll)
        wrap.scroll_end(animate=False)

    def _flush_stream(self) -> None:
        if not self._stream:
            return
        text = "".join(self._stream)
        self._stream.clear()
        # 助手输出按 Markdown 渲染，代码块自动高亮；普通文本行为不变。
        self._transcript.write(Markdown(text))
        self.query_one("#streaming", Static).update("")

    def _apply(self, event: Event) -> None:
        if isinstance(event, RunStarted):
            # 每轮运行的新起点：清掉上一轮遗留的流式缓冲与「最终答复」标记。
            # 不复位的话，同一会话的第二轮起 respond 的文本就不再打分隔标题，
            # 会和 act 的每步小结混成一片。
            self._flush_stream()
            self._answering = False
            self._plan = []
            self._step_idx = 0
            self._total = 1
            self._cost_usd = 0.0
            self._input_tokens = 0
            self._output_tokens = 0
            self._cost_budget = 0.0
            self._cost_alerts = []
            self._run_started = time.monotonic()
            self._workers_active = 0
            self._tasks.clear()
            self._plan_panel.render_plan([], 0)
            self._task_panel.render_tasks(self._tasks)
            self._render_session_info()

        elif isinstance(event, PlanCreated):
            self._plan = list(event.steps)
            self._step_idx = 0
            self._plan_panel.render_plan(self._plan, 0)
            if event.degraded:
                self._write("! 规划未解析，已退化为单步执行", "yellow")

        elif isinstance(event, PlanRevised):
            self._flush_stream()
            self._plan = list(event.steps)
            self._step_idx = event.step_idx
            self._total = max(len(self._plan), 1)
            self._plan_panel.render_plan(self._plan, event.step_idx)
            self._write("↻ 计划已调整", "yellow")

        elif isinstance(event, StepStarted):
            self._flush_stream()
            self._step_idx = event.index
            self._total = event.total
            self._plan_panel.render_plan(self._plan, event.index)
            if event.total > 1:
                self._write(f"第 {event.index + 1}/{event.total} 步  {event.text}", "cyan")

        elif isinstance(event, StepFinished):
            self._flush_stream()
            if event.budget_exhausted:
                self._write("（本步骤工具预算耗尽）", "yellow")
            elif event.cancelled:
                self._write("（本步骤未执行完，已放弃）", "yellow")

        elif isinstance(event, AssistantToken):
            # respond 与 act 的内容常有重叠（架构上就是「每步小结 + 最终汇总」），
            # 给最终答复一个明确起点，读者才不会当成重复输出
            if event.node == "respond" and not self._answering:
                self._answering = True
                self._flush_stream()
                self._write("── 最终答复 ──", "dim")
            self._stream.append(event.text)
            self._render_stream()

        elif isinstance(event, ToolCallStarted):
            self._flush_stream()
            level = f"[{event.level}] " if event.level else ""
            self._write(f"→ {level}{event.name}: {event.summary}", "dim")

        elif isinstance(event, ToolCallFinished):
            if event.decision == "denied":
                self._write("← 已被用户拒绝", "red")
            elif event.rejected:
                self._write("← 已被安全策略拒绝", "red")
            elif event.skipped:
                self._write("← 已跳过（环境未配置）", "dim")
            elif event.interrupted:
                self._write("← 结果未知（执行中断，未自动重放）", "yellow")
            elif event.ok:
                elapsed = f" {event.duration_ms / 1000:.2f}s" if event.duration_ms else ""
                self._write(f"← ✓{elapsed}", "dim")
            elif event.exit_code is not None:
                self._write(f"← ✗ exit={event.exit_code}", "yellow")
            else:
                self._write("← ✗", "yellow")

        elif isinstance(event, FileChanged):
            stamp = f" · 备份 {event.snapshot_id}" if event.snapshot_id else ""
            self._write(
                f"✎ {event.action} {event.path}  +{event.added} -{event.removed}{stamp}",
                "magenta",
            )

        elif isinstance(event, Verification):
            if event.status == "not_configured":
                self._write("验证：未检测到可用的测试命令", "dim")
            elif event.status == "skipped":
                pass
            elif event.ok:
                self._write(f"验证通过  {event.summary}", "green")
            else:
                self._write(f"验证失败  {event.summary}", "red")
                for issue in event.issues[:5]:
                    self._write(f"  · {issue}", "red")

        elif isinstance(event, ReviewFinished):
            if event.status == "skipped":
                pass
            elif event.blocked:
                self._write(f"代码审查未通过  {event.summary}", "red")
                for finding in event.findings[:5]:
                    self._write(f"  · {finding}", "red")
            elif event.status == "warned":
                self._write(f"代码审查有告警  {event.summary}", "yellow")
            else:
                self._write(f"代码审查通过  {event.summary}", "green")

        elif isinstance(event, SubAgentStarted):
            self._flush_stream()
            self._write(f"◈ 探索子智能体开始  {event.goal}", "cyan")

        elif isinstance(event, SubAgentFinished):
            self._flush_stream()
            style = "green" if event.status == "success" else "yellow"
            self._write(f"◈ 探索子智能体 {event.status}  {event.summary}", style)

        elif isinstance(event, WorkflowSelected):
            self._write(f"◈ 工作流：{event.workflow}  {event.reason}", "cyan")

        elif isinstance(event, TaskGraphCreated):
            self._tasks = {
                str(task.get("id", "")): {
                    "role": str(task.get("role", "")),
                    "goal": str(task.get("goal", "")),
                    "status": "pending",
                    "attempt": 1,
                }
                for task in event.tasks
            }
            self._task_panel.render_tasks(self._tasks)
            suffix = "（已降级）" if event.degraded else ""
            self._write(f"◈ 任务图：{len(event.tasks)} 个只读任务{suffix}", "cyan")
            for task in event.tasks:
                self._write(
                    f"  {task.get('id', '')} ({task.get('role', '')}): "
                    f"{task.get('goal', '')}",
                    "dim",
                )

        elif isinstance(event, TaskGraphExtended):
            for task in event.added:
                self._tasks[str(task.get("id", ""))] = {
                    "role": str(task.get("role", "")),
                    "goal": str(task.get("goal", "")),
                    "status": "pending",
                    "attempt": 1,
                }
            self._task_panel.render_tasks(self._tasks)
            self._write(
                f"＋ 动态扩展 {len(event.added)} 个只读任务", "cyan"
            )
            for task in event.added[:5]:
                self._write(
                    f"  {task.get('id', '')} ({task.get('role', '')}): "
                    f"{task.get('goal', '')}",
                    "dim",
                )
            if event.rejected:
                self._write(
                    f"  （拒绝 {len(event.rejected)} 个不合规提议）", "dim"
                )

        elif isinstance(event, WorkerStarted):
            retry = f"（第 {event.attempt} 次）" if event.attempt > 1 else ""
            task = self._tasks.setdefault(
                event.task_id,
                {"role": event.role, "goal": event.goal, "attempt": 1},
            )
            task["status"] = "running"
            task["attempt"] = event.attempt
            self._task_panel.render_tasks(self._tasks)
            self._workers_active += 1
            self._render_session_info()
            self._write(
                f"  → worker {event.role}/{event.task_id}{retry}: {event.goal}",
                "dim",
            )

        elif isinstance(event, WorkerFinished):
            style = (
                "green"
                if event.status == "success"
                else "yellow"
                if event.status == "partial"
                else "red"
            )
            task = self._tasks.setdefault(
                event.task_id,
                {"role": event.role, "goal": "", "attempt": 1},
            )
            task["status"] = event.status
            task["attempt"] = event.attempt
            self._task_panel.render_tasks(self._tasks)
            self._workers_active = max(0, self._workers_active - 1)
            self._render_session_info()
            merge_note = ""
            if event.merged:
                merge_note = "  · 已合并"
            elif event.merge_error:
                merge_note = f"  · 合并失败：{event.merge_error[:80]}"
            self._write(
                f"  ← worker {event.role}/{event.task_id} {event.status}"
                f"{merge_note}  {event.summary}",
                style,
            )

        elif isinstance(event, OrchestrationFinished):
            self._write(
                f"◈ 编排完成  成功 {event.completed}，失败/阻塞 {event.failed}  "
                f"{event.summary}",
                "cyan",
            )

        elif isinstance(event, RepairStarted):
            self._write(
                f"↻ 第 {event.attempt}/{event.limit} 次修复  {event.summary}", "yellow"
            )

        elif isinstance(event, SteeringApplied):
            self._flush_stream()
            if event.action == "pause":
                self._paused = True
                self._refresh_subtitle()
                self._render_session_info()
                self._write(
                    f"⏸ {event.detail or '已暂停'}（/resume 继续，/cancel 取消）",
                    "yellow",
                )
            elif event.action == "resume":
                self._paused = False
                self._refresh_subtitle()
                self._render_session_info()
                self._write("▶ 已恢复", "green")
            elif event.action == "cancel":
                self._paused = False
                self._refresh_subtitle()
                self._render_session_info()
                self._write(f"⛔ {event.detail or '已取消'}", "yellow")
            elif event.action == "add":
                self._write(f"＋ {event.detail or '已追加任务'}", "cyan")
            elif event.action == "replan":
                self._write(f"↻ {event.detail or '已请求重规划'}", "cyan")

        elif isinstance(event, WorkflowRevised):
            self._flush_stream()
            self._write(f"↻ 已重规划任务图：{event.reason}", "cyan")

        elif isinstance(event, ApprovalRequested):
            self._flush_stream()
            self._write(f"⏸ 需要确认（{event.level}）：{event.command}", "yellow")

        elif isinstance(event, RunFailed):
            self._flush_stream()
            self._run_started = None
            self._workers_active = 0
            self._render_session_info()
            self._write(f"运行失败：{event.message}", "red")

        elif isinstance(event, CostReport):
            self._flush_stream()
            self._cost_usd = float(event.cost_usd or 0.0)
            self._input_tokens = int(event.input_tokens or 0)
            self._output_tokens = int(event.output_tokens or 0)
            self._cost_budget = float(event.budget_usd or 0.0)
            self._cost_alerts = list(event.alerts)
            self._render_session_info()
            line = f"成本 ${event.cost_usd:.4f}"
            if event.budget_usd > 0:
                line += f" / 预算 ${event.budget_usd:.2f}（{event.ratio:.0%}）"
            if event.billed_usd:
                line += f" / 账单 ${event.billed_usd:.4f}（差 ${event.billing_delta_usd:+.4f}）"
            self._write(line, "dim")
            if event.billing_error:
                self._write(f"账单对账失败：{event.billing_error}", "yellow")
            for alert in event.alerts:
                self._write(f"⚠ {alert}", "yellow")

        elif isinstance(event, RunFinished):
            self._flush_stream()
            self._run_started = None
            self._workers_active = 0
            self._render_session_info()
            if (
                event.worker_input_tokens is not None
                or event.worker_output_tokens is not None
            ):
                self._write(
                    "worker token："
                    f"in {event.worker_input_tokens or 0} / "
                    f"out {event.worker_output_tokens or 0}",
                    "dim",
                )

    # ------------------------------------------------------------------
    # 执行
    # ------------------------------------------------------------------

    @work(exclusive=True)
    async def _execute(self, prompt: str) -> None:
        self._set_busy(True)
        try:
            await self._consume(self._runtime.run(prompt, thread_id=self._session_id))
        except Exception as exc:  # noqa: BLE001 - 任何异常都要显示出来而不是静默
            self._flush_stream()
            self._write(f"运行异常：{type(exc).__name__}: {exc}", "red")
        finally:
            self._set_busy(False)

    async def _consume(self, stream) -> None:
        """消费事件流；遇到审批挂起就弹窗收集答复后 resume，直到真正跑完。"""
        queue: list[ApprovalRequested] = []
        async for event in stream:
            self._apply(event)
            if isinstance(event, ApprovalRequested):
                queue.append(event)

        if queue:
            await self._resolve_approvals(queue)

    async def _resolve_approvals(
        self, queue: list[ApprovalRequested]
    ) -> None:
        """逐条（或批量）询问并 resume，直到没有新的审批请求。"""
        while queue:
            decisions: dict[str, bool] = {}
            for index, request in enumerate(queue, start=1):
                position = f"({index}/{len(queue)})" if len(queue) > 1 else ""
                answer = await self.push_screen_wait(
                    ApprovalScreen(
                        request,
                        position,
                        allow_all=len(queue) > 1,
                    )
                )
                if answer in {"approve_all", "deny_all"}:
                    granted = answer == "approve_all"
                    for remaining in queue[index - 1 :]:
                        decisions[remaining.request_id] = granted
                    break
                decisions[request.request_id] = answer == "approve"
            queue = []
            async for event in self._runtime.resume(self._session_id, decisions):
                self._apply(event)
                if isinstance(event, ApprovalRequested):
                    queue.append(event)

    @on(Input.Submitted)
    def _on_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        remember = getattr(event.input, "remember", None)
        if callable(remember):
            remember(text)
        self._submit_prompt(text)

    def _submit_prompt(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self._runtime is None:
            self._write("运行时未就绪，无法执行。", "red")
            return
        if text.startswith("/"):
            self._command(text)
            return
        if self._paused:
            self._write("（已暂停，请用 /resume 继续或 /cancel 取消）", "yellow")
            return
        if self._busy:
            self._write("（当前任务执行中，请等待完成）", "yellow")
            return

        self._write(f"你 › {text}", "bold cyan")
        self._execute(text)

    @work(exclusive=False)
    async def open_multiline_editor(self) -> None:
        if self._runtime is None:
            self._write("运行时未就绪，无法执行。", "red")
            return
        text = await self.push_screen_wait(MultilineScreen())
        if not text or not text.strip():
            return
        prompt = self.query_one("#prompt", PromptInput)
        prompt.remember(text.strip())
        self._submit_prompt(text)

    # ------------------------------------------------------------------
    # 斜杠命令
    # ------------------------------------------------------------------

    def _command(self, text: str) -> None:
        name, _, rest = text.partition(" ")
        name = name.lower()

        if name in {"/quit", "/exit", "/q"}:
            self.run_worker(self.action_quit())
        elif name == "/help":
            self._write(HELP_TEXT.rstrip(), "")
        elif name == "/clear":
            self._transcript.clear()
        elif name == "/new":
            self.action_new_session()
        elif name == "/workspace":
            self._write(f"工作区 {self._runtime.workspace}", "")
        elif name == "/audit":
            self._show_audit(rest)
        elif name == "/snapshots":
            self._show_snapshots(rest)
        elif name == "/diff":
            self._show_diff(rest)
        elif name == "/undo":
            self._undo(rest)
        elif name == "/sessions":
            self._show_sessions(rest)
        elif name == "/checkpoints":
            self._show_checkpoints(rest)
        elif name == "/status":
            self._show_status()
        elif name == "/cost":
            self._show_cost()
        elif name == "/switch":
            self._switch_session(rest.strip())
        elif name == "/fork":
            self._fork(rest.strip())
        elif name == "/rewind":
            self._rewind(rest.strip())
        elif name == "/memory":
            self._show_memory()
        elif name == "/remember":
            self._remember(rest)
        elif name == "/forget":
            self._forget(rest)
        elif name == "/pause":
            self._pause()
        elif name == "/resume":
            self._resume()
        elif name == "/cancel":
            self._cancel()
        elif name == "/add":
            self._add_task(rest)
        elif name == "/replan":
            self._replan()
        else:
            self._write(f"未知命令：{name}。输入 /help 查看可用命令。", "yellow")

    # ------------------------------------------------------------------
    # 实时干预（Phase 3D）
    # ------------------------------------------------------------------

    def _steer(self, action: str, **payload: Any) -> bool:
        """提交一条干预指令；返回是否成功入队。"""
        runtime = self._runtime
        if runtime is None or not hasattr(runtime, "steer"):
            self._write("当前运行时不支持实时干预。", "yellow")
            return False
        if not self._busy and not self._paused:
            self._write("当前没有正在运行的任务。", "yellow")
            return False
        try:
            runtime.steer(self._session_id, action, **payload)
        except Exception as exc:  # noqa: BLE001 - 审计/参数错误要显示而不是崩
            self._write(f"干预失败：{type(exc).__name__}: {exc}", "red")
            return False
        return True

    def _pause(self) -> None:
        if self._paused:
            self._write("已处于暂停状态。", "yellow")
            return
        if self._steer("pause"):
            self._write("⏸ 暂停请求已提交，将在当前节点结束后生效。", "yellow")

    def _resume(self) -> None:
        runtime = self._runtime
        if self._busy:
            self._write("（当前任务执行中，请等待完成）", "yellow")
            return
        paused = self._paused
        if not paused and runtime is not None and hasattr(runtime, "is_paused"):
            paused = bool(runtime.is_paused(self._session_id))
        if paused:
            if runtime is None or not hasattr(runtime, "resume_steering"):
                self._write("当前运行时不支持恢复。", "yellow")
                return
            self._paused = False
            self._refresh_subtitle()
            self._resume_stream()
            return
        # 不是 steering 暂停：尝试从 checkpoint 恢复审批挂起（跨进程场景）。
        if runtime is None or not hasattr(runtime, "pending_approvals"):
            self._write("当前没有处于暂停的任务。", "yellow")
            return
        self._resume_approvals()

    @work(exclusive=True)
    async def _resume_approvals(self) -> None:
        self._set_busy(True)
        try:
            pending = await self._runtime.pending_approvals(self._session_id)
            if not pending:
                self._write("当前没有可恢复的挂起任务。", "yellow")
                return
            self._write(
                f"发现 {len(pending)} 条挂起的审批请求，继续处理。", "yellow"
            )
            await self._resolve_approvals(pending)
        except Exception as exc:  # noqa: BLE001 - 恢复失败要显示
            self._flush_stream()
            self._write(f"恢复异常：{type(exc).__name__}: {exc}", "red")
        finally:
            self._set_busy(False)
            self._refresh_subtitle()

    @work(exclusive=True)
    async def _resume_stream(self) -> None:
        self._set_busy(True)
        try:
            await self._consume(
                self._runtime.resume_steering(self._session_id)
            )
        except Exception as exc:  # noqa: BLE001 - 恢复失败要显示
            self._flush_stream()
            self._write(f"恢复异常：{type(exc).__name__}: {exc}", "red")
        finally:
            self._set_busy(False)
            self._refresh_subtitle()

    def _cancel(self) -> None:
        if not self._busy and not self._paused:
            self._write("当前没有正在运行的任务。", "yellow")
            return
        if not self._steer("cancel"):
            return
        self._write("⛔ 取消请求已提交。", "yellow")
        # 暂停时图停在 interrupt 上，必须恢复一次才能让 cancel 被消费。
        if self._paused:
            self._resume()

    def _add_task(self, raw: str) -> None:
        text = raw.strip()
        if not text:
            self._write(
                "用法：/add [explore|test|review] <任务目标>", "yellow"
            )
            return
        role = "explore"
        parts = text.split(maxsplit=1)
        if parts[0] in {"explore", "test", "review"}:
            role = parts[0]
            text = parts[1].strip() if len(parts) > 1 else ""
        elif parts[0] == "implement":
            self._write(
                "/add 只支持只读任务（explore/test/review）；"
                "写任务请让 planner 生成。",
                "yellow",
            )
            return
        if not text:
            self._write("用法：/add [explore|test|review] <任务目标>", "yellow")
            return
        if self._steer("add", text=text, role=role):
            self._write(f"＋ 已追加 {role} 任务：{text}", "cyan")

    def _replan(self) -> None:
        if self._steer("replan"):
            self._write("↻ 重规划请求已提交。", "cyan")

    # ------------------------------------------------------------------
    # 会话与记忆
    # ------------------------------------------------------------------

    def _show_sessions(self, raw_limit: str) -> None:
        try:
            limit = max(int(raw_limit.strip() or 15), 1)
        except ValueError:
            self._write("用法：/sessions [条数]", "yellow")
            return

        found = self._runtime.sessions.list(limit=limit)
        if not found:
            self._write("没有历史会话记录。", "dim")
            return

        self._write(f"历史会话（最近 {len(found)} 个）", "dim")
        for info in found:
            marker = "▶" if info.thread_id == self._session_id else " "
            self._write(
                f" {marker} {info.thread_id}  {info.last_seen}  "
                f"{info.prompts} 次提问 / {info.tool_calls} 次工具  {info.title}",
                "cyan" if marker.strip() else "dim",
            )
        self._write("用 /switch <会话id> 切换过去。", "dim")

    def _show_checkpoints(self, raw_limit: str) -> None:
        try:
            limit = max(int(raw_limit.strip() or 10), 1)
        except ValueError:
            self._write("用法：/checkpoints [条数]", "yellow")
            return
        self._checkpoints_worker(limit)

    @work(exclusive=False)
    async def _checkpoints_worker(self, limit: int) -> None:
        try:
            items = await self._runtime.checkpoints(
                self._session_id, limit=limit
            )
        except Exception as exc:  # noqa: BLE001 - checkpoint 不存在要提示
            self._write(f"读取 checkpoint 失败：{exc}", "red")
            return
        if not items:
            self._write("当前会话还没有 checkpoint。", "dim")
            return
        self._write(f"checkpoint（最近 {len(items)} 个）", "dim")
        for item in items:
            self._write(
                f"  {item['checkpoint_id']}  step {item['step']}  "
                f"{item['preview']}",
                "dim",
            )

    def _show_status(self) -> None:
        workspace = getattr(self._runtime, "workspace", "") or ""
        if self._paused:
            state = "已暂停"
        elif self._busy:
            state = "执行中"
        else:
            state = "就绪"
        self._write(f"会话    {self._session_id}", "")
        self._write(f"工作区  {workspace}", "dim")
        self._write(
            f"权限    {'L1 可写' if self._allow_write else '只读'}", "dim"
        )
        self._write(f"模型    {self._model or '—'}", "dim")
        self._write(f"状态    {state}", "")
        if self._run_started is not None:
            elapsed = max(0.0, time.monotonic() - self._run_started)
            self._write(f"耗时    {elapsed:.0f}s", "dim")
        self._write(f"活跃 worker  {self._workers_active}", "dim")
        self._write(f"成本    ${self._cost_usd:.4f}", "dim")

    def _show_cost(self) -> None:
        if not self._cost_usd and not self._input_tokens and not self._output_tokens:
            self._write("本轮还没有收到成本报告。", "dim")
            return
        budget = (
            f" / 预算 ${self._cost_budget:.2f}"
            if self._cost_budget > 0
            else ""
        )
        self._write(f"本轮成本 ${self._cost_usd:.4f}{budget}", "")
        self._write(
            f"tokens  in {self._input_tokens} / out {self._output_tokens}",
            "dim",
        )
        for alert in self._cost_alerts:
            self._write(f"⚠ {alert}", "yellow")

    def _switch_session(self, thread_id: str) -> None:
        if not thread_id:
            self._write("用法：/switch <会话id>（用 /sessions 查看）", "yellow")
            return
        if self._busy:
            self._write("（当前任务执行中，无法切换会话）", "yellow")
            return
        self._load_session(thread_id)

    def _fork(self, checkpoint_id: str) -> None:
        if self._busy:
            self._write("（当前任务执行中，无法分叉会话）", "yellow")
            return
        self._fork_worker(checkpoint_id)

    @work(exclusive=True)
    async def _fork_worker(self, checkpoint_id: str) -> None:
        try:
            target = await self._runtime.fork(
                self._session_id, checkpoint_id=checkpoint_id
            )
        except Exception as exc:  # noqa: BLE001 - 分叉失败要提示而不是崩
            self._write(f"分叉失败：{exc}", "red")
            return
        self._write(f"已分叉到 {target}", "green")
        self._load_session(target)

    def _rewind(self, raw_steps: str) -> None:
        if self._busy:
            self._write("（当前任务执行中，无法回退会话）", "yellow")
            return
        try:
            steps = max(int(raw_steps.strip() or 1), 1)
        except ValueError:
            self._write("用法：/rewind [回退的 checkpoint 数]", "yellow")
            return
        self._rewind_worker(steps)

    @work(exclusive=True)
    async def _rewind_worker(self, steps: int) -> None:
        try:
            checkpoint = await self._runtime.rewind(
                self._session_id, steps=steps
            )
        except Exception as exc:  # noqa: BLE001
            self._write(f"回退失败：{exc}", "red")
            return
        self._write(f"已回退到新 checkpoint：{checkpoint}", "green")
        self._load_session(self._session_id)

    @work(exclusive=True)
    async def _load_session(self, thread_id: str) -> None:
        self._session_id = thread_id
        self._plan = []
        self._step_idx = 0
        self._stream.clear()
        self._answering = False
        self._cost_usd = 0.0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_budget = 0.0
        self._cost_alerts = []
        self._run_started = None
        self._workers_active = 0
        self._tasks.clear()
        self._plan_panel.render_plan([], 0)
        self._task_panel.render_tasks(self._tasks)
        self.query_one("#streaming", Static).update("")
        self._transcript.clear()

        self._render_session_info()
        is_paused = getattr(self._runtime, "is_paused", None)
        self._paused = bool(is_paused(thread_id)) if callable(is_paused) else False
        self._refresh_subtitle()

        try:
            history = await self._runtime.history(thread_id)
        except Exception as exc:  # noqa: BLE001 - 历史载入失败不该影响继续对话
            self._write(f"已切换到 {thread_id}（历史记录载入失败：{exc}）", "yellow")
            return

        if not history:
            self._write(f"已切换到会话 {thread_id}（暂无历史记录）", "dim")
            return

        self._write(f"已切换到会话 {thread_id}，{len(history)} 条历史记录：\n", "dim")
        for message in history:
            if message.role == "user":
                self._write(f"你 › {message.text}", "bold cyan")
            else:
                self._write(f"助手 › {message.text}", "green")
            self._write("")

    def _show_memory(self) -> None:
        facts = self._runtime.memories(scope="project")
        skipped = getattr(self._runtime.memory, "last_unsafe", [])
        if skipped:
            self._write(
                f"检测到 {len(skipped)} 条疑似密钥，它们不会注入模型。",
                "yellow",
            )
        if not facts:
            self._write("当前没有项目记忆。用 /remember <事实> 添加。", "dim")
            return
        self._write(f"项目记忆 {self._runtime.memory.path}（{len(facts)} 条）", "dim")
        for index, fact in enumerate(facts, start=1):
            self._write(f"  {index}. {fact}", "")

    def _remember(self, raw: str) -> None:
        text = raw.strip()
        if not text:
            self._write("用法：/remember <要记住的项目事实>", "yellow")
            return
        try:
            facts = self._runtime.remember(
                text, thread_id=self._session_id, reason="TUI /remember"
            )
        except Exception as exc:  # noqa: BLE001 - 上限/写盘失败要提示而不是崩
            self._write(str(exc), "red")
            return
        note = ""
        replaced = getattr(self._runtime.memory, "last_replaced", [])
        if replaced:
            note = "（已替换旧版本：" + "；".join(replaced) + "）"
        related = getattr(self._runtime.memory, "last_related", [])
        if related:
            note += (
                "（可能相关、未替换："
                + "；".join(related)
                + "；可用 agent memory --conflicts 查看）"
            )
        self._write(f"已记住（共 {len(facts)} 条）：{text}{note}", "green")

    def _forget(self, raw: str) -> None:
        try:
            index = int(raw.strip())
        except ValueError:
            self._write("用法：/forget <序号>（用 /memory 查看序号）", "yellow")
            return
        try:
            facts = self._runtime.forget(index, thread_id=self._session_id)
        except Exception as exc:  # noqa: BLE001 - 序号越界/写盘失败都要提示而不是崩
            self._write(str(exc), "red")
            return
        self._write(f"已删除第 {index} 条（剩余 {len(facts)} 条）", "green")

    # ------------------------------------------------------------------
    # 回滚相关命令
    # ------------------------------------------------------------------

    @staticmethod
    def _split_target(raw: str) -> tuple[str | None, str | None]:
        """把 /undo 的参数拆成 (snapshot_id, path)。"""
        target = raw.strip()
        if not target:
            return None, None
        if _SNAPSHOT_ID_RE.match(target):
            return target, None
        return None, target

    def _show_snapshots(self, raw_limit: str) -> None:
        try:
            limit = max(int(raw_limit.strip() or 15), 1)
        except ValueError:
            self._write("用法：/snapshots [条数]", "yellow")
            return

        entries = self._runtime.list_snapshots(limit=limit)
        if not entries:
            self._write("还没有任何快照。", "dim")
            return
        self._write(f"文件快照（最近 {len(entries)} 条）", "dim")
        for entry in entries:
            self._write(f"  {entry.snapshot_id}  {entry.path}", "dim")

    def _show_diff(self, raw: str) -> None:
        snapshot_id, path = self._split_target(raw)
        text = self._runtime.diff_snapshot(snapshot_id=snapshot_id, path=path)
        if not text:
            self._write("没有可显示的快照，或内容与快照一致。", "dim")
            return
        self._write_diff(text)

    def _undo(self, raw: str) -> None:
        snapshot_id, path = self._split_target(raw)
        target = snapshot_id or path or "最近一次改动"
        preview = ""
        try:
            preview = self._runtime.diff_snapshot(
                snapshot_id=snapshot_id, path=path
            )
        except Exception as exc:  # noqa: BLE001 - 预览失败不阻断确认流程
            preview = f"（预览失败：{type(exc).__name__}: {exc}）"
        lines = preview.splitlines()
        if len(lines) > 30:
            preview = "\n".join(lines[:30]) + f"\n…（共 {len(lines)} 行）"
        message = f"将回滚：{target}"
        if preview:
            message += "\n\n" + preview
        self.push_screen(
            ConfirmScreen(message, title="确认回滚？"),
            partial(self._do_undo, snapshot_id, path),
        )

    def _do_undo(
        self, snapshot_id: str | None, path: str | None, confirmed: bool
    ) -> None:
        if not confirmed:
            self._write("已取消回滚。", "dim")
            return
        result = self._runtime.restore(snapshot_id=snapshot_id, path=path)
        if not result.ok:
            self._write(result.message, "red")
            return
        self._write(result.message, "green")
        if result.undo_snapshot_id:
            self._write(f"回滚前的状态已留底：{result.undo_snapshot_id}", "dim")
        if result.diff:
            self._write_diff(result.diff)

    def _write_diff(self, raw: str) -> None:
        for line in raw.splitlines():
            if line.startswith(("+++", "---")):
                style = "bold"
            elif line.startswith("+"):
                style = "green"
            elif line.startswith("-"):
                style = "red"
            elif line.startswith("@@"):
                style = "cyan"
            else:
                style = ""
            self._write(line, style)

    def _show_audit(self, raw_limit: str) -> None:
        try:
            limit = max(int(raw_limit.strip() or 10), 1)
        except ValueError:
            self._write("用法：/audit [条数]", "yellow")
            return

        path = self._runtime.audit_path
        records = read_records_many(
            self._runtime.audit_files(), thread_id=self._session_id, limit=limit
        )
        if not records:
            self._write(f"本会话还没有审计记录：{path}", "dim")
            return

        self._write(f"审计日志 {path}（最近 {len(records)} 条）", "dim")
        for record in records:
            style = "red" if record.decision == "rejected" else "dim"
            target = record.tool or record.path or ""
            detail = f"{record.kind:<12} {target:<16} {record.level:<10} {record.decision}"
            self._write(f"  {record.ts[11:19]}  {detail}", style)

    async def action_quit(self) -> None:
        if self._busy or self._paused:
            confirmed = await self.push_screen_wait(
                ConfirmScreen(
                    "当前任务仍在执行或处于暂停状态。退出会中断本轮运行，"
                    "未合并的改动不会落盘。",
                    title="确认退出？",
                )
            )
            if not confirmed:
                return
        self.exit()

    def action_new_session(self) -> None:
        if self._busy or self._paused:
            self._write(
                "（当前任务执行中或已暂停，无法新建会话；"
                "请先 /cancel 或 /resume）",
                "yellow",
            )
            return
        self._session_id = uuid.uuid4().hex[:8]
        self._plan = []
        self._step_idx = 0
        self._stream.clear()
        self._answering = False
        self._paused = False
        self._cost_usd = 0.0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cost_budget = 0.0
        self._cost_alerts = []
        self._run_started = None
        self._workers_active = 0
        self._tasks.clear()
        self._plan_panel.render_plan([], 0)
        self._task_panel.render_tasks(self._tasks)
        self.query_one("#streaming", Static).update("")
        self._transcript.clear()
        if self._runtime is not None:
            self._render_session_info()
        self._refresh_subtitle()
        self._write(f"已开始新会话 {self._session_id}", "dim")

    def action_clear_log(self) -> None:
        self._transcript.clear()
