"""Phase B4：独立 Agent Server 与 A2 HTTP 适配。"""

from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient

from coding_agent import web
from coding_agent.config import Settings
from coding_agent.control.agent_client import (
    AgentContext,
    HttpAgentClient,
)
from coding_agent.events import (
    ApprovalRequested,
    AssistantToken,
    RunFinished,
    RunStarted,
)
from coding_agent.sandbox.snapshots import SnapshotEntry
from coding_agent.servers import agent_server
from coding_agent.servers.agent_server_routes import state as state_routes
from coding_agent.servers.agent_server_routes import workspace as workspace_routes


class FakeRuntime:
    instances: list[FakeRuntime] = []

    def __init__(self, *args, **kwargs) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.steered: list[tuple] = []
        self.resumed = False
        FakeRuntime.instances.append(self)

    async def run(self, prompt: str, *, thread_id: str):
        yield RunStarted(thread_id=thread_id)
        yield AssistantToken(node="respond", text="hello")
        if self.kwargs.get("_approval"):
            yield ApprovalRequested(
                request_id="c1", tool="shell_exec", command="ls"
            )
            return
        yield RunFinished(thread_id=thread_id, answer="hello")

    async def resume(self, thread_id: str, decisions: dict[str, bool]):
        self.resumed = True
        yield RunFinished(thread_id=thread_id, answer="resumed")

    async def resume_steering(self, thread_id: str):
        yield RunFinished(thread_id=thread_id, answer="resumed")

    def is_paused(self, thread_id: str) -> bool:
        return False

    async def pending_approvals(self, thread_id: str):
        return []

    async def history(self, thread_id: str, *, limit=None, offset=0):
        return [SimpleNamespace(role="user", text="hi")]

    def steer(self, thread_id: str, action: str, **kwargs) -> None:
        self.steered.append((thread_id, action))

    def list_snapshots(self, *, limit=20):
        return [SnapshotEntry("s1", "a.py")]

    def diff_snapshot(self, *, path=None, snapshot_id=None):
        return "diff"

    def memories(self, *, scope=None):
        return ["fact"]

    def remember(self, text, *, thread_id="", scope="project"):
        return [text]

    def store_upload(self, filename, data, *, thread_id="", max_bytes=0):
        return {
            "path": f"/tmp/ws/.agent/uploads/{thread_id}/{filename}",
            "relative_path": f".agent/uploads/{thread_id}/{filename}",
            "bytes": len(data),
        }

    async def aclose(self) -> None:
        self.closed = True


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        agent_data_root=str(tmp_path / "agent"),
    )


def _headers(context: AgentContext | None = None) -> dict[str, str]:
    ctx = context or AgentContext(workspace="/tmp/ws")
    return {
        "Authorization": "Bearer tok",
        "X-Agent-Context": ctx.to_header(),
    }


def _frames(text: str) -> list[tuple[int, dict]]:
    frames: list[tuple[int, dict]] = []
    for frame in text.split("\n\n"):
        seq = None
        data = []
        for line in frame.splitlines():
            if line.startswith("id:"):
                seq = int(line[3:].strip())
            elif line.startswith("data:"):
                data.append(line[5:].strip())
        if data and seq is not None:
            frames.append((seq, json.loads("\n".join(data))))
    return frames


def _app(tmp_path, monkeypatch, *, approval: bool = False):
    monkeypatch.setattr(
        "pathlib.Path.mkdir",
        lambda self, parents=False, exist_ok=False: None,
    )
    FakeRuntime.instances = []
    settings = _settings(tmp_path)

    def factory(*args, **kwargs):
        kwargs["_approval"] = approval
        return FakeRuntime(*args, **kwargs)

    return agent_server.create_agent_app(
        settings, token="tok", runtime_factory=factory
    )


def test_agent_server_run_and_sse(tmp_path, monkeypatch) -> None:
    app = _app(tmp_path, monkeypatch)
    client = TestClient(app)
    response = client.post(
        "/v1/runs",
        headers=_headers(),
        json={"prompt": "hi", "thread_id": "t1"},
    )
    assert response.status_code == 200
    frames = _frames(response.text)
    assert [seq for seq, _ in frames] == list(range(1, len(frames) + 1))
    assert frames[-1][1]["type"] == "run_finished"
    assert FakeRuntime.instances[0].closed is True

    status = client.get("/v1/status/t1", headers=_headers()).json()
    assert status["exists"] is True
    assert status["streaming"] is False


def test_agent_server_requires_token_and_context(tmp_path, monkeypatch) -> None:
    client = TestClient(_app(tmp_path, monkeypatch))
    assert (
        client.post(
            "/v1/runs", json={"prompt": "x", "thread_id": "t"}
        ).status_code
        == 401
    )
    bad = _headers()
    bad["X-Agent-Context"] = "not-base64"
    assert (
        client.post(
            "/v1/runs",
            headers=bad,
            json={"prompt": "x", "thread_id": "t"},
        ).status_code
        == 400
    )


def test_agent_server_approval_resume_keeps_sequence(
    tmp_path, monkeypatch
) -> None:
    app = _app(tmp_path, monkeypatch, approval=True)
    client = TestClient(app)
    first = client.post(
        "/v1/runs",
        headers=_headers(),
        json={"prompt": "hi", "thread_id": "t1"},
    )
    first_frames = _frames(first.text)
    assert first_frames[-1][1]["type"] == "approval_requested"

    resumed = client.post(
        "/v1/resume/t1",
        headers=_headers(),
        json={"decisions": {"c1": True}},
    )
    resumed_frames = _frames(resumed.text)
    assert resumed_frames[0][0] == first_frames[-1][0] + 1
    assert resumed_frames[-1][1]["type"] == "run_finished"
    assert FakeRuntime.instances[0].resumed is True


def test_agent_server_approvals_status_history(tmp_path, monkeypatch) -> None:
    client = TestClient(_app(tmp_path, monkeypatch))
    client.post(
        "/v1/runs",
        headers=_headers(),
        json={"prompt": "hi", "thread_id": "t1"},
    )
    assert client.get(
        "/v1/approvals/t1", headers=_headers()
    ).json() == {"approvals": []}
    history = client.get(
        "/v1/history/t1", headers=_headers()
    ).json()
    assert history["messages"][0]["text"] == "hi"
    assert client.post(
        "/v1/steer/t1", headers=_headers(), json={"action": "cancel"}
    ).status_code == 404


def test_a2_proxies_lifecycle_to_agent_server(tmp_path, monkeypatch) -> None:
    agent_app = _app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=agent_app)
    client = HttpAgentClient(
        "http://agent", "tok", transport=transport
    )
    settings = _settings(tmp_path)
    a2 = TestClient(
        web.create_app(settings, agent_client=client)
    )

    response = a2.post(
        "/api/run", json={"prompt": "hi", "thread_id": "t1"}
    )
    assert response.status_code == 200
    frames = _frames(response.text)
    assert frames[-1][1]["type"] == "run_finished"
    assert (
        FakeRuntime.instances[0].kwargs["secret_store"].get("deepseek")
        == "sk-test"
    )
    last = frames[-1][0]

    replay = _frames(a2.get("/api/stream/t1?after=1").text)
    assert replay[0][0] == 2
    assert replay[-1][1]["type"] == "run_finished"
    assert a2.get(f"/api/stream/t1?after={last}").status_code == 204

    status = a2.get("/api/status/t1").json()
    assert status["exists"] is True
    history = a2.get("/api/thread/t1").json()
    assert history["messages"][0]["text"] == "hi"
    assert a2.get("/api/approvals/t1").json() == {"approvals": []}
    rows = a2.get("/api/sessions").json()["sessions"]
    assert rows[0]["title"] == "hi"


def test_a2_proxies_files_snapshots_memory_and_audit(
    tmp_path, monkeypatch
) -> None:
    agent_app = _app(tmp_path, monkeypatch)
    monkeypatch.setattr(
        workspace_routes,
        "list_directory",
        lambda workspace, path: {
            "path": path,
            "entries": [{"name": "a.py", "path": "a.py", "is_dir": False, "size": 1}],
            "truncated": False,
        },
    )
    monkeypatch.setattr(
        state_routes, "read_records_many", lambda *args, **kwargs: []
    )
    transport = httpx.ASGITransport(app=agent_app)
    client = HttpAgentClient(
        "http://agent", "tok", transport=transport
    )
    settings = _settings(tmp_path)
    a2 = TestClient(web.create_app(settings, agent_client=client))

    files = a2.get(
        "/api/files", params={"thread_id": "t1", "path": "src"}
    ).json()
    assert files["entries"][0]["name"] == "a.py"

    snapshots = a2.get(
        "/api/snapshots", params={"thread_id": "t1"}
    ).json()
    assert snapshots["snapshots"][0]["snapshot_id"] == "s1"

    assert a2.get("/api/memory").json()["facts"] == ["fact"]
    assert a2.get("/api/audit").json()["records"] == []

    a2.post(
        "/api/sessions",
        json={"thread_id": "t1", "workspace": "/tmp/ws"},
    )
    uploaded = a2.post(
        "/api/sessions/t1/uploads",
        files={"files": ("a.txt", b"hi", "text/plain")},
    ).json()
    assert uploaded["uploads"][0]["bytes"] == 2


def test_http_agent_client_surfaces_agent_errors(tmp_path, monkeypatch) -> None:
    agent_app = _app(tmp_path, monkeypatch)
    transport = httpx.ASGITransport(app=agent_app)
    client = HttpAgentClient(
        "http://agent", "wrong-token", transport=transport
    )
    settings = _settings(tmp_path)
    a2 = TestClient(web.create_app(settings, agent_client=client))
    response = a2.post(
        "/api/run", json={"prompt": "hi", "thread_id": "t1"}
    )
    assert response.status_code == 401
