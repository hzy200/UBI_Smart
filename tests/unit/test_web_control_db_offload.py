"""控制面 DB 热路径不得阻塞事件循环。"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from coding_agent import web
from coding_agent.config import Settings
from coding_agent.control.store import ControlStore


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        control_db_url=f"sqlite:///{tmp_path / 'control.db'}",
    )


def _in_event_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def test_session_db_calls_run_off_the_event_loop(tmp_path, monkeypatch) -> None:
    calls: list[tuple[str, bool]] = []
    original_save = ControlStore.save_sessions
    original_load = ControlStore.load_sessions

    def save_spy(self, *args, **kwargs):
        calls.append(("save", _in_event_loop()))
        return original_save(self, *args, **kwargs)

    def load_spy(self, *args, **kwargs):
        calls.append(("load", _in_event_loop()))
        return original_load(self, *args, **kwargs)

    monkeypatch.setattr(ControlStore, "save_sessions", save_spy)
    monkeypatch.setattr(ControlStore, "load_sessions", load_spy)

    client = TestClient(web.create_app(_settings(tmp_path)))
    created = client.post(
        "/api/sessions", json={"thread_id": "t1", "workspace": "/tmp/ws"}
    )
    assert created.status_code == 200
    listed = client.get("/api/sessions")
    assert listed.status_code == 200

    assert calls, "控制面 DB 调用没有被触发，测试前提不成立"
    assert all(not in_loop for _, in_loop in calls), calls
    assert {kind for kind, _ in calls} >= {"save", "load"}
