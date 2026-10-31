"""终端 CLI：环境自检、纯对话、事件流渲染。

    agent doctor        环境自检（不需要 API Key）
    agent sandbox-init  创建沙箱工作区
    agent chat          纯流式对话，验证 API 联通
    agent run           走 AgentRuntime 执行任务

run 命令只消费 AgentRuntime 产出的领域事件，不直接接触图和工具。
TUI / Web 前端将复用同一套事件。
"""

from __future__ import annotations

import sys

import typer
from rich.console import Console

from ubismart.cli.admin import register_admin_commands
from ubismart.cli.commands import (
    audit as _audit_commands,
)
from ubismart.cli.commands import (
    desktop as _desktop_commands,
)
from ubismart.cli.commands import (
    memory as _memory_commands,
)
from ubismart.cli.commands import (
    rules as _rules_commands,
)
from ubismart.cli.commands import (
    run as _run_commands,
)
from ubismart.cli.commands import (
    sessions as _sessions_commands,
)
from ubismart.cli.commands import (
    setup as _setup_commands,
)
from ubismart.cli.commands import (
    version as _version_commands,
)
from ubismart.cli.commands import (
    web as _web_commands,
)
from ubismart.cli.commands.audit import _audit_outcome as _audit_outcome
from ubismart.cli.commands.run import _ask_approval as _ask_approval
from ubismart.cli.commands.run import _make_renderer as _make_renderer
from ubismart.cli.commands.sessions import _diff_text as _diff_text
from ubismart.cli.commands.setup import _check_langsmith as _check_langsmith
from ubismart.cli.commands.setup import _report as _report
from ubismart.cli.commands.setup import _warn as _warn
from ubismart.cli.commands.version import _package_version as _package_version
from ubismart.cli.commands.web import _is_loopback_bind as _is_loopback_bind
from ubismart.config import get_settings as get_settings
from ubismart.llm.deepseek import MissingApiKeyError
from ubismart.runtime import AgentRuntime as AgentRuntime


def _configure_stdio() -> None:
    """Windows 的标准流默认走 GBK：管道/重定向时中文会乱码，stdin 还会产生
    代理字符导致下游 JSON 序列化直接崩溃。统一按 UTF-8 处理。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")


_configure_stdio()

app = typer.Typer(add_completion=True, no_args_is_help=True, help="终端原生编程智能体")
console = Console()

register_admin_commands(app, console)


@app.callback()
def _root_options(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_commands._version_callback,
        is_eager=True,
        help="显示版本并退出",
    ),
) -> None:
    """终端原生编程智能体。"""


app.command()(_version_commands.version)


# --------------------------------------------------------------------------
# 渲染辅助
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# doctor
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# chat
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


# 拆分出去的独立命令组：按原顺序注册，命令名与选项保持不变。
app.command()(_setup_commands.doctor)
app.command("sandbox-init")(_setup_commands.sandbox_init)
app.command("init")(_setup_commands.init)
app.command()(_run_commands.chat)
app.command("repl")(_run_commands.repl)
app.command()(_run_commands.run)
app.command()(_run_commands.resume)
app.command()(_run_commands.steer)

for _command in (
    _sessions_commands.snapshots,
    _sessions_commands.undo,
    _sessions_commands.diff,
    _sessions_commands.patch,
    _sessions_commands.sessions,
    _sessions_commands.history,
    _sessions_commands.checkpoints,
    _sessions_commands.fork,
    _sessions_commands.rewind,
    _memory_commands.memory,
    _rules_commands.rules,
    _audit_commands.audit,
    _web_commands.web,
    _desktop_commands.gui,
    _desktop_commands.tui,
):
    app.command()(_command)


def main() -> None:
    try:
        app()
    except MissingApiKeyError as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(code=2) from exc


if __name__ == "__main__":
    main()
