"""运行路由的配额、租约、代理与错误分支（fake RunDeps，不碰 WSL/模型）。"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from ubismart.config import Settings
from ubismart.control.leases import LeaseHeld
from ubismart.control.store import QuotaExceeded
from ubismart.web.auth import AuthUser
from ubismart.web.dependencies import current_user
from ubismart.web.models import SessionConfig
from ubismart.web.routes.runs import RunDeps, register_run_routes


class _Leases:
    def __init__(self) -> None:
        self.conflict = False
        self.released: list[str] = []
        self.renewed = 0

    def acquire(self, key: str, owner: str, *, ttl_s: int, lease_id: str = ""):
        if self.conflict:
            raise LeaseHeld("held")
        return lease_id or "lease-1"

    def active(self, key: str):
        return {"lease_id": "lease-1"}

    def release(self, key: str, lease_id: str) -> None:
        self.released.append(lease_id)

    def renew(self, key: str, lease_id: str, *, ttl_s: int) -> None:
        self.renewed += 1


class _Control:
    def __init__(self) -> None:
        self.reserved: list[dict] = []
        self.settled: list[str] = []
        self.raise_quota = False

    def reserve_quota(self, *args, **kwargs):
        if self.raise_quota:
            raise QuotaExceeded({"kind": "tokens", "limit": 1, "used": 1})
        row = {"id": "r1"}
        self.reserved.append(row)
        return row

    def settle_quota(self, reservation_id: str) -> None:
        self.settled.append(reservation_id)


class _Runtimes:
    contains_value = False

    def contains(self, key) -> bool:
        return self.contains_value

    def put(self, key, runtime) -> None:
        pass


class _Runtime:
    def __init__(self) -> None:
        self.closed = False
        self.steer_error: Exception | None = None

    async def run(self, prompt: str, *, thread_id: str):
        yield SimpleNamespace(type="run_finished")

    def steer(self, *args, **kwargs) -> None:
        if self.steer_error is not None:
            raise self.steer_error

    async def history(self, thread_id, *, limit=None, offset=0):
        return [SimpleNamespace(role="user", text="hi")]

    async def aclose(self) -> None:
        self.closed = True


class _AgentClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.error: Exception | None = None
        self.approvals_rows: list[dict] = []
        self.history_data = {"messages": [{"role": "user", "text": "hi"}]}

    async def open_run(self, request):
        self.calls.append("open_run")
        if self.error:
            raise self.error
        return object()

    async def open_resume(self, *args, **kwargs):
        self.calls.append("open_resume")
        return object()

    async def open_resume_steering(self, *args, **kwargs):
        self.calls.append("open_resume_steering")
        return object()

    async def approvals(self, context, thread_id):
        self.calls.append("approvals")
        if self.error:
            raise self.error
        return {"approvals": self.approvals_rows}

    async def status(self, context, thread_id):
        self.calls.append("status")
        if self.error:
            raise self.error
        return {"exists": True, "paused": False}

    async def history(self, context, thread_id, *, limit=None, offset=0):
        self.calls.append("history")
        if self.error:
            raise self.error
        return self.history_data

    async def open_stream(self, *args, **kwargs):
        self.calls.append("open_stream")
        if self.error:
            raise self.error
        return object()

    async def steer(self, *args, **kwargs):
        self.calls.append("steer")
        if self.error:
            raise self.error


class _Approvals:
    def merge_decisions(self, **kwargs):
        return {"c1": True}


def _deps(**overrides) -> RunDeps:
    runtime = _Runtime()
    client = _AgentClient()
    leases = _Leases()
    control = _Control()
    values: dict[str, Any] = {
        "settings": Settings(_env_file=None),
        "client_source": "web",
        "stream_buffer": True,
        "transport": SimpleNamespace(latest_seq=lambda key: 0),
        "streams": {},
        "registry_lock": threading.Lock(),
        "runtimes": _Runtimes(),
        "rate_limit": lambda _user: None,
        "sweep_expired": _async_noop,
        "tenant": lambda _user: SimpleNamespace(
            user="alice",
            workspace="/tmp/ws",
            thread_id=lambda value: value,
        ),
        "config_for": lambda *_a, **_k: SessionConfig(
            thread_id="t", workspace="/tmp/ws", access_mode="read_only"
        ),
        "set_config": lambda *_a, **_k: None,
        "reject_deleted": lambda *_a: None,
        "agent_client": client,
        "agent_context": lambda *_a, **_k: SimpleNamespace(),
        "agent_failure": lambda exc: HTTPException(
            status_code=502, detail=str(exc)
        ),
        "proxy_agent": _proxy,
        "stream_guard": lambda *_a, **_k: None,
        "start_stream": lambda *_a, **_k: object(),
        "buffered_sse": lambda *_a, **_k: _response(),
        "sse_response": lambda *_a, **_k: _response(),
        "get_entry": lambda *_a: None,
        "runtime_for_resume": _resume_tuple(runtime),
        "new_runtime": lambda *_a, **_k: runtime,
        "event_key": lambda *_a: "k:t",
        "transport_sse": lambda *_a, **_k: _response(),
        "ops_runtime": lambda *_a, **_k: runtime,
        "run_leases": leases,
        "lease_owner": "app-1",
        "lease_ttl_s": 60,
        "idempotency": None,
        "control_store": control,
        "quota_reserve_tokens": 10,
        "quota_owner": "app-1",
        "approvals": _Approvals(),
        "four_eyes_min_level": 3,
        "record_approval": lambda *_a: None,
    }
    values.update(overrides)
    return RunDeps(**values), runtime, client, leases, control


async def _async_noop(*_args, **_kwargs):
    return None


async def _resume_tuple(runtime: _Runtime):
    return ("k:t", runtime)


async def _proxy(stream, **kwargs):
    on_event = kwargs.get("on_event")
    if on_event is not None:
        await on_event({"type": "approval_requested", "request_id": "c1"})
    on_terminal = kwargs.get("on_terminal")
    if on_terminal is not None:
        on_terminal()

    async def body():
        yield b'data: {"type":"run_finished"}\n\n'

    return StreamingResponse(body(), media_type="text/event-stream")


def _response() -> StreamingResponse:
    async def body():
        yield b'data: {"type":"run_finished"}\n\n'

    return StreamingResponse(body(), media_type="text/event-stream")


def _client(deps: RunDeps) -> TestClient:
    app = FastAPI()
    register_run_routes(app, deps)
    app.dependency_overrides[current_user] = lambda: AuthUser(
        name="alice", tenant="default"
    )
    return TestClient(app, raise_server_exceptions=False)


def test_run_rejects_long_prompt_and_active_runtime() -> None:
    deps, runtime, _client_agent, _leases, _control = _deps(
        agent_client=None
    )
    client = _client(deps)
    assert client.post(
        "/api/run",
        json={"prompt": "x" * (deps.settings.web_max_prompt_chars + 1)},
    ).status_code == 413

    deps.runtimes.contains_value = True
    assert client.post(
        "/api/run", json={"prompt": "hi", "thread_id": "t"}
    ).status_code == 409


def test_run_quota_exceeded_and_release_on_stream_error() -> None:
    deps, _runtime, _agent, _leases, control = _deps()
    control.raise_quota = True
    client = _client(deps)
    response = client.post(
        "/api/run", json={"prompt": "hi", "thread_id": "t"}
    )
    assert response.status_code == 429

    deps, _runtime, _agent, _leases, control = _deps(
        agent_client=None,
        start_stream=lambda *_a, **_k: (_ for _ in ()).throw(
            RuntimeError("stream broke")
        ),
    )
    client = _client(deps)
    assert client.post(
        "/api/run", json={"prompt": "hi", "thread_id": "t"}
    ).status_code == 500
    assert control.settled == ["r1"]


def test_run_via_agent_client_reserves_and_releases() -> None:
    deps, _runtime, agent, leases, control = _deps()
    client = _client(deps)
    response = client.post(
        "/api/run", json={"prompt": "hi", "thread_id": "t"}
    )
    assert response.status_code == 200
    assert agent.calls[-1] == "open_run"
    assert control.settled == ["r1"]
    assert leases.released == ["lease-1"]


def test_lease_conflict_returns_409_and_idempotent_detail() -> None:
    deps, _runtime, _agent, leases, _control = _deps()
    leases.conflict = True
    client = _client(deps)
    response = client.post(
        "/api/run",
        headers={"Idempotency-Key": "lease-1"},
        json={"prompt": "hi", "thread_id": "t"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["idempotent"] is True


def test_resume_merges_approvals_and_records() -> None:
    recorded: list[dict] = []
    deps, _runtime, agent, _leases, _control = _deps(
        record_approval=lambda *args: recorded.append(args[-1])
    )
    client = _client(deps)
    response = client.post(
        "/api/resume/t", json={"decisions": {"c1": False}}
    )
    assert response.status_code == 200
    assert agent.calls[-1] == "open_resume"
    assert recorded and recorded[0]["request_id"] == "c1"


def test_status_history_and_approvals_via_agent_client(monkeypatch) -> None:
    import ubismart.web.routes.runs as runs

    monkeypatch.setattr(
        runs, "load_transcript", lambda *_a, **_k: ([], False)
    )
    deps, _runtime, agent, _leases, _control = _deps()
    client = _client(deps)
    assert client.get("/api/status/t").json()["exists"] is True
    history = client.get("/api/thread/t")
    assert history.status_code == 200, (history.status_code, history.text)
    assert history.json()["messages"][0]["text"] == "hi"
    assert client.get("/api/approvals/t").json() == {"approvals": []}

    agent.error = RuntimeError("agent down")
    assert client.get("/api/status/t").status_code == 502
    assert client.get("/api/thread/t").status_code == 502
    assert client.get("/api/approvals/t").status_code == 502


def test_steer_errors_are_mapped() -> None:
    runtime = _Runtime()
    deps, runtime, _agent, _leases, _control = _deps(
        agent_client=None,
        get_entry=lambda *_a: SimpleNamespace(runtime=runtime),
    )
    client = _client(deps)
    runtime.steer_error = ValueError("bad action")
    assert client.post(
        "/api/steer/t", json={"action": "pause"}
    ).status_code == 422

    runtime.steer_error = RuntimeError("boom")
    assert client.post(
        "/api/steer/t", json={"action": "pause"}
    ).status_code == 502
