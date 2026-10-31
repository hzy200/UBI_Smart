"""CLI 的平台探测适配器。

这是前端层唯一允许触达 `WslSandbox` 的窄接口，只服务 `doctor` 与
`sandbox-init`。它**不提供"执行命令"的能力**：四个方法都是固定意图
（`identity` / `which` / `dir_exists` / `ensure_dir`），实现放在 A4 的
`sandbox/probe.py`，CLI 只负责转发 —— 前端层没有第二个 shell（TD-032）。
"""

from __future__ import annotations

from coding_agent.config import Settings
from coding_agent.sandbox import probe as _probe
from coding_agent.sandbox.probe import SandboxProbeError, ShellIdentity
from coding_agent.sandbox.wsl_exec import WslSandbox
from coding_agent.sandbox.wsl_exec import resolve_workspace as _resolve_workspace

# `SandboxProbeError` 在这里被转出：CLI 命令层不该直接 import `sandbox`，
# 否则就绕过了"前端层只经窄接口触达沙箱"的约束。
__all__ = ["SandboxProbe", "SandboxProbeError", "resolve_workspace"]


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

    def identity(self) -> ShellIdentity:
        """运行用户 + 内核版本（一次调用）。"""
        return _probe.identity(self._sandbox)

    def which(self, name: str) -> str:
        """命令路径；空串表示没装。"""
        return _probe.which(self._sandbox, name)

    def dir_exists(self, path: str) -> bool:
        return _probe.dir_exists(self._sandbox, path)

    def ensure_dir(self, path: str) -> str:
        """创建目录并返回 realpath。"""
        return _probe.ensure_dir(self._sandbox, path)


def resolve_workspace(settings: Settings, probe: SandboxProbe) -> str:
    return _resolve_workspace(settings, probe._sandbox)
