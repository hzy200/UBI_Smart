"""执行后端选择：Windows 宿主用 WSL，WSL 内 Agent Server 用本地 Linux。"""

from __future__ import annotations

import os
from typing import Any

from coding_agent.concurrency import SyncLeasePool
from coding_agent.config import Settings
from coding_agent.sandbox.local_exec import LocalLinuxSandbox
from coding_agent.sandbox.wsl_exec import WslSandbox


def resolve_execution_backend(settings: Settings) -> str:
    raw = str(getattr(settings, "execution_backend", "") or "auto").lower()
    if raw in {"local", "wsl"}:
        return raw
    # auto：Windows 宿主走 wsl.exe；Linux/WSL 内直接执行。
    return "wsl" if os.name == "nt" else "local"


def build_sandbox(
    settings: Settings, *, slots: SyncLeasePool | None = None
) -> Any:
    if resolve_execution_backend(settings) == "local":
        return LocalLinuxSandbox(settings, slots=slots)
    return WslSandbox(settings, slots=slots)
