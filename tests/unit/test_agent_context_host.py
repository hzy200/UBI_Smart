"""A3 host 工作区：上下文字段与 runtime 自动注入。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fastapi import HTTPException

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


def _library_factory(tmp_path, **settings_kwargs):
    settings = Settings(
        _env_file=None,
        AGENT_AGENT_DATA_ROOT=str(tmp_path),
        **settings_kwargs,
    )
    return AgentRuntimeFactory(settings, lambda _settings, **_kwargs: FakeRuntime())


def _library_context(tenant: str, root: str = "") -> AgentContext:
    return AgentContext(
        tenant=tenant,
        workspace=f"/srv/ws/{tenant}",
        workspace_kind="sandbox",
        library_root=root,
    )


def test_library_root_is_derived_per_tenant(tmp_path) -> None:
    factory = _library_factory(
        tmp_path, AGENT_WEB_LIBRARY_ROOT="/srv/library"
    )
    settings = factory.tenant_settings(
        _library_context("alice"), validate_library_root=True
    )
    assert settings.library_root == "/srv/library/alice"


def test_library_root_rejects_another_tenant(tmp_path) -> None:
    factory = _library_factory(
        tmp_path, AGENT_WEB_LIBRARY_ROOT="/srv/library"
    )
    with pytest.raises(HTTPException) as caught:
        factory.tenant_settings(
            _library_context("alice", "/srv/library/bob"),
            validate_library_root=True,
        )
    assert caught.value.status_code == 403


def test_library_root_rejects_path_outside_base(tmp_path) -> None:
    factory = _library_factory(
        tmp_path, AGENT_WEB_LIBRARY_ROOT="/srv/library"
    )
    with pytest.raises(HTTPException) as caught:
        factory.tenant_settings(
            _library_context("alice", "/tmp/alice"),
            validate_library_root=True,
        )
    assert caught.value.status_code == 403


def test_library_root_fails_closed_without_base_for_tenant(tmp_path) -> None:
    factory = _library_factory(tmp_path)
    with pytest.raises(HTTPException) as caught:
        factory.tenant_settings(
            _library_context("alice"), validate_library_root=True
        )
    assert caught.value.status_code == 400


def test_library_root_accepts_default_tenant_path_without_base(tmp_path) -> None:
    factory = _library_factory(tmp_path)
    settings = factory.tenant_settings(
        _library_context("alice", "/home/u/agent-library/alice"),
        validate_library_root=True,
    )
    assert settings.library_root == "/home/u/agent-library/alice"


def test_audit_dir_does_not_require_a_valid_library_root(tmp_path) -> None:
    factory = _library_factory(tmp_path)
    # 审计路径只需要 tenant 目录；没有资料库配置时不能因此拒绝。
    settings = factory.tenant_settings(_library_context("alice"))
    normalized = settings.audit_dir.replace("\\", "/")
    assert normalized.endswith("alice/audit")


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
