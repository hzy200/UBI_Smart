"""P2：受控 exec 只在 AppContainer 内运行，且只允许已注册命令。"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import pytest

from ubismart.host_broker.availability import appcontainer_required
from ubismart.host_broker.errors import BrokerOpError
from ubismart.host_broker.ops import HostReadOnlyOps
from ubismart.host_broker.paths import HostPathPolicy
from ubismart.host_broker.policy import GrantStore, HostPolicy
from ubismart.host_broker.protocol import (
    EXEC_UNAVAILABLE,
    TOOLCHAIN_UNAVAILABLE,
    make_request,
)
from ubismart.host_broker.snapshots import HostSnapshotStore
from ubismart.host_broker.toolchain import ToolchainRef, ToolchainRegistry
from ubismart.host_broker.win_isolation import AppContainerRunner

_APP_OK, _APP_REASON = appcontainer_required()
pytestmark = [
    pytest.mark.host,
    pytest.mark.skipif(os.name != "nt", reason="仅 Windows 真机"),
    pytest.mark.skipif(not _APP_OK, reason=_APP_REASON),
]


def _setup(tmp_path: Path):
    ws = tmp_path / "project"
    ws.mkdir()
    build = ws / "build"
    build.mkdir()
    toolchain_root = tmp_path / "toolchain"
    toolchain_root.mkdir()
    shutil.copy2(
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32"
        / "cmd.exe",
        toolchain_root / "cmd.exe",
    )
    ref = ToolchainRef(
        id="cmd-test",
        kind="shell",
        root=str(toolchain_root),
        commands=("cmd",),
        output_roots=("build",),
    )
    registry = ToolchainRegistry([ref])
    runner = AppContainerRunner(
        [str(ws)],
        exec_roots=[str(toolchain_root)],
        write_roots=[str(build)],
    )
    grants = GrantStore()
    ops = HostReadOnlyOps(
        policy=HostPolicy(deny_roots=[], sensitive_roots=[]),
        path_policy=HostPathPolicy(),
        grants=grants,
        toolchains=registry,
        workspace_roots={"ws-1": str(ws)},
        snapshots=HostSnapshotStore(tmp_path / "snapshots"),
        allow_write=True,
        allow_exec=True,
        security_tier="appcontainer",
        appcontainer=runner,
    )
    grant = grants.issue(scope=[str(ws)], verbs=["exec"])
    return ops, grant, ws, build, runner


def _frame(grant_id: str, payload: dict, op_id: str = "op-exec"):
    return make_request(
        "host.exec",
        op_id=op_id,
        workspace={"workspace_id": "ws-1", "kind": "host"},
        payload=payload,
        grant_id=grant_id,
    )


def test_appcontainer_exec_echo_and_output_root(tmp_path: Path) -> None:
    ops, grant, ws, build, runner = _setup(tmp_path)
    try:
        result = asyncio.run(
            ops.handle(
                _frame(
                    grant.grant_id,
                    {
                        "toolchain": "cmd-test",
                        "argv": ["cmd", "/c", "echo hello"],
                    },
                )
            )
        )
        assert result.data["exit_code"] == 0
        assert "hello" in result.data["stdout"]

        made = asyncio.run(
            ops.handle(
                _frame(
                    grant.grant_id,
                    {
                        "toolchain": "cmd-test",
                        "argv": ["cmd", "/c", "echo x > ok.txt"],
                        "cwd": "build",
                    },
                    op_id="op-write-build",
                )
            )
        )
        assert made.data["exit_code"] == 0
        assert (build / "ok.txt").exists()
    finally:
        runner.close()


def test_workspace_root_is_read_only_for_exec(tmp_path: Path) -> None:
    ops, grant, ws, _build, runner = _setup(tmp_path)
    try:
        result = asyncio.run(
            ops.handle(
                _frame(
                    grant.grant_id,
                    {
                        "toolchain": "cmd-test",
                        "argv": ["cmd", "/c", "echo x > blocked.txt"],
                    },
                )
            )
        )
        assert result.data["exit_code"] != 0
        assert not (ws / "blocked.txt").exists()
    finally:
        runner.close()


def test_timeout_kills_child(tmp_path: Path) -> None:
    ops, grant, _ws, _build, runner = _setup(tmp_path)
    try:
        result = asyncio.run(
            ops.handle(
                _frame(
                    grant.grant_id,
                    {
                        "toolchain": "cmd-test",
                        "argv": [
                            "cmd",
                            "/c",
                            "for /l %i in (1,1,50000000) do @rem",
                        ],
                        "timeout_s": 1,
                    },
                )
            )
        )
        assert result.data["timed_out"] is True
    finally:
        runner.close()


def test_unregistered_command_and_missing_runner_denied(tmp_path: Path) -> None:
    ops, grant, ws, _build, runner = _setup(tmp_path)
    try:
        with pytest.raises(BrokerOpError) as exc:
            asyncio.run(
                ops.handle(
                    _frame(
                        grant.grant_id,
                        {
                            "toolchain": "cmd-test",
                            "argv": ["powershell", "-Command", "echo hi"],
                        },
                    )
                )
            )
        assert exc.value.code == TOOLCHAIN_UNAVAILABLE
    finally:
        runner.close()

    grants = GrantStore()
    no_runner = HostReadOnlyOps(
        policy=HostPolicy(deny_roots=[], sensitive_roots=[]),
        path_policy=HostPathPolicy(),
        grants=grants,
        toolchains=ToolchainRegistry(),
        workspace_roots={"ws-1": str(ws)},
        allow_write=True,
        allow_exec=True,
        security_tier="lowpriv",
    )
    grant2 = grants.issue(scope=[str(ws)], verbs=["exec"])
    with pytest.raises(BrokerOpError) as exc:
        asyncio.run(
            no_runner.handle(
                _frame(
                    grant2.grant_id,
                    {"toolchain": "cmd-test", "argv": ["cmd", "/c", "echo"]},
                )
            )
        )
    assert exc.value.code == EXEC_UNAVAILABLE
