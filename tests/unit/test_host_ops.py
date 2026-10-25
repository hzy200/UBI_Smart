"""宿主侧内部执行白名单（ADR-0011）。

这些调用绕过工具层与命令分级，所以必须显式标记 origin/kind，并且命令前缀
要与 kind 匹配。测试同时锁定：orchestrator 包内不再出现直接的 sandbox.run。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

from coding_agent.execution.host_ops import (
    HOST_KINDS,
    HOST_ORIGIN,
    HostExecutionDenied,
    host_run,
)

SRC = Path(__file__).resolve().parents[2] / "src" / "coding_agent"


class _FakeSandbox:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def run(self, command: str, *, cwd: str | None = None) -> Any:
        self.calls.append((command, cwd))
        return "ok"


_SAMPLES = {
    "git_apply": "git apply --check /ws/.agent/patches/t.patch",
    "git_read_tree": "GIT_INDEX_FILE='/tmp/idx' git read-tree HEAD",
    "git_reset": "git reset -q",
    "git_worktree": "git worktree prune",
    "git_patch_prepare": "git diff --cached --binary HEAD -- .",
    "git_probe": "inside=$(git rev-parse --is-inside-work-tree 2>/dev/null)",
    "worktree_prepare": "mkdir -p /ws/.agent/worktrees",
    "worktree_cleanup": "rm -rf /ws/.agent/worktrees/t1",
    "merge_temp_cleanup": "rm -f /tmp/agent-merge-index-deadbeef",
    "snapshot_read": "python3 - <<'__AGENT_SNAPSHOT__'",
    "repo_survey": 'echo "AGENT_DIRS"',
    "workspace_import_prepare": "mkdir -p /home/agent/ws/demo.import-abc",
    "workspace_import": (
        "tar -C '/mnt/d/proj' --exclude=./.agent -cf - . "
        "| tar -C '/home/agent/ws/demo.import-abc' -xf -"
    ),
    "workspace_import_cleanup": "rm -rf /home/agent/ws/demo.import-abc",
    "workspace_git_init": "git init -q",
    "workspace_revision_add": "git add -A",
    "workspace_revision_commit": "git commit -q -m upload --allow-empty",
    "workspace_revision_probe": "git rev-parse HEAD",
    "workspace_rollback": "git reset --hard abc123",
    "workspace_archive_delete": "rm -f /srv/archive/rev.tar.gz",
}


def test_every_whitelisted_kind_has_a_sample() -> None:
    """白名单新增 kind 时必须补一条可执行样例，防止“登记了但没人知道怎么用”。"""
    assert set(_SAMPLES) == set(HOST_KINDS)


@pytest.mark.parametrize("kind", sorted(_SAMPLES))
def test_whitelisted_kind_runs_the_command(kind: str) -> None:
    sandbox = _FakeSandbox()
    command = _SAMPLES[kind]
    assert (
        host_run(
            sandbox,
            command,
            origin=HOST_ORIGIN,
            kind=kind,
            cwd="/ws",
        )
        == "ok"
    )
    assert sandbox.calls == [(command, "/ws")]


def test_unknown_origin_is_rejected() -> None:
    sandbox = _FakeSandbox()
    with pytest.raises(HostExecutionDenied, match="origin"):
        host_run(
            sandbox,
            "git apply /ws/t.patch",
            origin="agent",
            kind="git_apply",
            cwd="/ws",
        )
    assert sandbox.calls == []


def test_unknown_kind_is_rejected() -> None:
    sandbox = _FakeSandbox()
    with pytest.raises(HostExecutionDenied, match="kind"):
        host_run(
            sandbox,
            "git apply /ws/t.patch",
            origin=HOST_ORIGIN,
            kind="anything",
            cwd="/ws",
        )
    assert sandbox.calls == []


def test_kind_must_match_the_command() -> None:
    sandbox = _FakeSandbox()
    with pytest.raises(HostExecutionDenied, match="不允许命令"):
        host_run(
            sandbox,
            "rm -rf /ws",
            origin=HOST_ORIGIN,
            kind="git_apply",
            cwd="/ws",
        )
    assert sandbox.calls == []


def test_env_assignment_prefix_is_ignored_for_matching() -> None:
    """3-way 预演通过 `GIT_INDEX_FILE=...` 前缀设置临时 index，不能因此被拒。"""
    sandbox = _FakeSandbox()
    host_run(
        sandbox,
        "GIT_INDEX_FILE='/tmp/idx with space' git read-tree HEAD",
        origin=HOST_ORIGIN,
        kind="git_read_tree",
        cwd="/ws",
    )
    assert len(sandbox.calls) == 1


def _direct_sandbox_run_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "sandbox"
        ):
            lines.append(node.lineno)
    return lines


def test_orchestrator_has_no_unmarked_sandbox_execution() -> None:
    """orchestrator 里所有宿主执行都必须经 host_run（ADR-0011 白名单）。"""
    offenders: list[str] = []
    for path in sorted((SRC / "orchestrator").rglob("*.py")):
        lines = _direct_sandbox_run_calls(path)
        if lines:
            offenders.append(f"{path.relative_to(SRC)}:{lines}")
    assert not offenders, (
        "发现绕过 host_run 的宿主执行：" + "；".join(offenders)
    )
