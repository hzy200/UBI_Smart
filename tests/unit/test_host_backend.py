"""A4 host 后端与 ExecutionService 的 target 路由。"""

from __future__ import annotations

import pytest

from coding_agent.config import Settings
from coding_agent.execution.contracts import ExecutionError, ExecutionRequest
from coding_agent.execution.host_backend import (
    HostCallError,
    HostExecutionBackend,
)
from coding_agent.execution.service import ExecutionService
from coding_agent.sandbox.wsl_exec import ExecResult
from coding_agent.tools.registry import build_tools


class FakeCaller:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, dict, str]] = []

    def call(self, kind, *, workspace, payload, grant_id=""):
        self.calls.append((kind, workspace, payload, grant_id))
        if kind == "host.read_text":
            return {"text": "hello\n", "path": payload["path"]}
        if kind == "host.list_dir":
            return {"entries": [{"kind": "f", "path": "a.py"}]}
        if kind == "host.stat":
            return {"exists": True, "is_file": True, "size": 6}
        raise HostCallError("OP_UNKNOWN", kind)


class FakeSandboxBackend:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def capabilities(self):
        return {"backend": "fake-sandbox", "workspace": "/ws"}

    def run(self, command, *, cwd=None, timeout=None, capture_limit=None):
        self.calls.append("run")
        raise ExecutionError("sandbox run not expected")

    def read_text(self, path, *, max_bytes):
        self.calls.append("read_text")
        return f"sandbox:{path}"

    def list_dir(self, path, *, max_depth=1, limit=200):
        self.calls.append("list_dir")
        return []

    def stat(self, path):
        self.calls.append("stat")
        return {"exists": True, "path": path}

    def write_bytes(self, path, data):
        self.calls.append("write_bytes")
        return len(data)


def test_host_backend_maps_only_read_ops() -> None:
    caller = FakeCaller()
    backend = HostExecutionBackend(
        caller,
        workspace_id="ws-1",
        root_handle="h-1",
        grant_id="g-1",
    )
    assert backend.read_text("a.py", max_bytes=10) == "hello\n"
    assert backend.list_dir(".", limit=5) == [("f", "a.py")]
    assert backend.stat("a.py")["exists"] is True
    assert caller.calls[0][1]["kind"] == "host"
    assert caller.calls[0][3] == "g-1"

    with pytest.raises(ExecutionError):
        backend.read_text("/abs/a.py", max_bytes=10)
    with pytest.raises(ExecutionError):
        backend.write_bytes("a.py", b"x")
    with pytest.raises(ExecutionError):
        backend.run("echo x", cwd=".")


def test_service_routes_by_workspace_kind(tmp_path) -> None:
    caller = FakeCaller()
    host = HostExecutionBackend(
        caller, workspace_id="ws-1", root_handle="h-1", grant_id="g-1"
    )
    sandbox = FakeSandboxBackend()
    service = ExecutionService(
        sandbox,
        "/ws",
        targets={"host": host},
        workspace_kind="host",
        journal_path=tmp_path / "receipts.jsonl",
    )
    result = service.execute(
        ExecutionRequest(
            op_id="op-host",
            kind="read_file",
            workspace_id="ws-1",
            workspace_kind="host",
            target="auto",
            grant_id="g-1",
            payload={"path": "a.py"},
        )
    )
    assert result.status == "ok"
    assert result.data["target"] == "host"
    assert result.data["content"] == "hello\n"
    assert sandbox.calls == []
    assert caller.calls[0][0] == "host.read_text"


def test_service_rejects_target_kind_mismatch(tmp_path) -> None:
    host = HostExecutionBackend(
        FakeCaller(), workspace_id="ws-1", root_handle="h-1"
    )
    service = ExecutionService(
        FakeSandboxBackend(),
        "/ws",
        targets={"host": host},
        workspace_kind="host",
        journal_path=tmp_path / "receipts.jsonl",
    )
    result = service.execute(
        ExecutionRequest(
            op_id="op-mismatch",
            kind="read_file",
            workspace_kind="host",
            target="sandbox",
            payload={"path": "a.py"},
        )
    )
    assert result.status == "error"
    assert "TARGET_KIND_MISMATCH" in result.error


class FakeSandbox:
    def __init__(self, settings):
        self.settings = settings

    def run(self, command, *, cwd=None, timeout=None, capture_limit=None):
        return ExecResult(
            command=command, exit_code=1, stdout="", stderr="", duration_ms=0
        )


def test_host_mode_exposes_only_readonly_file_tools() -> None:
    settings = Settings(_env_file=None, AGENT_WSL_WORKSPACE="/ws")
    sandbox = FakeSandbox(settings)
    tools = build_tools(
        settings,
        sandbox=sandbox,
        file_access=object(),
        host_mode=True,
    )
    assert {tool.name for tool in tools} == {
        "file_read",
        "file_list",
        "find_files",
    }


def test_service_requires_host_backend(tmp_path) -> None:
    service = ExecutionService(
        FakeSandboxBackend(),
        "/ws",
        workspace_kind="sandbox",
        journal_path=tmp_path / "receipts.jsonl",
    )
    result = service.execute(
        ExecutionRequest(
            op_id="op-no-host",
            kind="read_file",
            workspace_kind="host",
            target="host",
            payload={"path": "a.py"},
        )
    )
    assert result.status == "error"
    assert "host" in result.error
