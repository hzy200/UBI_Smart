"""A3 运行状态与 SSE 原语的纯函数契约。"""

from __future__ import annotations

from coding_agent.events import (
    ApprovalRequested,
    AssistantToken,
    RunFailed,
    RunFinished,
    SteeringApplied,
)
from coding_agent.servers.agent_server_support import (
    RunState,
    _ContextSecretStore,
    _is_suspended,
    _safe_tenant,
    _sse,
    _sse_with_id,
    _terminal_payload,
)


def test_sse_frames_include_optional_id() -> None:
    event = AssistantToken(node="respond", text="hi")
    assert _sse(event).startswith("data: ")
    framed = _sse_with_id(event, 7)
    assert framed.startswith("id: 7\ndata: ")
    assert framed.endswith("\n\n")


def test_suspended_events() -> None:
    assert _is_suspended(ApprovalRequested(request_id="r", tool="t"))
    assert _is_suspended(SteeringApplied(action="pause"))
    assert not _is_suspended(SteeringApplied(action="resume"))
    assert not _is_suspended(AssistantToken(node="act", text="x"))


def test_terminal_payload_shapes() -> None:
    for payload in (
        RunFinished(thread_id="t", answer="ok"),
        RunFailed(message="boom"),
        ApprovalRequested(request_id="r", tool="t"),
    ):
        assert _terminal_payload(payload.model_dump_json()) is True

    assert (
        _terminal_payload(SteeringApplied(action="pause").model_dump_json())
        is True
    )
    assert (
        _terminal_payload(SteeringApplied(action="resume").model_dump_json())
        is False
    )
    assert (
        _terminal_payload(AssistantToken(node="act", text="x").model_dump_json())
        is False
    )
    assert _terminal_payload("not-json") is False
    assert _terminal_payload("{}") is False


def test_safe_tenant_filters_and_falls_back() -> None:
    assert _safe_tenant("") == "local"
    assert _safe_tenant(None) == "local"  # type: ignore[arg-type]
    assert _safe_tenant("team/a b:c") == "teamabc"
    assert _safe_tenant("a" * 100) == "a" * 64


def test_context_secret_store() -> None:
    store = _ContextSecretStore({"k": "v"})
    assert store.get("k") == "v"
    assert store.has("k") is True
    store.set("x", 123)  # type: ignore[arg-type]
    assert store.get("x") == "123"
    store.delete("k")
    assert store.get("k") == ""
    assert store.has("k") is False
    assert store.has("missing") is False


def test_run_state_defaults() -> None:
    state = RunState(key=("local", "t1"), thread_id="t1", runtime=object())
    assert state.seq == 0
    assert state.chars == 0
    assert state.done is False
    assert state.events == state.events.__class__()
    assert state.subscribers == []
