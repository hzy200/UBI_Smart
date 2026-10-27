"""WorkspaceRef：跨层只传句柄，不传宿主绝对路径。"""

from __future__ import annotations

import pytest

from coding_agent.workspace.model import (
    WorkspaceRef,
    normalize_workspace_kind,
)


def test_host_ref_requires_opaque_handle() -> None:
    ref = WorkspaceRef(
        workspace_id="ws_1",
        kind="host",
        root_handle="h_abc-123",
        display_name="demo",
    )
    assert ref.kind == "host"
    assert ref.root_handle == "h_abc-123"


def test_host_ref_rejects_missing_handle() -> None:
    with pytest.raises(ValueError):
        WorkspaceRef(workspace_id="ws_1", kind="host")


def test_handle_rejects_path_characters() -> None:
    for handle in ("C:\\work", "/home/a", "a/b", "a:b"):
        with pytest.raises(ValueError):
            WorkspaceRef(workspace_id="ws_1", kind="host", root_handle=handle)


def test_unknown_kind_falls_back_to_sandbox() -> None:
    ref = WorkspaceRef(workspace_id="ws_1", kind="weird")  # type: ignore[arg-type]
    assert ref.kind == "sandbox"
    assert normalize_workspace_kind("HOST") == "host"
    assert normalize_workspace_kind("") == "sandbox"


def test_roundtrip_keeps_handle_not_path() -> None:
    ref = WorkspaceRef("ws_1", "host", "h_1", "Demo")
    payload = ref.to_dict()
    assert payload["root_handle"] == "h_1"
    assert "root_path" not in payload
    assert WorkspaceRef.from_dict(payload) == ref
