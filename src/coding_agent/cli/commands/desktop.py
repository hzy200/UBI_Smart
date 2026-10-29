"""桌面与终端界面命令。"""

from __future__ import annotations

import os

import typer
from rich.markup import escape

from coding_agent.cli.context import console, get_settings


def gui(
    workspace: str = typer.Option(
        "",
        "--workspace",
        "-C",
        help="工作区：/mnt/d/proj、wsl:/home/u/proj 或 D:\\\\proj；Git Bash 建议用 wsl: 前缀",
    ),
    write: bool = typer.Option(
        False,
        "--write",
        help="允许会话选择审批写/自动写；默认所有会话只读",
    ),
    port: int = typer.Option(0, "--port", help="本地服务端口；0 表示自动分配"),
    setup: bool = typer.Option(False, "--setup", help="重新运行 WSL 配置向导"),
    host_mode: bool = typer.Option(
        False,
        "--host-mode",
        help="旧 Windows 宿主模式；默认使用 WSL Agent Server",
    ),
) -> None:
    """启动桌面图形界面。

    Windows 默认：配置向导 + WSL Agent Server + WebView；
    `--host-mode` 保留旧的 Windows 宿主模式。
    """
    try:
        if os.name == "nt" and not host_mode:
            from coding_agent.desktop.launcher import main as launch

            argv = ["--setup"] if setup else []
            if workspace:
                argv += ["--workspace", workspace]
            if write:
                argv.append("--write")
            code = launch(argv)
            if code:
                raise typer.Exit(code=code)
            return
        from coding_agent.desktop import run_desktop
    except ImportError as exc:
        hint = escape('pip install -e ".[web,desktop]"')
        console.print(f"[red]未安装桌面依赖。请运行：{hint}[/]")
        raise typer.Exit(code=2) from exc
    except RuntimeError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=2) from exc

    try:
        run_desktop(
            get_settings(),
            workspace=workspace,
            allow_write=write,
            port=port,
        )
    except RuntimeError as exc:
        console.print(f"[red]{escape(str(exc))}[/]")
        raise typer.Exit(code=2) from exc

def tui(
    workspace: str = typer.Option(
        "",
        "--workspace",
        "-C",
        help="工作区：/mnt/d/proj、wsl:/home/u/proj 或 D:\\\\proj；Git Bash 建议用 wsl: 前缀",
    ),
    write: bool = typer.Option(False, "--write", help="放开 L1 低风险写命令（mkdir/cp 等）"),
) -> None:
    """启动终端界面（Textual）。"""
    try:
        from coding_agent.tui import AgentTuiApp
    except ImportError as exc:
        # 必须转义 [ui]：不转义的话 rich 会把它当样式标记吞掉，
        # 用户看到的是一条坏掉的安装命令
        hint = escape('pip install -e ".[ui]"')
        console.print(f"[red]未安装 UI 依赖。请运行：{hint}[/]")
        raise typer.Exit(code=2) from exc

    AgentTuiApp(get_settings(), workspace=workspace or None, allow_write=write).run()
