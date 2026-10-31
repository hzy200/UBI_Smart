"""TD-011 P0：Windows Job Object / 低完整性 / AppContainer 只读红队用例。"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ubismart.host_broker.availability import appcontainer_required
from ubismart.host_broker.win_isolation import (
    AppContainerRunner,
    JobLimits,
    JobObject,
)

pytestmark = [
    pytest.mark.host,
    pytest.mark.skipif(os.name != "nt", reason="仅 Windows 真机"),
]


def test_job_object_kills_child_on_close() -> None:
    job = JobObject(JobLimits(max_processes=4, job_memory_mb=512))
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    try:
        job.assign(proc._handle)
        assert proc.poll() is None
        job.close()
        proc.wait(timeout=10)
        assert proc.returncode != 0
    finally:
        job.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_low_integrity_can_be_applied_in_child_process() -> None:
    code = (
        "from ubismart.host_broker.win_isolation import set_low_integrity;"
        "raise SystemExit(0 if set_low_integrity() else 1)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_appcontainer_reads_only_granted_tree(tmp_path: Path) -> None:
    available, reason = appcontainer_required()
    if not available:
        pytest.skip(reason)
    allowed = tmp_path / "allowed"
    denied = tmp_path / "denied"
    allowed.mkdir()
    denied.mkdir()
    (allowed / "a.txt").write_text("hello", encoding="utf-8")
    (denied / "b.txt").write_text("secret", encoding="utf-8")

    runner = AppContainerRunner([str(allowed)])
    try:
        read = runner.call("read", {"path": str(allowed / "a.txt")})
        assert read["ok"] is True, read
        assert read["text"] == "hello"

        listed = runner.call("list", {"path": str(allowed)})
        assert listed["ok"] is True
        assert [item["name"] for item in listed["entries"]] == ["a.txt"]

        stat = runner.call("stat", {"path": str(allowed / "a.txt")})
        assert stat["ok"] is True and stat["size"] == 5

        found = runner.call(
            "find", {"path": str(allowed), "pattern": "*.txt"}
        )
        assert found["ok"] is True and found["files"] == ["a.txt"]

        blocked = runner.call("read", {"path": str(denied / "b.txt")})
        assert blocked["ok"] is False
        assert blocked["code"] == "ACCESS_DENIED"
    finally:
        runner.close()


def test_read_ops_use_appcontainer_runner(tmp_path: Path) -> None:
    available, reason = appcontainer_required()
    if not available:
        pytest.skip(reason)
    from ubismart.host_broker.ops import HostReadOnlyOps
    from ubismart.host_broker.paths import HostPathPolicy
    from ubismart.host_broker.policy import GrantStore, HostPolicy
    from ubismart.host_broker.protocol import (
        make_request,
    )
    from ubismart.host_broker.toolchain import ToolchainRegistry

    root = tmp_path / "project"
    root.mkdir()
    (root / "a.txt").write_text("from-appcontainer", encoding="utf-8")
    grants = GrantStore()
    grant = grants.issue(scope=[str(root)], verbs=["read"])
    runner = AppContainerRunner([str(root)])
    try:
        ops = HostReadOnlyOps(
            policy=HostPolicy(deny_roots=[], sensitive_roots=[]),
            path_policy=HostPathPolicy(),
            grants=grants,
            toolchains=ToolchainRegistry(),
            workspace_roots={"ws-1": str(root)},
            appcontainer=runner,
            isolation={"appcontainer": True},
        )
        frame = make_request(
            "host.read_text",
            op_id="op-appc",
            workspace={"workspace_id": "ws-1", "kind": "host"},
            payload={"path": "a.txt"},
            grant_id=grant.grant_id,
        )
        response = asyncio.run(ops.handle(frame))
        assert response.status == "ok"
        assert response.data["text"] == "from-appcontainer"
        health = asyncio.run(
            ops.handle(
                make_request(
                    "host.health",
                    op_id="op-health",
                    workspace={"workspace_id": "ws-1", "kind": "host"},
                )
            )
        )
        assert health.data["isolation"]["appcontainer"] is True
    finally:
        runner.close()
