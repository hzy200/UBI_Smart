"""A3 host 工作区：上下文字段与 runtime 自动注入。"""

from __future__ import annotations

from dataclasses import dataclass

from coding_agent.access import ACCESS_READ_ONLY
from coding_agent.agent_context import AgentRuntimeFactory
from coding_agent.agent_protocol import AgentContext
from coding_agent.config import Settings


class FakeCaller:
    def call(self, kind, *, workspace, payload, grant_id=""):
        return {}


class FakeAcceptor:
    def __init__(self) -> None:
        self.caller = FakeCaller()

    def caller_for(self, a2l_id):
        return self.caller if a2l_id == "a2l-1" else None


@dataclass
class FakeRuntime:
    pass


def _factory(tmp_path):
    captured = {}

    def build(settings, **kwargs):
        captured.update(kwargs)
        return FakeRuntime()

    settings = Settings(
        _env_file=None, AGENT_AGENT_DATA_ROOT=str(tmp_path)
    )
    factory = AgentRuntimeFactory(
        settings, build, host_broker_acceptor=FakeAcceptor()
    )
    return factory, captured


def test_host_context_roundtrip() -> None:
    context = AgentContext(
        workspace="",
        workspace_id="ws-1",
        workspace_kind="host",
        a2l_id="a2l-1",
        root_handle="h-1",
    )
    restored = AgentContext.from_header(context.to_header())
    assert restored.workspace_kind == "host"
    assert restored.a2l_id == "a2l-1"
    assert restored.root_handle == "h-1"


def test_factory_injects_host_file_access_and_forces_read_only(tmp_path) -> None:
    factory, captured = _factory(tmp_path)
    context = AgentContext(
        workspace="",
        workspace_id="ws-1",
        workspace_kind="host",
        a2l_id="a2l-1",
        root_handle="h-1",
        access_mode="write_auto",
        allow_write=True,
    )
    factory.new(context)
    assert captured["workspace_kind"] == "host"
    assert captured["allow_write"] is False
    assert captured["access_mode"] == ACCESS_READ_ONLY
    assert captured["file_access"] is not None
    assert "host" in captured["execution_targets"]
    assert str(captured["workspace"]).endswith("host-workspace")


def test_factory_fails_closed_without_caller(tmp_path) -> None:
    factory, _ = _factory(tmp_path)
    context = AgentContext(
        workspace="",
        workspace_id="ws-1",
        workspace_kind="host",
        a2l_id="missing",
        root_handle="h-1",
    )
    try:
        factory.new(context)
    except Exception as exc:  # HTTPException
        assert "A2L" in str(exc.detail if hasattr(exc, "detail") else exc)
    else:  # pragma: no cover
        raise AssertionError("缺少 caller 时必须拒绝")
