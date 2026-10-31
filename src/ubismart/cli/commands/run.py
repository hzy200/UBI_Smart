"""对话、运行、恢复与 steering 命令。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Any

import typer
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from rich.markup import escape

from coding_agent.audit import AuditError
from coding_agent.audit.logger import now_iso
from coding_agent.cli.context import AgentRuntime, console, get_settings
from coding_agent.cli.renderer import CliRenderer
from coding_agent.config import (
    PRODUCTION_PROFILES,
    Settings,
    apply_security_profile,
)
from coding_agent.events import ApprovalRequested, Event
from coding_agent.llm.deepseek import build_llm
from coding_agent.llm.prompts import SYSTEM_PROMPT
from coding_agent.protocol.messages import text_of
from coding_agent.protocol.terminal_text import sanitize_terminal_text
from coding_agent.runtime import submit_steering_command
from coding_agent.session.steering import SteeringCommand


def _emit(text: str) -> None:
    # markup=False 很关键：模型输出里的 [..] 会被 rich 当成标记吞掉
    console.print(
        sanitize_terminal_text(text),
        end="",
        markup=False,
        highlight=False,
        soft_wrap=True,
    )

def _render_plan(steps: list[str], step_idx: int) -> None:
    console.print("[dim]计划：[/]")
    for i, step in enumerate(steps):
        if i < step_idx:
            style, box = "dim", "✓"
        elif i == step_idx:
            style, box = "cyan", "▶"
        else:
            style, box = "dim", "·"
        console.print(f"[{style}]  {box} {i + 1}. {escape(step)}[/]")

async def _chat_loop(settings: Settings) -> None:
    llm = build_llm(settings, streaming=True, task_type="chat")
    history: list[Any] = [SystemMessage(content=SYSTEM_PROMPT)]
    console.print("[dim]纯对话模式（无工具）。输入 exit 退出。[/]\n")

    while True:
        try:
            text = console.input("[bold cyan]你 › [/]")
        except (EOFError, KeyboardInterrupt):
            console.print()
            return
        if text.strip().lower() in {"exit", "quit", ":q"}:
            return
        if not text.strip():
            continue

        history.append(HumanMessage(content=text))
        console.print("[bold green]助手 › [/]", end="")
        buf: list[str] = []
        try:
            async for chunk in llm.astream(history):
                piece = text_of(chunk)
                if piece:
                    buf.append(piece)
                    _emit(piece)
        except KeyboardInterrupt:
            console.print("\n[dim]已中断[/]")
        _emit("\n\n")
        history.append(AIMessage(content="".join(buf)))

def chat() -> None:
    """纯流式对话，不走工具 —— 用于验证 API 联通。"""
    asyncio.run(_chat_loop(get_settings()))

def _make_renderer() -> Callable[[Event], None]:
    """把事件映射为终端输出。

    具体映射在 `cli/renderer.py` 的表驱动实现里；TUI / Web 各写一份自己的。
    """
    from coding_agent.session.costs import CurrencyConfig

    return CliRenderer(
        console,
        _emit,
        _render_plan,
        currency=CurrencyConfig.from_settings(get_settings()),
    ).render

def _is_danger_level(level: str) -> bool:
    return str(level).strip().startswith("L3")

def _ask_approval(requests: list[ApprovalRequested], *, assume_yes: bool) -> dict[str, bool]:
    """逐条询问审批结果。非交互环境下默认拒绝（fail closed）。

    `--yes` 只自动批准 L2；L3（危险）始终要求人工确认。
    """
    decisions: dict[str, bool] = {}
    for request in requests:
        console.print()
        console.print(f"[yellow bold]需要确认[/] [dim]({escape(request.level)})[/]")
        console.print(f"  [bold]{escape(request.command)}[/]")
        if request.reason:
            console.print(f"  [dim]理由：{escape(request.reason)}[/]")

        if assume_yes and not _is_danger_level(request.level):
            console.print("  [green]→ 已按 --yes 自动批准[/]")
            decisions[request.request_id] = True
            continue
        if assume_yes:
            console.print("  [yellow]→ L3 危险命令始终需要确认，--yes 不自动批准[/]")

        try:
            answer = console.input("  执行？[y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            console.print("\n  [red]→ 非交互环境，按拒绝处理[/]")
            decisions[request.request_id] = False
            continue
        decisions[request.request_id] = answer in {"y", "yes"}

    return decisions

async def _consume_stream(
    runtime: AgentRuntime,
    stream,
    thread_id: str,
    render: Callable[[Event], None],
    *,
    assume_yes: bool = False,
) -> None:
    """消费一轮事件；审批挂起时逐条询问并 resume，直到真正跑完。"""
    queue: list[ApprovalRequested] = []
    async for event in stream:
        render(event)
        if isinstance(event, ApprovalRequested):
            queue.append(event)

    # 图每次挂起后都要重新续跑，可能连续挂起多次
    while queue:
        decisions = _ask_approval(queue, assume_yes=assume_yes)
        queue = []
        async for event in runtime.resume(thread_id, decisions):
            render(event)
            if isinstance(event, ApprovalRequested):
                queue.append(event)

async def _run_events(
    settings: Settings,
    prompt: str,
    allow_write: bool,
    thread_id: str,
    workspace: str,
    *,
    assume_yes: bool = False,
    plan_only: bool = False,
) -> None:
    runtime = AgentRuntime(settings, workspace=workspace or None, allow_write=allow_write)
    kind = "SQLite 持久化" if runtime.persistent else "进程内"
    console.print(f"[dim]会话 {thread_id} · 工作区 {runtime.workspace} · checkpoint {kind}[/]\n")

    render = _make_renderer()
    try:
        await _consume_stream(
            runtime,
            runtime.run(prompt, thread_id=thread_id, plan_only=plan_only),
            thread_id,
            render,
            assume_yes=assume_yes,
        )
    except KeyboardInterrupt:
        console.print("\n[dim]已中断[/]")
    finally:
        await runtime.aclose()

REPL_HELP = """\
直接输入任务；工具调用与审批和 agent run 一致。
  /new        开始新会话
  /workspace  显示当前工作区
  /help       显示本帮助
  exit / quit 退出
"""

async def _repl_loop(
    settings: Settings,
    workspace: str,
    *,
    allow_write: bool,
    thread_id: str,
    assume_yes: bool = False,
) -> None:
    """带工具的交互式多轮会话。"""
    runtime = AgentRuntime(
        settings, workspace=workspace or None, allow_write=allow_write
    )
    kind = "SQLite 持久化" if runtime.persistent else "进程内"
    console.print(
        f"[dim]交互模式 · 工作区 {runtime.workspace} · checkpoint {kind}[/]"
    )
    console.print("[dim]输入 /help 查看命令，exit 退出。[/]\n")
    try:
        while True:
            try:
                text = console.input("[bold cyan]你 › [/]").strip()
            except (EOFError, KeyboardInterrupt):
                console.print()
                return
            if not text:
                continue
            lowered = text.lower()
            if lowered in {"exit", "quit", ":q"}:
                return
            if text == "/help":
                console.print(REPL_HELP.rstrip())
                continue
            if text == "/workspace":
                console.print(f"工作区 {runtime.workspace}")
                continue
            if text == "/new":
                thread_id = uuid.uuid4().hex[:8]
                console.print(f"[dim]已开始新会话 {thread_id}[/]")
                continue
            try:
                await _consume_stream(
                    runtime,
                    runtime.run(text, thread_id=thread_id),
                    thread_id,
                    _make_renderer(),
                    assume_yes=assume_yes,
                )
            except KeyboardInterrupt:
                console.print("\n[dim]已中断本轮；可继续输入下一条指令。[/]")
    finally:
        await runtime.aclose()

def repl(
    workspace: str = typer.Option(
        "",
        "--workspace",
        "-C",
        help="工作区：/mnt/d/proj、wsl:/home/u/proj 或 D:\\\\proj",
    ),
    write: bool = typer.Option(
        False, "--write", help="放开 L1 低风险写命令与文件工具写入"
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="自动批准 L2 命令；L3 危险命令始终需要人工确认",
    ),
    thread_id: str = typer.Option(
        "", "--thread-id", help="复用已有会话 id"
    ),
) -> None:
    """带工具的交互式多轮会话（REPL）。"""
    settings = apply_security_profile(get_settings())
    if (
        str(settings.security_profile or "").strip().lower()
        in PRODUCTION_PROFILES
        and (write or yes)
    ):
        console.print(
            "[yellow]production/ops profile 是只读审计档："
            "--write 与 --yes 不会生效，L2/L3 一律拒绝。[/]"
        )
    asyncio.run(
        _repl_loop(
            settings,
            workspace,
            allow_write=write,
            thread_id=thread_id or uuid.uuid4().hex[:8],
            assume_yes=yes,
        )
    )

async def _resume_events(
    settings: Settings,
    thread_id: str,
    workspace: str,
    *,
    allow_write: bool = False,
    assume_yes: bool = False,
) -> None:
    """从 checkpoint 恢复：steering 暂停走 resume_steering，审批走 resume。"""
    runtime = AgentRuntime(
        settings, workspace=workspace or None, allow_write=allow_write
    )
    render = _make_renderer()
    try:
        if runtime.is_paused(thread_id):
            async for event in runtime.resume_steering(thread_id):
                render(event)
            return

        pending = await runtime.pending_approvals(thread_id)
        if not pending:
            console.print("[yellow]该会话没有可恢复的挂起状态。[/]")
            return
        queue = pending
        while queue:
            decisions = _ask_approval(queue, assume_yes=assume_yes)
            queue = []
            async for event in runtime.resume(thread_id, decisions):
                render(event)
                if isinstance(event, ApprovalRequested):
                    queue.append(event)
    except KeyboardInterrupt:
        console.print("\n[dim]已中断[/]")
    finally:
        await runtime.aclose()

def run(
    prompt: str = typer.Argument(..., help="交给智能体的自然语言指令"),
    workspace: str = typer.Option(
        "",
        "--workspace",
        "-C",
        help="工作区：/mnt/d/proj、wsl:/home/u/proj 或 D:\\\\proj；Git Bash 建议用 wsl: 前缀",
    ),
    write: bool = typer.Option(False, "--write", help="放开 L1 低风险写命令与文件工具写入"),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="自动批准 L2 命令；L3 危险命令始终需要人工确认",
    ),
    thread_id: str = typer.Option("", "--thread-id", help="复用已有会话 id"),
    plan_only: bool = typer.Option(
        False,
        "--plan-only",
        help="只生成并展示计划，不执行任何工具或写操作",
    ),
) -> None:
    """走 LangGraph 图执行一轮任务。

    默认只读；变更类命令（L2/L3）会先停下来请求你确认。
    `--yes` 只跳过 L2，L3 危险命令始终逐条确认。
    `--plan-only` 只预览计划，适合在执行前审查范围与成本。
    """
    settings = apply_security_profile(get_settings())
    if (
        str(settings.security_profile or "").strip().lower()
        in PRODUCTION_PROFILES
        and (write or yes)
    ):
        console.print(
            "[yellow]production/ops profile 是只读审计档："
            "--write 与 --yes 不会生效，L2/L3 一律拒绝。[/]"
        )
    asyncio.run(
        _run_events(
            settings,
            prompt,
            write,
            thread_id or uuid.uuid4().hex[:8],
            workspace,
            assume_yes=yes,
            plan_only=plan_only,
        )
    )

def resume(
    thread_id: str = typer.Option(..., "--thread-id", help="要恢复的会话 id"),
    workspace: str = typer.Option(
        "",
        "--workspace",
        "-C",
        help="工作区；必须与挂起时一致（默认用当前配置）",
    ),
    write: bool = typer.Option(
        False, "--write", help="恢复后放开 L1 低风险写入"
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        "-y",
        help="自动批准 L2 命令；L3 危险命令始终需要人工确认",
    ),
    steering_dir: str = typer.Option(
        "",
        "--steering-dir",
        help=(
            "与挂起时一致的 steering 目录；"
            "留空用 AGENT_STEERING_DIR 或 <当前目录>/.agent/steering"
        ),
    ),
) -> None:
    """从 checkpoint 恢复被审批或 /pause 挂起的会话。

    审批挂起会先读取 checkpoint 里的待批请求再逐条询问；steering 暂停
    直接续跑。跨进程恢复不再依赖 TUI/Web。
    """
    settings = apply_security_profile(get_settings())
    if steering_dir:
        settings = settings.model_copy(update={"steering_dir": steering_dir})
    if (
        str(settings.security_profile or "").strip().lower()
        in PRODUCTION_PROFILES
        and (write or yes)
    ):
        console.print(
            "[yellow]production/ops profile 是只读审计档："
            "--write 与 --yes 不会生效，L2/L3 一律拒绝。[/]"
        )
    asyncio.run(
        _resume_events(
            settings,
            thread_id,
            workspace,
            allow_write=write,
            assume_yes=yes,
        )
    )

def steer(
    thread_id: str = typer.Option(..., "--thread-id", help="正在运行的会话 id"),
    action: str = typer.Argument(..., help="pause / cancel / add / replan"),
    text: str = typer.Option("", "--text", help="add/replan 的说明文本"),
    role: str = typer.Option("explore", "--role", help="add 的角色：explore / test / review"),
    steering_dir: str = typer.Option(
        "",
        "--steering-dir",
        help=(
            "与运行中 agent 一致的队列目录；"
            "留空用 AGENT_STEERING_DIR 或 <当前目录>/.agent/steering"
        ),
    ),
    workspace: str = typer.Option(
        "",
        "--workspace",
        "-C",
        help="写入审计记录的工作区（应与运行中会话一致；不影响队列目录）",
    ),
) -> None:
    """向正在运行的会话注入一条实时干预指令。

    指令写入 `--steering-dir`（默认 `.agent/steering/`）并**先落审计**，
    运行中的进程会在下一个节点边界消费。队列目录必须与运行中的 agent
    一致：两者从不同目录启动时，请显式传 `--steering-dir` 或设置
    `AGENT_STEERING_DIR`。跨进程 resume 可用 `agent resume`、TUI 或 Web。
    """
    try:
        command = SteeringCommand(
            action=action,
            text=text,
            role=role,
            source="cli",
            ts=now_iso(),
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc

    settings = apply_security_profile(get_settings())
    if steering_dir:
        settings = settings.model_copy(update={"steering_dir": steering_dir})
    try:
        submit_steering_command(
            settings,
            thread_id,
            command,
            workspace=workspace or None,
        )
    except (AuditError, OSError) as exc:
        console.print(f"[red]提交 steering 失败：{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[green]已提交 steering[/green] {action} → {thread_id}；"
        f"队列目录 {settings.resolved_steering_dir}，"
        "运行中的 agent 会在下一个边界消费。"
    )
