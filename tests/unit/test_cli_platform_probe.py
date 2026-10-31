"""探活接口的边界：前端层只有固定意图，没有"执行命令"的入口。

背景（TD-032 / TD-034）：`cli/platform.py` 曾把 `sandbox.run` 原样透传出去，
并在 2026-10-29 一度用"命令名闭集"兜底。现在更进一步 —— 探活变成四个固定
意图（identity / which / dir_exists / ensure_dir），CLI 侧根本没有可传命令的
参数，闸门因此不再需要存在。
"""

from __future__ import annotations

import pytest

from ubismart.cli.platform import SandboxProbe, SandboxProbeError
from ubismart.config import Settings
from ubismart.sandbox import probe
from ubismart.sandbox.wsl_exec import ExecResult, WslSandbox


class _Sandbox:
    def __init__(self, stdout: str = "", *, ok: bool = True) -> None:
        self.stdout = stdout
        self.ok = ok
        self.commands: list[str] = []

    def run(self, command: str, *, cwd: str | None = None) -> ExecResult:
        self.commands.append(command)
        return ExecResult(
            command=command,
            exit_code=0 if self.ok else 1,
            stdout=self.stdout,
            stderr="" if self.ok else "boom",
            duration_ms=1,
        )


def _probe(monkeypatch, stdout: str = "", *, ok: bool = True):
    sandbox = _Sandbox(stdout, ok=ok)
    monkeypatch.setattr(WslSandbox, "__init__", lambda self, settings: None)
    monkeypatch.setattr(
        WslSandbox,
        "run",
        lambda self, command, *, cwd=None: sandbox.run(command, cwd=cwd),
        raising=True,
    )
    return SandboxProbe(Settings(_env_file=None)), sandbox


# ---------------- identity ----------------


def test_identity_parses_user_and_kernel_in_one_call() -> None:
    sandbox = _Sandbox("USER=agent\nKERNEL=5.15.0-91-generic\n")

    ident = probe.identity(sandbox)  # type: ignore[arg-type]

    assert ident.user == "agent"
    assert ident.kernel == "5.15.0-91-generic"
    assert len(sandbox.commands) == 1  # 用户与内核在同一条命令里


def test_identity_raises_when_sandbox_fails() -> None:
    sandbox = _Sandbox("", ok=False)
    with pytest.raises(SandboxProbeError):
        probe.identity(sandbox)  # type: ignore[arg-type]


def test_identity_tolerates_missing_kernel() -> None:
    assert probe.identity(_Sandbox("USER=agent\n")).kernel == ""  # type: ignore[arg-type]


# ---------------- which ----------------


def test_which_returns_path_or_empty() -> None:
    assert probe.which(_Sandbox("/usr/bin/rg\n"), "rg") == "/usr/bin/rg"  # type: ignore[arg-type]
    assert probe.which(_Sandbox("\n"), "rg") == ""  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "name",
    ["rg; rm -rf /", "a b", "../evil", "", "x" * 65],
)
def test_which_rejects_anything_but_a_plain_command_name(name: str) -> None:
    sandbox = _Sandbox("/usr/bin/rg\n")
    with pytest.raises(SandboxProbeError):
        probe.which(sandbox, name)  # type: ignore[arg-type]
    assert sandbox.commands == []  # 非法名字不会被拼进脚本


# ---------------- 目录能力（走 SandboxFs） ----------------


class _FsSandbox:
    """回显 stat/mkdir 脚本所需的输出，不依赖 WSL。"""

    def __init__(self, *, exists: bool = True, is_file: bool = False) -> None:
        self.exists = exists
        self.is_file = is_file
        self.commands: list[str] = []

    def run(self, command: str, *, cwd: str | None = None) -> ExecResult:
        self.commands.append(command)
        stdout = ""
        if command.strip().startswith("realpath -m -- "):
            stdout = command.strip().split("-- ", 1)[1].strip("'\"") + "\n"
        elif 'echo "EXISTS=' in command:
            stdout = (
                f"EXISTS={1 if self.exists else 0}\n"
                f"FILE={1 if self.is_file else 0}\n"
                "SIZE=0\nMTIME=0\nREAL=/srv/ws\n"
            )
        elif "REAL=$real_p" in command:
            stdout = "REAL=/srv/ws\n"
        return ExecResult(
            command=command, exit_code=0, stdout=stdout, stderr="", duration_ms=1
        )


def test_dir_exists_true_for_a_directory() -> None:
    assert probe.dir_exists(_FsSandbox(), "/srv/ws") is True  # type: ignore[arg-type]


def test_dir_exists_false_for_a_file() -> None:
    sandbox = _FsSandbox(is_file=True)
    assert probe.dir_exists(sandbox, "/srv/ws") is False  # type: ignore[arg-type]


def test_dir_exists_false_when_missing() -> None:
    sandbox = _FsSandbox(exists=False)
    assert probe.dir_exists(sandbox, "/srv/ws") is False  # type: ignore[arg-type]


def test_ensure_dir_returns_realpath() -> None:
    sandbox = _FsSandbox()

    assert probe.ensure_dir(sandbox, "/srv/ws") == "/srv/ws"  # type: ignore[arg-type]
    assert "mkdir -p" in sandbox.commands[-1]


# ---------------- SandboxProbe 只是转发 ----------------


def test_probe_rejects_shell_style_commands(monkeypatch) -> None:
    """`SandboxProbe` 没有 `run`：调用方连"传一条命令"的入口都拿不到。"""
    probe_adapter, _ = _probe(monkeypatch)
    assert not hasattr(probe_adapter, "run")
    with pytest.raises(AttributeError):
        probe_adapter.run("whoami")  # type: ignore[attr-defined]


def test_probe_which_delegates(monkeypatch) -> None:
    probe_adapter, sandbox = _probe(monkeypatch, "/usr/bin/rg\n")
    assert probe_adapter.which("rg") == "/usr/bin/rg"
    assert "command -v" in sandbox.commands[0]


def test_probe_identity_delegates(monkeypatch) -> None:
    probe_adapter, _ = _probe(monkeypatch, "USER=agent\nKERNEL=6.1\n")
    assert probe_adapter.identity().user == "agent"
