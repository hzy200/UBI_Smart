"""ExecutionRequest 的 host/sandbox 路由字段（Phase 1 只冻结契约）。"""

from __future__ import annotations

from coding_agent.execution.contracts import (
    ExecutionRequest,
    normalize_execution_target,
)


def test_new_fields_default_to_legacy_sandbox() -> None:
    request = ExecutionRequest(op_id="op", kind="read_file")
    assert request.workspace_kind == "sandbox"
    assert request.target == "auto"
    assert request.grant_id == ""


def test_new_fields_are_explicit() -> None:
    request = ExecutionRequest(
        op_id="op",
        kind="host.read_text",
        workspace_id="ws_1",
        workspace_kind="host",
        target="host",
        grant_id="g_1",
    )
    assert request.workspace_kind == "host"
    assert request.target == "host"
    assert request.grant_id == "g_1"


def test_target_normalization_is_conservative() -> None:
    assert normalize_execution_target("HOST") == "host"
    assert normalize_execution_target("sandbox") == "sandbox"
    assert normalize_execution_target("") == "auto"
    assert normalize_execution_target("weird") == "auto"
