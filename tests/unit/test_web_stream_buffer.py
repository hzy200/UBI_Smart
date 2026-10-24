"""缓冲 SSE：运行由后台任务持有，客户端可以按序号重放。"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from coding_agent import web
from coding_agent.config import Settings
from coding_agent.control.transport import MemoryEventTransport
from coding_agent.events import (
    ApprovalRequested,
    AssistantToken,
    RunFinished,
    RunStarted,
)
from coding_agent.web import local_runtime


class FakeRuntime:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        approval: bool = False,
    ) -> None:
        self.error = error
        self.approval = approval
        self.closed = False
        self.resumed = False

    async def run(self, prompt: str, *, thread_id: str):
        yield RunStarted(thread_id=thread_id)
        yield AssistantToken(node="respond", text="hello")
        if self.error is not None:
            raise self.error
        if self.approval:
            yield ApprovalRequested(
                request_id="c1", tool="shell_exec", command="ls"
            )
            return
        yield RunFinished(thread_id=thread_id, answer="hello")

    async def resume(self, thread_id: str, decisions: dict[str, bool]):
        self.resumed = True
        yield RunFinished(thread_id=thread_id, answer="resumed")

    def is_paused(self, thread_id: str) -> bool:
        return False

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
    )


def _frames(text: str) -> list[tuple[int | None, dict]]:
    frames: list[tuple[int | None, dict]] = []
    for frame in text.split("\n\n"):
        seq = None
        data = []
        for line in frame.splitlines():
            if line.startswith("id:"):
                seq = int(line[3:].strip())
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data:
            frames.append((seq, json.loads("\n".join(data))))
    return frames


def test_buffered_stream_has_sequence_ids(settings, monkeypatch) -> None:
    runtime = FakeRuntime()
    monkeypatch.setattr(local_runtime, "AgentRuntime", lambda *a, **k: runtime)
    client = TestClient(web.create_app(settings, stream_buffer=True))

    response = client.post("/api/run", json={"prompt": "hi", "thread_id": "t"})
    frames = _frames(response.text)
    assert [seq for seq, _ in frames] == list(
        range(1, len(frames) + 1)
    )
    assert frames[-1][1]["type"] == "run_finished"
    assert runtime.closed is True

    status = client.get("/api/status/t").json()
    assert status["last_seq"] == len(frames)
    assert status["streaming"] is False


def test_buffered_stream_replays_after_sequence(settings, monkeypatch) -> None:
    monkeypatch.setattr(
        local_runtime, "AgentRuntime", lambda *a, **k: FakeRuntime()
    )
    client = TestClient(web.create_app(settings, stream_buffer=True))
    client.post("/api/run", json={"prompt": "hi", "thread_id": "t"})

    replay = client.get("/api/stream/t?after=1")
    frames = _frames(replay.text)
    assert frames
    assert all(seq is not None and seq > 1 for seq, _ in frames)
    assert frames[-1][1]["type"] == "run_finished"


def test_buffered_stream_turns_exceptions_into_run_failed(
    settings, monkeypatch
) -> None:
    runtime = FakeRuntime(error=RuntimeError("boom"))
    monkeypatch.setattr(local_runtime, "AgentRuntime", lambda *a, **k: runtime)
    client = TestClient(web.create_app(settings, stream_buffer=True))

    response = client.post("/api/run", json={"prompt": "hi", "thread_id": "t"})
    frames = _frames(response.text)
    assert frames[-1][1]["type"] == "run_failed"
    assert "boom" in frames[-1][1]["message"]
    assert runtime.closed is True


def test_buffered_approval_keeps_runtime_for_resume(
    settings, monkeypatch
) -> None:
    runtime = FakeRuntime(approval=True)
    monkeypatch.setattr(local_runtime, "AgentRuntime", lambda *a, **k: runtime)
    client = TestClient(web.create_app(settings, stream_buffer=True))

    response = client.post("/api/run", json={"prompt": "hi", "thread_id": "t"})
    run_frames = _frames(response.text)
    assert run_frames[-1][1]["type"] == "approval_requested"
    assert runtime.closed is False
    last_seq = run_frames[-1][0]

    status = client.get("/api/status/t").json()
    assert status["exists"] is True
    assert status["streaming"] is False

    resumed = client.post(
        "/api/resume/t", json={"decisions": {"c1": True}}
    )
    resumed_frames = _frames(resumed.text)
    assert resumed_frames[-1][1]["type"] == "run_finished"
    assert resumed_frames[0][0] == last_seq + 1
    assert runtime.resumed is True
    assert runtime.closed is True


def test_buffer_evicts_old_events_by_size(settings, monkeypatch) -> None:
    class BigRuntime(FakeRuntime):
        async def run(self, prompt: str, *, thread_id: str):
            yield RunStarted(thread_id=thread_id)
            yield AssistantToken(node="act", text="x" * 300)
            yield AssistantToken(node="act", text="y" * 300)
            yield RunFinished(thread_id=thread_id, answer="done")

    monkeypatch.setattr(web.app, "_STREAM_BUFFER_MAX_CHARS", 500)
    monkeypatch.setattr(local_runtime, "AgentRuntime", lambda *a, **k: BigRuntime())
    client = TestClient(web.create_app(settings, stream_buffer=True))
    client.post("/api/run", json={"prompt": "hi", "thread_id": "t"})

    replay = _frames(client.get("/api/stream/t?after=0").text)
    assert replay
    assert replay[0][0] > 1
    assert replay[-1][1]["type"] == "run_finished"


def test_resume_rejects_a_finished_stream(settings, monkeypatch) -> None:
    monkeypatch.setattr(
        local_runtime, "AgentRuntime", lambda *a, **k: FakeRuntime()
    )
    client = TestClient(web.create_app(settings, stream_buffer=True))
    client.post("/api/run", json={"prompt": "hi", "thread_id": "t"})
    response = client.post("/api/resume/t", json={"decisions": {}})
    assert response.status_code == 409


def test_stream_endpoint_requires_buffer_mode(settings, monkeypatch) -> None:
    monkeypatch.setattr(
        local_runtime, "AgentRuntime", lambda *a, **k: FakeRuntime()
    )
    client = TestClient(web.create_app(settings))
    assert client.get("/api/stream/t").status_code == 404


def test_transport_replays_after_local_stream_is_gone(
    settings, monkeypatch
) -> None:
    transport = MemoryEventTransport()
    monkeypatch.setattr(
        local_runtime, "AgentRuntime", lambda *a, **k: FakeRuntime()
    )
    first = TestClient(
        web.create_app(
            settings, stream_buffer=True, event_transport=transport
        )
    )
    frames = _frames(
        first.post("/api/run", json={"prompt": "hi", "thread_id": "t"}).text
    )
    last = frames[-1][0]

    # 新 app 实例模拟 A2 重启：本地 streams 为空，事件从传输层重放。
    restarted = TestClient(
        web.create_app(
            settings, stream_buffer=True, event_transport=transport
        )
    )
    replay = _frames(restarted.get("/api/stream/t?after=1").text)
    assert [seq for seq, _ in replay] == list(range(2, last + 1))
    assert replay[-1][1]["type"] == "run_finished"

    assert (
        restarted.get(f"/api/stream/t?after={last}").status_code == 204
    )


def test_transport_sequence_continues_after_restart(
    settings, monkeypatch
) -> None:
    transport = MemoryEventTransport()
    monkeypatch.setattr(
        local_runtime, "AgentRuntime", lambda *a, **k: FakeRuntime()
    )
    first = TestClient(
        web.create_app(
            settings, stream_buffer=True, event_transport=transport
        )
    )
    first_frames = _frames(
        first.post("/api/run", json={"prompt": "hi", "thread_id": "t"}).text
    )
    last = first_frames[-1][0]

    restarted = TestClient(
        web.create_app(
            settings, stream_buffer=True, event_transport=transport
        )
    )
    second_frames = _frames(
        restarted.post(
            "/api/run", json={"prompt": "again", "thread_id": "t"}
        ).text
    )
    assert second_frames[0][0] == last + 1
    assert second_frames[-1][1]["type"] == "run_finished"


def test_transport_replay_stops_at_approval(settings, monkeypatch) -> None:
    class ApprovalRuntime(FakeRuntime):
        async def run(self, prompt: str, *, thread_id: str):
            yield RunStarted(thread_id=thread_id)
            yield ApprovalRequested(
                request_id="c1", tool="shell_exec", command="ls"
            )

    transport = MemoryEventTransport()
    monkeypatch.setattr(
        local_runtime, "AgentRuntime", lambda *a, **k: ApprovalRuntime()
    )
    first = TestClient(
        web.create_app(
            settings, stream_buffer=True, event_transport=transport
        )
    )
    first.post("/api/run", json={"prompt": "hi", "thread_id": "t"})

    restarted = TestClient(
        web.create_app(
            settings, stream_buffer=True, event_transport=transport
        )
    )
    frames = _frames(restarted.get("/api/stream/t").text)
    assert frames[-1][1]["type"] == "approval_requested"
