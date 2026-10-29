"""P1-5：A3 收到 SIGTERM 后的优雅 drain。

drain 的边界：停止接新运行，等待进行中的运行自然结束；超过
`AGENT_DRAIN_TIMEOUT_S` 仍未结束才取消。取消不是常规路径，所以要验证
它确实发生、且 `/readyz` 提前摘流量。
"""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from coding_agent import agent_server
from coding_agent.agent_protocol import AgentContext
from coding_agent.agent_server_support import RunState
from coding_agent.config import Settings
from coding_agent.events import RunFinished, RunStarted


class _HangingRuntime:
    instances: list[_HangingRuntime] = []

    def __init__(self, *args, **kwargs) -> None:
        self.closed = False
        _HangingRuntime.instances.append(self)

    async def run(self, prompt: str, *, thread_id: str):
        yield RunStarted(thread_id=thread_id)
        await asyncio.Event().wait()  # 永不结束，模拟长跑

    async def resume(self, thread_id: str, decisions: dict[str, bool]):
        yield RunFinished(thread_id=thread_id, answer="resumed")

    async def aclose(self) -> None:
        self.closed = True


class _HealthSandbox:
    def health(self) -> bool:
        return True


def _settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        wsl_workspace="/tmp/ws",
        agent_data_root=str(tmp_path / "agent"),
    )


def _app(tmp_path):
    return agent_server.create_agent_app(
        _settings(tmp_path),
        token="tok",
        runtime_factory=_HangingRuntime,
    )


def _headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer tok",
        "X-Agent-Context": AgentContext(workspace="/tmp/ws").to_header(),
    }


def _sandbox(monkeypatch) -> None:
    monkeypatch.setattr(
        "coding_agent.sandbox.backend.build_sandbox",
        lambda settings: _HealthSandbox(),
    )


def test_draining_rejects_new_runs_and_marks_not_ready(
    tmp_path, monkeypatch
) -> None:
    _sandbox(monkeypatch)
    app = _app(tmp_path)
    client = TestClient(app)
    app.state.draining = True

    run = client.post(
        "/v1/runs", headers=_headers(), json={"prompt": "hi", "thread_id": "t"}
    )
    assert run.status_code == 503
    assert run.json()["detail"]["code"] == "agent.draining"

    resume = client.post(
        "/v1/resume/t",
        headers=_headers(),
        json={"decisions": {}},
    )
    assert resume.status_code == 503

    ready = client.get("/readyz", headers=_headers())
    assert ready.status_code == 503
    assert ready.json()["checks"] == {
        "sandbox": "ok",
        "checkpoint": "in_memory",
        "draining": "true",
    }
    # 读接口不拦：存量会话仍可被观察。
    status = client.get("/v1/status/t", headers=_headers())
    assert status.status_code == 200


def test_drain_cancels_runs_that_exceed_deadline(tmp_path) -> None:
    app = _app(tmp_path)

    async def scenario():
        async def runner() -> None:
            await asyncio.Event().wait()

        task = asyncio.create_task(runner())
        state = RunState(
            key=("t", "thread"), thread_id="thread", runtime=None, task=task
        )
        app.state.states[("t", "thread")] = state
        result = await app.state.drain(0)
        await asyncio.sleep(0)
        return result, task

    result, task = asyncio.run(scenario())
    assert result == {"pending": 1, "cancelled": 1, "completed": 0}
    assert task.cancelled()


def test_drain_waits_for_runs_that_finish_in_time(tmp_path) -> None:
    app = _app(tmp_path)

    async def scenario():
        state = RunState(
            key=("t", "f"), thread_id="f", runtime=None
        )
        app.state.states[("t", "f")] = state

        async def finish() -> None:
            await asyncio.sleep(0.05)
            state.done = True

        state.task = asyncio.create_task(finish())
        return await app.state.drain(5)

    result = asyncio.run(scenario())
    assert result == {"pending": 0, "cancelled": 0, "completed": 1}
