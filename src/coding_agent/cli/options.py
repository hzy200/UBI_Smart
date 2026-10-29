"""多个命令共用的 Typer 选项定义。"""

from __future__ import annotations

import typer

_WORKSPACE_OPTION = typer.Option(
    "",
    "--workspace",
    "-C",
    help="工作区，WSL 绝对路径（/mnt/d/proj）或 Windows 路径（D:\\\\proj）均可，默认用配置值",
)
