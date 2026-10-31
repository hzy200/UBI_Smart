"""Phase 5：诊断包、更新检查与崩溃看门狗。"""

from __future__ import annotations

import json
import zipfile
from datetime import datetime

from ubismart.desktop import diagnostics, update
from ubismart.desktop.config import LauncherConfig
from ubismart.desktop.watchdog import supervise


def test_sanitize_redacts_common_secret_shapes() -> None:
    text = (
        "api_key=sk-abcdefgh12345678\n"
        "Authorization: Bearer abcdefghijklmnop\n"
        "token: supersecretvalue\n"
    )
    clean = diagnostics.sanitize(text)
    assert "sk-abcdefgh" not in clean
    assert "abcdefghijklmnop" not in clean
    assert "supersecretvalue" not in clean
    assert "[redacted]" in clean


def test_collect_diagnostics_writes_redacted_zip(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    config = LauncherConfig(
        distro="CodingAgent",
        workspace="/home/agent/ws",
        python="/opt/coding-agent/venv/bin/python",
    )
    config.save()
    logs = tmp_path / "CodingAgent" / "logs"
    logs.mkdir(parents=True)
    (logs / "launcher.log").write_text(
        "starting with Bearer abcdefghijklmnop\n", encoding="utf-8"
    )
    (logs / "server.log").write_text(
        "key=sk-abcdefgh12345678\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        diagnostics, "_wsl_info", lambda cfg: "wsl text sk-abcdefgh12345678"
    )

    path = diagnostics.collect_diagnostics(
        config, now=datetime(2026, 10, 24, 8, 0, 0)
    )
    assert path.name == "UBISmart-diagnostics-20261024-080000.zip"
    with zipfile.ZipFile(path) as archive:
        names = set(archive.namelist())
        assert {
            "summary.json",
            "launcher.json",
            "logs/launcher.log",
            "logs/server.log",
            "wsl.txt",
            "README.txt",
        } <= names
        summary = json.loads(archive.read("summary.json"))
        assert summary["config"]["distro"] == "CodingAgent"
        for name in names:
            data = archive.read(name).decode("utf-8")
            assert "sk-abcdefgh" not in data
            assert "abcdefghijklmnop" not in data


class _Response:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, limit: int = 0) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_update_check_compares_versions(monkeypatch) -> None:
    monkeypatch.setattr(
        update.urllib.request,
        "urlopen",
        lambda request, timeout: _Response(
            {
                "version": "0.2.0",
                "download_url": "https://example.com/setup.exe",
                "sha256": "abc",
                "notes": "fix",
            }
        ),
    )
    result = update.check_update("0.1.0", "https://example.com/latest.json")
    assert result["ok"] is True
    assert result["available"] is True
    assert result["latest"] == "0.2.0"
    assert result["download_url"].endswith("setup.exe")

    same = update.check_update("0.2.0", "https://example.com/latest.json")
    assert same["available"] is False

    bad = update.check_update("0.1.0", "ftp://example.com/latest.json")
    assert bad["ok"] is False


def test_update_check_rejects_bad_manifest(monkeypatch) -> None:
    monkeypatch.setattr(
        update.urllib.request,
        "urlopen",
        lambda request, timeout: _Response({"version": "nope"}),
    )
    result = update.check_update("0.1.0", "https://example.com/latest.json")
    assert result["ok"] is False


class _Proc:
    def __init__(self, code: int | None) -> None:
        self.code = code

    def poll(self):
        return self.code


class _Server:
    def __init__(self, code: int | None) -> None:
        self.proc = _Proc(code)


class _Event:
    def __init__(self, stopped: bool = False) -> None:
        self.stopped = stopped

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, timeout: float) -> bool:
        return self.stopped


def test_watchdog_restarts_after_crash() -> None:
    event = _Event()
    calls: list[int] = []
    restored: list[_Server] = []

    def restart():
        calls.append(len(calls))
        if len(calls) == 1:
            raise RuntimeError("first attempt fails")
        return _Server(None)

    def on_restart(server) -> None:
        restored.append(server)
        event.stopped = True

    result = supervise(
        _Server(1),
        restart,
        on_restart,
        event,
        poll_interval=0,
        backoff=(0, 0),
        sleep=lambda seconds: None,
    )
    assert result == "stopped"
    assert len(calls) == 2
    assert len(restored) == 1


def test_watchdog_reports_failure_after_attempts() -> None:
    event = _Event()
    calls: list[int] = []

    def restart():
        calls.append(len(calls))
        raise RuntimeError("always")

    result = supervise(
        _Server(1),
        restart,
        lambda server: None,
        event,
        poll_interval=0,
        backoff=(0, 0),
        sleep=lambda seconds: None,
    )
    assert result == "failed"
    assert len(calls) == 2


def test_watchdog_does_nothing_after_stop() -> None:
    calls: list[int] = []
    result = supervise(
        _Server(1),
        lambda: calls.append(1),
        lambda server: None,
        _Event(stopped=True),
        poll_interval=0,
        sleep=lambda seconds: None,
    )
    assert result == "stopped"
    assert calls == []
