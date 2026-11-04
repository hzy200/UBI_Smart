"""租户目录与 thread 命名空间隔离。"""

from __future__ import annotations

import re

import pytest

from ubismart.config import Settings
from ubismart.identity.tenancy import TenantError, TenantManager
from ubismart.sandbox.wsl_exec import ExecResult

_REALPATH_RE = re.compile(r"^realpath -m -- (.*)$")
_PATH_ASSIGN_RE = re.compile(r"^p=(.*)$", re.MULTILINE)


def _emit_path(command: str) -> str:
    """模拟脚本里 `$p` 对应的路径（测试里的路径不需要真正的 shell 展开）。"""
    match = _PATH_ASSIGN_RE.search(command)
    return match.group(1).strip().strip("'\"") if match else ""


class _FakeSandbox:
    def __init__(self, home: str = "/home/tester") -> None:
        self._home = home
        self.commands: list[str] = []

    def home(self) -> str:
        return self._home

    def run(self, command, *, cwd=None, timeout=None, capture_limit=None):  # noqa: ANN001
        self.commands.append(command)
        # `SandboxFs` 构造时解析根目录、`mkdir` 结束时回显 REAL=，两者都要有输出。
        stdout = ""
        probe = _REALPATH_RE.match(command.strip())
        if probe:
            stdout = probe.group(1).strip().strip("'\"") + "\n"
        elif "REAL=$real_p" in command:
            stdout = f"REAL={_emit_path(command)}\n"
        return ExecResult(
            command=command,
            exit_code=0,
            stdout=stdout,
            stderr="",
            duration_ms=1,
        )


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        web_auth_tokens='{"tok": "alice"}',
        web_workspaces_root="/srv/workspaces",
        web_memory_root="/srv/memory",
        web_library_root="/srv/library",
        web_data_root=str(tmp_path / "host-data"),
        wsl_workspace="/srv/workspaces/alice",
    )


def test_per_user_paths_are_isolated(tmp_path) -> None:
    manager = TenantManager(_settings(tmp_path), sandbox=_FakeSandbox())

    tenant = manager.for_user("alice")

    assert tenant.multi_user is True
    assert tenant.workspace == "/srv/workspaces/alice"
    assert tenant.settings.wsl_workspace == "/srv/workspaces/alice"
    assert tenant.settings.audit_dir == str(
        tmp_path / "host-data" / "alice" / "audit"
    )
    assert tenant.settings.checkpoint_path == str(
        tmp_path / "host-data" / "alice" / "checkpoints.sqlite"
    )
    assert tenant.settings.memory_root == "/srv/memory/alice"
    assert tenant.library_dir == "/srv/library/alice"
    assert tenant.settings.library_root == "/srv/library/alice"
    assert tenant.thread_id("t1") == "alice:t1"
    assert tenant.public_thread("alice:t1") == "t1"

    bob = manager.for_user("bob")
    assert bob.library_dir == "/srv/library/bob"
    assert bob.library_dir != tenant.library_dir


def test_tenant_dirs_are_created_without_shell(tmp_path) -> None:
    """租户目录由 `SandboxFs.mkdir` 创建，tenancy 不再自己拼 `mkdir -p`（TD-034）。"""
    sandbox = _FakeSandbox()
    manager = TenantManager(_settings(tmp_path), sandbox=sandbox)

    manager.for_user("alice")

    # 没有一条命令是 tenancy 手写的 `mkdir -p <path>`；创建动作来自 A4 的脚本。
    assert not any(
        command.strip().startswith("mkdir -p /")
        for command in sandbox.commands
    )
    assert any("REAL=$real_p" in command for command in sandbox.commands)
    assert any(command.startswith("realpath -m") for command in sandbox.commands)


def test_invalid_user_is_rejected(tmp_path) -> None:
    manager = TenantManager(_settings(tmp_path), sandbox=_FakeSandbox())

    for name in ("../escape", "a/b", "", "x" * 65):
        with pytest.raises(TenantError):
            manager.for_user(name)


def test_local_mode_keeps_existing_paths(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        wsl_workspace="/tmp/ws",
        web_data_root=str(tmp_path / "host-data"),
    )
    manager = TenantManager(settings, sandbox=_FakeSandbox())

    tenant = manager.for_user("local")

    assert tenant.multi_user is False
    assert tenant.settings is settings
    assert tenant.thread_id("t1") == "t1"


def test_local_mode_still_accepts_any_absolute_path(tmp_path) -> None:
    settings = Settings(
        _env_file=None,
        wsl_workspace="/tmp/ws",
        web_data_root=str(tmp_path / "host-data"),
    )
    manager = TenantManager(settings, sandbox=_FakeSandbox())

    assert manager.resolve_workspace("local", "/srv/elsewhere") == "/srv/elsewhere"


def test_multi_user_request_is_scoped_to_own_workspace(tmp_path) -> None:
    manager = TenantManager(_settings(tmp_path), sandbox=_FakeSandbox())

    assert manager.resolve_workspace("alice", "") == "/srv/workspaces/alice"
    assert (
        manager.resolve_workspace("alice", "/srv/workspaces/alice/proj")
        == "/srv/workspaces/alice/proj"
    )


def test_cross_user_workspace_request_is_rejected(tmp_path) -> None:
    """工作区文件不按用户隔离：越到别人目录就是越权，必须拒绝。"""
    manager = TenantManager(_settings(tmp_path), sandbox=_FakeSandbox())

    for target in (
        "/srv/workspaces/bob",
        "/srv/workspaces/bob/proj",
        "/srv/workspaces/alice/../bob",
        "/srv/workspaces",
    ):
        with pytest.raises(TenantError):
            manager.resolve_workspace("alice", target)
