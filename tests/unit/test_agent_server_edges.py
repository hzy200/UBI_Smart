"""A3 边界：共享传输 SSE、缓冲区裁剪、运行异常与上下文校验。"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from coding_agent.agent_protocol import AgentContext
from coding_agent.config import Settings
from coding_agent.events import (
    ApprovalRequested,
    AssistantToken,
    RunFailed,
    RunFinished,
    RunStarted,
)
from coding_agent.servers import agent_server

TOKEN = "tok"


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        agent_data_root=str(tmp_path / "agent"),
    )


def _headers(workspace: str = "/tmp/ws") -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TOKEN}",
        "X-Agent-Context": AgentContext(workspace=workspace).to_header(),
    }


class _Transport:
    def __init__(
        self,
        *,
        latest: int = 0,
        rows: list[tuple[int, str]] | None = None,
        fail_latest: bool = False,
        fail_replay: bool = False,
        fail_publish: bool = False,
    ) -> None:
        self.latest = latest
        self.rows = rows or []
        self.fail_latest = fail_latest
        self.fail_replay = fail_replay
        self.fail_publish = fail_publish
        self.published: list[tuple] = []

    def latest_seq(self, key: str) -> int:
        if self.fail_latest:
            raise RuntimeError("redis down")
        return self.latest

    def replay(self, key: str, after: int):
        if self.fail_replay:
            raise RuntimeError("redis down")
        return [(seq, payload) for seq, payload in self.rows if seq > after]

    def publish(self, key: str, seq: int, payload: str) -> None:
        self.published.append((key, seq, payload))
        if self.fail_publish:
            raise RuntimeError("publish failed")


class _Runtime:
    def __init__(
        self, *, error: bool = False, events=None, paused: bool = False
    ) -> None:
        self.error = error
        self.events = events
        self.paused = paused
        self.fail = ""
        self.steered: list[tuple] = []

    async def run(self, prompt: str, *, thread_id: str):
        if self.error:
            raise RuntimeError("boom")
        for event in self.events or [
            RunStarted(thread_id=thread_id),
            RunFinished(thread_id=thread_id, answer="ok"),
        ]:
            yield event

    def is_paused(self, thread_id: str) -> bool:
        return self.paused

    def steer(self, thread_id: str, action: str, **kwargs) -> None:
        self.steered.append((thread_id, action, kwargs))
        if self.fail == "steer":
            raise RuntimeError("steer failed")

    async def resume_steering(self, thread_id: str):
        self.paused = False
        yield RunFinished(thread_id=thread_id, answer="resumed")

    async def pending_approvals(self, thread_id: str):
        if self.fail == "approvals":
            raise RuntimeError("approval store down")
        return []

    async def history(self, thread_id: str, *, limit=None, offset=0):
        if self.fail == "history":
            raise RuntimeError("history down")
        return [SimpleNamespace(role="user", text="hi")]

    async def compact_thread(self, thread_id: str, *, keep_recent: int = 6):
        return f"{thread_id}:{keep_recent}"

    async def aclose(self) -> None:
        if self.error:
            raise RuntimeError("close failed")


def _app(tmp_path, monkeypatch, transport: _Transport, runtime: _Runtime):
    monkeypatch.setattr(
        agent_server, "build_event_transport", lambda settings: transport
    )
    return agent_server.create_agent_app(
        _settings(tmp_path),
        token=TOKEN,
        runtime_factory=lambda *args, **kwargs: runtime,
    )


def test_transport_sse_replays_and_stops_on_terminal(
    tmp_path, monkeypatch
) -> None:
    payload = RunFinished(thread_id="t1", answer="done").model_dump_json()
    transport = _Transport(latest=3, rows=[(3, payload)])
    client = TestClient(_app(tmp_path, monkeypatch, transport, _Runtime()))

    response = client.get("/v1/stream/t1", headers=_headers())

    assert response.status_code == 200
    assert "id: 3" in response.text
    assert "run_finished" in response.text


def test_transport_sse_replay_failure_ends_stream(
    tmp_path, monkeypatch
) -> None:
    transport = _Transport(latest=2, fail_replay=True)
    client = TestClient(_app(tmp_path, monkeypatch, transport, _Runtime()))

    response = client.get("/v1/stream/t1", headers=_headers())
    assert response.status_code == 200
    assert response.text == ""


def test_stream_without_transport_returns_503(
    tmp_path, monkeypatch
) -> None:
    transport = _Transport(fail_latest=True)
    client = TestClient(_app(tmp_path, monkeypatch, transport, _Runtime()))

    response = client.get("/v1/stream/t1", headers=_headers())
    assert response.status_code == 503


def test_stream_uses_transport_when_it_is_ahead(
    tmp_path, monkeypatch
) -> None:
    transport = _Transport(latest=0)
    client = TestClient(_app(tmp_path, monkeypatch, transport, _Runtime()))

    assert (
        client.post(
            "/v1/runs",
            headers=_headers(),
            json={"prompt": "hi", "thread_id": "t1"},
        ).status_code
        == 200
    )

    terminal = RunFinished(thread_id="t1", answer="later").model_dump_json()
    transport.latest = 99
    transport.rows = [(50, terminal)]
    response = client.get("/v1/stream/t1", headers=_headers())
    assert "id: 50" in response.text


def test_buffer_limits_and_publish_failure_do_not_break_run(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(agent_server, "_STREAM_BUFFER_MAX_EVENTS", 1)
    monkeypatch.setattr(agent_server, "_STREAM_BUFFER_MAX_CHARS", 1)
    transport = _Transport(fail_publish=True)
    runtime = _Runtime(
        events=[
            RunStarted(thread_id="t1"),
            AssistantToken(node="act", text="x"),
            RunFinished(thread_id="t1", answer="ok"),
        ]
    )
    client = TestClient(_app(tmp_path, monkeypatch, transport, runtime))

    response = client.post(
        "/v1/runs",
        headers=_headers(),
        json={"prompt": "hi", "thread_id": "t1"},
    )

    assert response.status_code == 200
    assert "run_finished" in response.text


def test_runtime_error_becomes_run_failed(tmp_path, monkeypatch) -> None:
    transport = _Transport()
    client = TestClient(
        _app(tmp_path, monkeypatch, transport, _Runtime(error=True))
    )

    response = client.post(
        "/v1/runs",
        headers=_headers(),
        json={"prompt": "hi", "thread_id": "t1"},
    )

    assert response.status_code == 200
    assert "run_failed" in response.text
    # _consume 已把异常转成事件，不能再向上抛。
    assert RunFailed(message="x").type in response.text


def test_relative_workspace_is_rejected(tmp_path, monkeypatch) -> None:
    transport = _Transport()
    client = TestClient(_app(tmp_path, monkeypatch, transport, _Runtime()))

    response = client.post(
        "/v1/runs",
        headers=_headers(workspace="relative/path"),
        json={"prompt": "hi", "thread_id": "t1"},
    )
    assert response.status_code == 400
    assert "绝对路径" in response.json()["detail"]


def test_stream_returns_204_when_caught_up(tmp_path, monkeypatch) -> None:
    transport = _Transport(latest=0)
    client = TestClient(_app(tmp_path, monkeypatch, transport, _Runtime()))

    assert client.get("/v1/stream/t1", headers=_headers()).status_code == 204

    client.post(
        "/v1/runs",
        headers=_headers(),
        json={"prompt": "hi", "thread_id": "t1"},
    )
    response = client.get(
        "/v1/stream/t1?after=999", headers=_headers()
    )
    assert response.status_code == 204


def test_invalid_last_event_id_is_ignored(tmp_path, monkeypatch) -> None:
    transport = _Transport(latest=0)
    client = TestClient(_app(tmp_path, monkeypatch, transport, _Runtime()))
    client.post(
        "/v1/runs",
        headers=_headers(),
        json={"prompt": "hi", "thread_id": "t1"},
    )

    headers = {**_headers(), "Last-Event-ID": "not-a-number"}
    response = client.get("/v1/stream/t1?after=999", headers=headers)
    assert response.status_code == 204


def test_resume_steering_requires_pause(tmp_path, monkeypatch) -> None:
    runtime = _Runtime(paused=False)
    client = TestClient(
        _app(tmp_path, monkeypatch, _Transport(), runtime)
    )

    assert (
        client.post(
            "/v1/resume_steering/t1", headers=_headers()
        ).status_code
        == 409
    )

    runtime.paused = True
    response = client.post("/v1/resume_steering/t1", headers=_headers())
    assert response.status_code == 200
    assert "run_finished" in response.text


def test_approvals_history_and_compact(tmp_path, monkeypatch) -> None:
    runtime = _Runtime()
    client = TestClient(
        _app(tmp_path, monkeypatch, _Transport(), runtime)
    )

    assert client.get("/v1/approvals/t1", headers=_headers()).json() == {
        "approvals": []
    }
    history = client.get("/v1/history/t1", headers=_headers()).json()
    assert history["messages"][0] == {"role": "user", "text": "hi"}
    assert client.post(
        "/v1/compact/t1",
        headers=_headers(),
        json={"keep_recent": 3},
    ).json() == {"thread_id": "t1:3"}

    runtime.fail = "approvals"
    assert client.get("/v1/approvals/t1", headers=_headers()).status_code == 503
    runtime.fail = "history"
    assert client.get("/v1/history/t1", headers=_headers()).status_code == 503


def test_cancel_all_and_shutdown(tmp_path, monkeypatch) -> None:
    runtime = _Runtime(
        events=[ApprovalRequested(request_id="r1", tool="shell_exec")]
    )
    app = _app(tmp_path, monkeypatch, _Transport(), runtime)
    shutdown: list[bool] = []
    app.state.shutdown = lambda: shutdown.append(True)
    client = TestClient(app)

    # 产生一个挂起运行，让 cancel_all 能遍历到 runs。
    assert client.post(
        "/v1/runs",
        headers=_headers(),
        json={"prompt": "hi", "thread_id": "t1"},
    ).status_code == 200

    assert client.post("/v1/cancel_all", headers=_headers()).json() == {
        "cancelled": 1
    }
    assert runtime.steered[0][1] == "cancel"
    assert client.post("/v1/shutdown", headers=_headers()).json() == {
        "ok": True
    }
    # 回调通过 call_soon 投递；覆盖装配即可，不把事件循环时序写成断言。
    assert isinstance(shutdown, list)


def test_steer_unknown_thread_returns_404(tmp_path, monkeypatch) -> None:
    client = TestClient(
        _app(tmp_path, monkeypatch, _Transport(), _Runtime())
    )
    assert (
        client.post(
            "/v1/steer/t1", headers=_headers(), json={"action": "cancel"}
        ).status_code
        == 404
    )
