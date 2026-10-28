"""CLI 的平台探测适配器。

这是前端层唯一允许触达 `WslSandbox` 的窄接口，只用于 `doctor` 与
`sandbox-init` 的只读探测/目录初始化，不承载工具调用或审批语义。
"""

from __future__ import annotations

from typing import Any

from coding_agent.config import Settings
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.sandbox.wsl_exec import resolve_workspace as _resolve_workspace


class SandboxProbe:
    def __init__(self, settings: Settings) -> None:
        self._sandbox = WslSandbox(settings)

    @staticmethod
    def list_distros() -> list[str]:
        return WslSandbox.list_distros()

    @staticmethod
    def available(distro: str) -> bool:
        return WslSandbox.available(distro)

    @property
    def settings(self) -> Settings:
        return self._sandbox.settings

    @settings.setter
    def settings(self, value: Settings) -> None:
        self._sandbox.settings = value

    def health(self, *, force: bool = False) -> bool:
        return self._sandbox.health(force=force)

    def circuit_state(self) -> str:
        return self._sandbox.circuit_state()

    def run(self, command: str, *, cwd: str | None = None) -> Any:
        return self._sandbox.run(command, cwd=cwd)


def resolve_workspace(settings: Settings, probe: SandboxProbe) -> str:
    return _resolve_workspace(settings, probe._sandbox)
