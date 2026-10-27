"""Phase 3：桌面桥的参数校验与命令构造。"""

from __future__ import annotations

import logging
import subprocess

from coding_agent.desktop import api as desktop_api
from coding_agent.desktop.config import LauncherConfig

TRUSTED = "http://127.0.0.1:8780"


def _config() -> LauncherConfig:
    return LauncherConfig(
        distro="CodingAgent",
        workspace="/home/agent/.coding-agent/workspaces/default",
        python="/opt/coding-agent/venv/bin/python",
    )


def _bridge(actual: str = TRUSTED) -> desktop_api.DesktopApi:
    return desktop_api.DesktopApi(
        _config(),
        allowed_origin=TRUSTED,
        origin_provider=lambda: actual,
    )


def test_copy_rejects_non_mnt_source() -> None:
    result = _bridge().copy_to_wsl("/home/me/proj")
    assert result["ok"] is False
    assert "/mnt" in result["error"]


def test_copy_rejects_illegal_names() -> None:
    bridge = _bridge()
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

    result = _bridge().open_in_vscode("/home/agent/proj")
    assert result["ok"] is True
    assert calls == [
        ["code", "--remote", "wsl+CodingAgent", "/home/agent/proj"]
    ]
    assert _bridge().open_in_vscode("relative")["ok"] is False


def test_host_editor_and_reveal_respect_workspace(monkeypatch, tmp_path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    target = root / "a.py"
    target.write_text("print(1)", encoding="utf-8")
    calls: list[list[str]] = []

    class FakePopen:
        def __init__(self, args, **kwargs) -> None:
            calls.append(list(args))

    monkeypatch.setattr(desktop_api.shutil, "which", lambda name: "code")
    monkeypatch.setattr(desktop_api.subprocess, "Popen", FakePopen)
    bridge = _bridge()

    opened = bridge.open_host_in_vscode(str(root), "a.py")
    assert opened["ok"] is True
    assert calls[-1] == ["code", str(target)]

    assert bridge.open_host_in_vscode(str(root), "../etc/passwd")["ok"] is False
    assert bridge.open_host_in_vscode(str(root), "missing.py")["ok"] is False
    assert bridge.open_host_in_vscode(str(root), str(target))["ok"] is False

    revealed = bridge.reveal_in_explorer(str(root), "a.py")
    assert revealed["ok"] is True
    assert calls[-1][0] in {"explorer.exe", "open", "xdg-open"}


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

    bridge = _bridge()
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
    result = _bridge().copy_to_wsl("/mnt/d/proj", "proj")
    assert result["ok"] is False
    assert "已存在" in result["error"]


def test_bridge_rejects_untrusted_or_unknown_origin() -> None:
    evil = _bridge("https://evil.example.com")
    assert evil.copy_to_wsl("/mnt/d/proj", "proj")["ok"] is False
    assert "来源" in evil.open_url("https://example.com")["error"]
    assert evil.pick_folder() == ""
    assert evil.copy_status("job")["state"] == "denied"

    # 没有可证明的当前 origin（测试环境无 webview）时必须 fail closed。
    no_origin = desktop_api.DesktopApi(
        _config(), allowed_origin=TRUSTED
    )
    assert no_origin.open_in_vscode("/home/agent/proj")["ok"] is False


def test_bridge_denial_is_logged(caplog) -> None:
    evil = _bridge("https://evil.example.com")
    with caplog.at_level(logging.WARNING):
        evil.open_url("https://example.com")
    assert "桌面桥拒绝" in caplog.text
    assert "https://evil.example.com" in caplog.text


def test_bridge_rejects_non_loopback_allowed_origin() -> None:
    remote = desktop_api.DesktopApi(
        _config(),
        allowed_origin="https://app.example.com",
        origin_provider=lambda: "https://app.example.com",
    )
    assert remote.open_url("https://example.com")["ok"] is False


def test_origin_helpers_normalize_and_validate() -> None:
    assert (
        desktop_api.origin_of("http://127.0.0.1:8780/#token=x")
        == "http://127.0.0.1:8780"
    )
    assert desktop_api.origin_of("javascript:alert(1)") == ""
    assert desktop_api.is_loopback_origin("http://127.0.0.1:8780")
    assert desktop_api.is_loopback_origin("http://localhost:8780")
    assert not desktop_api.is_loopback_origin("https://example.com")
