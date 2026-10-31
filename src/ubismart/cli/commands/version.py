"""版本命令。"""

from __future__ import annotations

import importlib.metadata as metadata

import typer

from ubismart.cli.context import console


def _package_version() -> str:
    for dist in ("ubismart", "coding-agent"):
        try:
            return metadata.version(dist)
        except metadata.PackageNotFoundError:
            continue
    return "unknown"

def _version_callback(value: bool) -> None:
    if value:
        console.print(_package_version())
        raise typer.Exit()

def version() -> None:
    """显示当前安装的版本。"""
    console.print(_package_version())
