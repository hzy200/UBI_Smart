"""M4：steer 的幂等键。"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

from ubismart.agent_protocol import AgentContext
from ubismart.config import Settings
from ubismart.control.idempotency import IdempotencyStore
from ubismart.control.schema import Base
from ubismart.web.auth import AuthUser
from ubismart.web.dependencies import current_user
from ubismart.web.routes.runs import RunDeps, register_run_routes


class _Agent:
    def __init__(self) -> None:
        self.calls = 0

    async def steer(self, context, thread_id, **kwargs):
        self.calls += 1
        return {"ok": True, "action": kwargs["action"]}


async def _noop(*_args, **_kwargs) -> None:
    return None


def _client(tmp_path) -> tuple[TestClient, _Agent, IdempotencyStore]:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'control.db'}", future=True
    )
    Base.metadata.create_all(engine)
    idempotency = IdempotencyStore(engine)
    agent = _Agent()
    app = FastAPI()
    register_run_routes(
        app,
        RunDeps(
            settings=Settings(_env_file=None),
            client_source="web",
            stream_buffer=False,
            transport=None,
            streams={},
            registry_lock=threading.Lock(),
            runtimes=SimpleNamespace(contains=lambda _key: False),
            rate_limit=lambda _user: None,
            sweep_expired=_noop,
            tenant=lambda _user: SimpleNamespace(
                user="alice", thread_id=lambda thread: thread
            ),
            config_for=lambda *_a, **_k: SimpleNamespace(
                workspace="/ws"
            ),
            set_config=lambda *_a: None,
            reject_deleted=lambda *_a: None,
            agent_client=agent,
            agent_context=lambda *_a, **_k: AgentContext(
                workspace="/ws"
            ),
            agent_failure=lambda exc: HTTPException(
                status_code=502, detail=str(exc)
            ),
            proxy_agent=_noop,
            stream_guard=lambda *_a, **_k: None,
            start_stream=lambda *_a, **_k: None,
            buffered_sse=lambda *_a, **_k: None,
            sse_response=lambda *_a, **_k: None,
            get_entry=lambda *_a: None,
            runtime_for_resume=_noop,
            new_runtime=lambda *_a: None,
            event_key=lambda *_a: "",
            transport_sse=lambda *_a, **_k: None,
            ops_runtime=lambda *_a: None,
            idempotency=idempotency,
        ),
    )
    app.dependency_overrides[current_user] = lambda: AuthUser(
        name="alice"
    )
    return TestClient(app), agent, idempotency


def test_same_key_replays(tmp_path) -> None:
    client, agent, _idem = _client(tmp_path)
    headers = {"Idempotency-Key": "steer-1"}
    first = client.post(
        "/api/steer/t1",
        json={"action": "pause"},
        headers=headers,
    )
    second = client.post(
        "/api/steer/t1",
        json={"action": "pause"},
        headers=headers,
    )
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert agent.calls == 1


def test_no_key_does_not_dedupe(tmp_path) -> None:
    client, agent, _idem = _client(tmp_path)
    client.post("/api/steer/t1", json={"action": "pause"})
    client.post("/api/steer/t1", json={"action": "pause"})
    assert agent.calls == 2


def test_in_flight_key_is_409(tmp_path) -> None:
    client, _agent, idem = _client(tmp_path)
    assert idem.begin("steer-2", "run.steer") is None
    response = client.post(
        "/api/steer/t1",
        json={"action": "pause"},
        headers={"Idempotency-Key": "steer-2"},
    )
    assert response.status_code == 409
