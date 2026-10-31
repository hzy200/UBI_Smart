"""执行后端选择：Windows 宿主走 WSL，WSL/Linux 内走本地 bash。"""

from __future__ import annotations

import os

from ubismart.config import Settings
from ubismart.sandbox.backend import build_sandbox, resolve_execution_backend
from ubismart.sandbox.local_exec import LocalLinuxSandbox
from ubismart.sandbox.wsl_exec import WslSandbox


def _settings(tmp_path, **overrides) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        **overrides,
    )


def test_explicit_local_backend(tmp_path) -> None:
    settings = _settings(tmp_path, execution_backend="local")
    assert resolve_execution_backend(settings) == "local"
    assert isinstance(build_sandbox(settings), LocalLinuxSandbox)


def test_explicit_wsl_backend(tmp_path) -> None:
    settings = _settings(tmp_path, execution_backend="wsl")
    assert resolve_execution_backend(settings) == "wsl"
    assert isinstance(build_sandbox(settings), WslSandbox)


def test_auto_backend_follows_platform(tmp_path, monkeypatch) -> None:
    settings = _settings(tmp_path)
    monkeypatch.setattr(os, "name", "nt")
    assert resolve_execution_backend(settings) == "wsl"
    monkeypatch.setattr(os, "name", "posix")
    assert resolve_execution_backend(settings) == "local"
