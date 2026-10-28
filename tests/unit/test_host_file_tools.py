"""host 工作区的文件工具端到端：写/改/移动/删除/恢复都经 A2L。"""

from __future__ import annotations

import asyncio
from typing import Any

from coding_agent.config import Settings
from coding_agent.execution.host_file_access import HostFileAccess
from coding_agent.execution.host_fs import HostFs
from coding_agent.execution.host_snapshots import HostSnapshotStore
from coding_agent.host_broker.errors import BrokerOpError
from coding_agent.host_broker.ops import HostReadOnlyOps
from coding_agent.host_broker.paths import HostPathPolicy
from coding_agent.host_broker.policy import GrantStore, HostPolicy
from coding_agent.host_broker.protocol import make_request
from coding_agent.host_broker.snapshots import (
    HostSnapshotStore as BrokerSnapshots,
)
from coding_agent.host_broker.toolchain import ToolchainRegistry
from coding_agent.tools.artifacts import unpack
from coding_agent.tools.files import build_file_tools


class _Caller:
    def __init__(self, ops: HostReadOnlyOps, grant_id: str) -> None:
        self.ops = ops
        self.grant_id = grant_id
        self.counter = 0

    def call(
        self,
        kind: str,
        *,
        workspace: dict[str, Any],
        payload: dict[str, Any],
        grant_id: str = "",
    ) -> dict[str, Any]:
        self.counter += 1
        frame = make_request(
            kind,
            op_id=f"tool-{self.counter}",
            workspace=workspace,
            payload=payload,
            grant_id=grant_id or self.grant_id,
        )
        try:
            response = asyncio.run(self.ops.handle(frame))
        except BrokerOpError as exc:
            raise RuntimeError(f"{exc.code}: {exc.message}") from exc
        return dict(response.data)


def _tools(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    grants = GrantStore()
    grant = grants.issue(
        scope=[str(root)], verbs=["read", "write"]
    )
    ops = HostReadOnlyOps(
        policy=HostPolicy(deny_roots=[], sensitive_roots=[]),
        path_policy=HostPathPolicy(),
        grants=grants,
        toolchains=ToolchainRegistry(),
        workspace_roots={"ws-1": str(root)},
        snapshots=BrokerSnapshots(tmp_path / "snapshots"),
        allow_write=True,
    )
    caller = _Caller(ops, grant.grant_id)
    access = HostFileAccess(
        caller, workspace_id="ws-1", root_handle="h-1"
    )
    fs = HostFs(caller, workspace_id="ws-1", root_handle="h-1")
    snapshots = HostSnapshotStore(
        caller, workspace_id="ws-1", root_handle="h-1"
    )
    settings = Settings(_env_file=None)
    tools = build_file_tools(
        settings,
        None,
        allow_write=True,
        file_access=access,
        fs=fs,
        snapshots=snapshots,
        root="",
    )
    return root, {tool.name: tool for tool in tools}


def _invoke(tool, **kwargs):
    text, artifact = unpack(tool.invoke(kwargs))
    assert artifact is not None
    return text, artifact


def test_file_write_edit_move_delete_and_restore(tmp_path) -> None:
    root, tools = _tools(tmp_path)

    text, artifact = _invoke(
        tools["file_write"],
        path="src/a.txt",
        content="hello\n",
        reason="创建",
    )
    assert artifact["ok"] is True
    assert (root / "src" / "a.txt").read_text(encoding="utf-8") == "hello\n"

    _text, artifact = _invoke(
        tools["file_edit"],
        path="src/a.txt",
        old_string="hello",
        new_string="world",
        reason="修改",
    )
    assert artifact["ok"] is True
    assert (root / "src" / "a.txt").read_text(encoding="utf-8") == "world\n"

    _text, artifact = _invoke(
        tools["file_move"],
        source="src/a.txt",
        destination="src/b.txt",
        reason="移动",
    )
    assert artifact["ok"] is True
    assert not (root / "src" / "a.txt").exists()
    assert (root / "src" / "b.txt").read_text(encoding="utf-8") == "world\n"

    _text, artifact = _invoke(
        tools["file_mkdir"], path="empty/dir", reason="建目录"
    )
    assert artifact["ok"] is True
    assert (root / "empty" / "dir").is_dir()

    _text, artifact = _invoke(
        tools["file_delete"], path="src/b.txt", reason="删除"
    )
    assert artifact["ok"] is True
    assert not (root / "src" / "b.txt").exists()

    _text, artifact = _invoke(
        tools["file_restore"], path="src/b.txt", reason="恢复"
    )
    assert artifact["ok"] is True
    assert (root / "src" / "b.txt").read_text(encoding="utf-8") == "world\n"
