"""把现有 WSL/Linux 沙箱包装成 ExecutionBackend。"""

from __future__ import annotations

import shlex
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ubismart.sandbox.fs import SandboxFs
from ubismart.sandbox.snapshots import SnapshotStore


class SandboxExecutionBackend:
    def __init__(
        self,
        sandbox: Any,
        workspace: str,
        *,
        manifest_root: str | Path | None = None,
    ) -> None:
        self.sandbox = sandbox
        self.workspace = str(workspace)
        self.fs = SandboxFs(sandbox, self.workspace)
        self.snapshots = SnapshotStore(
            sandbox,
            self.fs,
            self.workspace,
            manifest_root=manifest_root,
        )

    def capabilities(self) -> dict[str, Any]:
        return {
            "backend": type(self.sandbox).__name__,
            "workspace": self.workspace,
            "shell": "bash",
            "operations": [
                "run",
                "read_file",
                "write_file",
                "list_dir",
                "stat",
                "git",
                "snapshot",
                "restore",
                "worktree_create",
                "worktree_remove",
                "patch_apply",
            ],
            "git": True,
            "snapshot": True,
            "worktree": True,
        }

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        capture_limit: int | None = None,
    ) -> Any:
        return self.sandbox.run(
            command,
            cwd=cwd,
            timeout=timeout,
            capture_limit=capture_limit,
        )

    def run_raw(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        capture_limit: int | None = None,
    ) -> Any:
        return self.sandbox.run(
            command,
            cwd=cwd,
            timeout=timeout,
            capture_limit=capture_limit,
        )

    def read_text(self, path: str, *, max_bytes: int) -> str:
        return self.fs.read_text(path, max_bytes=max_bytes)

    def write_bytes(self, path: str, data: bytes) -> int:
        return self.fs.write_bytes(path, data)

    def list_dir(
        self, path: str, *, max_depth: int = 1, limit: int = 200
    ) -> list[tuple[str, str]]:
        return self.fs.list_dir(path, max_depth=max_depth, limit=limit)

    def stat(self, path: str) -> Any:
        return asdict(self.fs.stat(self.fs.lexical(path)))

    def git(self, args: list[str], *, cwd: str | None = None) -> Any:
        workdir = cwd or self.workspace
        command = (
            f"git -C {shlex.quote(workdir)} "
            + " ".join(shlex.quote(str(arg)) for arg in args)
        )
        return self.sandbox.run(command, cwd=workdir)

    def apply_patch(
        self, patch_path: str, *, root: str, check: bool = False
    ) -> Any:
        args = ["apply"]
        if check:
            args.append("--check")
        args.append(patch_path)
        return self.git(args, cwd=root)

    def snapshot(
        self, path: str, content: str, *, existed: bool = True
    ) -> str:
        return self.snapshots.save(
            path, content, existed=existed
        )

    def restore(self, snapshot_id: str, path: str | None = None) -> Any:
        entry = self.snapshots.find(snapshot_id, path)
        if entry is None:
            raise ValueError(f"快照不存在：{snapshot_id}")
        return asdict(self.snapshots.restore(entry))

    def worktree(
        self,
        action: str,
        *,
        path: str,
        branch: str = "",
        base: str = "HEAD",
    ) -> Any:
        if action == "create":
            args = ["worktree", "add"]
            if branch:
                args += ["-b", branch]
            args += [path, base]
        elif action == "remove":
            args = ["worktree", "remove", "--force", path]
        elif action == "list":
            args = ["worktree", "list", "--porcelain"]
        else:
            raise ValueError(f"未知 worktree action={action!r}")
        return self.git(args)

    def close(self) -> None:
        close = getattr(self.sandbox, "close", None)
        if callable(close):
            close()
