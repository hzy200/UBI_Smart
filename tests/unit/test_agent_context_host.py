"""A3 host 工作区：上下文字段与 runtime 自动注入。"""

from __future__ import annotations

from dataclasses import dataclass

from ubismart.agent_protocol import AgentContext
from ubismart.config import Settings
from ubismart.identity.access import ACCESS_WRITE_AUTO
from ubismart.servers.agent_context import AgentRuntimeFactory


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


def _factory(tmp_path, *, profile: str = "local"):
    captured = {}

    def build(settings, **kwargs):
        captured.update(kwargs)
        return FakeRuntime()

    settings = Settings(
        _env_file=None,
        AGENT_AGENT_DATA_ROOT=str(tmp_path),
        AGENT_SECURITY_PROFILE=profile,
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


def test_factory_injects_host_file_access_and_write_tools(tmp_path) -> None:
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
    assert captured["allow_write"] is True
    assert captured["access_mode"] == ACCESS_WRITE_AUTO
    assert captured["file_access"] is not None
    assert captured["host_fs"] is not None
    assert captured["host_snapshots"] is not None
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


def test_untrusted_profile_rejects_host_workspace(tmp_path) -> None:
    factory, _ = _factory(tmp_path, profile="untrusted")
    context = AgentContext(
        workspace="",
        workspace_id="ws-1",
        workspace_kind="host",
        a2l_id="a2l-1",
        root_handle="h-1",
        access_mode="write_auto",
        allow_write=True,
    )
    try:
        factory.new(context)
    except Exception as exc:  # HTTPException
        detail = getattr(exc, "detail", str(exc))
        assert "sandbox" in str(detail)
    else:  # pragma: no cover
        raise AssertionError("untrusted 必须拒绝 host 工作区")
