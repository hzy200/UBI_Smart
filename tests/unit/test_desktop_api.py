"""Phase 3：桌面桥的参数校验与命令构造。"""

from __future__ import annotations

import subprocess

from coding_agent.desktop import api as desktop_api
from coding_agent.desktop.config import LauncherConfig


def _config() -> LauncherConfig:
    return LauncherConfig(
        distro="CodingAgent",
        workspace="/home/agent/.coding-agent/workspaces/default",
        python="/opt/coding-agent/venv/bin/python",
    )


def test_copy_rejects_non_mnt_source() -> None:
    result = desktop_api.DesktopApi(_config()).copy_to_wsl("/home/me/proj")
    assert result["ok"] is False
    assert "/mnt" in result["error"]


def test_copy_rejects_illegal_names() -> None:
    bridge = desktop_api.DesktopApi(_config())
    for name in ("../x", "a b", "x" * 65):
        result = bridge.copy_to_wsl("/mnt/d/proj", name)
        assert result["ok"] is False


def test_open_in_vscode_builds_wsl_remote_command(monkeypatch) -> None:
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, args, **kwargs) -> None:
            calls.append(list(args))

    monkeypatch.setattr(desktop_api.shutil, "which", lambda name: "code")
    monkeypatch.setattr(desktop_api.subprocess, "Popen", FakePopen)

    result = desktop_api.DesktopApi(_config()).open_in_vscode(
        "/home/agent/proj"
    )
    assert result["ok"] is True
    assert calls == [
        ["code", "--remote", "wsl+CodingAgent", "/home/agent/proj"]
    ]
    assert desktop_api.DesktopApi(_config()).open_in_vscode("relative")["ok"] is False


def test_copy_runs_cp_without_shell_and_reports_done(monkeypatch) -> None:
    calls: list[list[str]] = []

    class ImmediateThread:
        def __init__(self, target, **kwargs) -> None:
            self._target = target

        def start(self) -> None:
            self._target()

    def fake_run(distro: str, script: str, **kwargs):
        return subprocess.CompletedProcess(
            args=["wsl.exe"], returncode=0, stdout=b"SRC\n", stderr=b""
        )

    def fake_subprocess_run(args, **kwargs):
        calls.append(list(args))
        return subprocess.CompletedProcess(args=args, returncode=0)

    monkeypatch.setattr(desktop_api, "home", lambda distro: "/home/agent")
    monkeypatch.setattr(desktop_api, "run", fake_run)
    monkeypatch.setattr(desktop_api.subprocess, "run", fake_subprocess_run)
    monkeypatch.setattr(desktop_api.threading, "Thread", ImmediateThread)

    bridge = desktop_api.DesktopApi(_config())
    result = bridge.copy_to_wsl("/mnt/d/proj", "proj")
    assert result["ok"] is True
    assert result["target"] == "/home/agent/.coding-agent/workspaces/proj"
    assert calls == [
        [
            "wsl.exe",
            "-d",
            "CodingAgent",
            "--",
            "cp",
            "-a",
            "--",
            "/mnt/d/proj",
            "/home/agent/.coding-agent/workspaces/proj",
        ]
    ]
    status = bridge.copy_status(result["job_id"])
    assert status["state"] == "done"


def test_copy_refuses_existing_target(monkeypatch) -> None:
    monkeypatch.setattr(desktop_api, "home", lambda distro: "/home/agent")
    monkeypatch.setattr(
        desktop_api,
        "run",
        lambda distro, script, **kwargs: subprocess.CompletedProcess(
            args=["wsl.exe"], returncode=0, stdout=b"SRC\nDEST\n", stderr=b""
        ),
    )
    result = desktop_api.DesktopApi(_config()).copy_to_wsl(
        "/mnt/d/proj", "proj"
    )
    assert result["ok"] is False
    assert "已存在" in result["error"]
