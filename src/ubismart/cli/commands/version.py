"""版本命令。"""

from __future__ import annotations

import importlib.metadata as metadata

import typer

from coding_agent.cli.context import console


def _package_version() -> str:
    try:
        return metadata.version("coding-agent")
    except metadata.PackageNotFoundError:
        return "unknown"

def _version_callback(value: bool) -> None:
    if value:
        console.print(_package_version())
        raise typer.Exit()

def version() -> None:
    """显示当前安装的版本。"""
    console.print(_package_version())
