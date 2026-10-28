"""A4 的 host 后端：把 ExecutionService 的请求映射成 A2L typed op。

Phase 1 只开放只读；写/执行/快照/Git 一律拒绝。真正的异步 A2L 客户端
由同步 caller 适配后注入，避免把 asyncio 事件循环带进工具层。
"""

from __future__ import annotations

import base64
import os
from typing import Any, Protocol

from coding_agent.execution.contracts import ExecutionError
from coding_agent.execution.host_file_access import (
    CHUNK_BYTES,
    HostFileAccess,
)


class HostCallError(RuntimeError):
    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message


class HostCaller(Protocol):
    def call(
        self,
        kind: str,
        *,
        workspace: dict[str, Any],
        payload: dict[str, Any],
        grant_id: str = "",
    ) -> dict[str, Any]: ...


def _relative_path(path: str) -> str:
    text = str(path or ".").strip().replace("\\", "/")
    if text.startswith("/") or os.path.isabs(text) or (
        len(text) > 1 and text[1] == ":"
    ):
        raise ExecutionError(
            "host 工作区只接受相对路径；绝对路径只在 A2L 内解析"
        )
    if ".." in [part for part in text.split("/") if part]:
        raise ExecutionError("host 工作区不允许 .. 路径")
    return text or "."


class HostExecutionBackend:
    def __init__(
        self,
        caller: HostCaller,
        *,
        workspace_id: str,
        root_handle: str,
        grant_id: str = "",
    ) -> None:
        self.caller = caller
        self.workspace_id = str(workspace_id)
        self.root_handle = str(root_handle)
        self.grant_id = str(grant_id)
        self.workspace = {
            "workspace_id": self.workspace_id,
            "kind": "host",
            "root_handle": self.root_handle,
        }
        self.access = HostFileAccess(
            caller,
            workspace_id=self.workspace_id,
            root_handle=self.root_handle,
            grant_id=self.grant_id,
        )

    def capabilities(self) -> dict[str, Any]:
        return {
            "backend": "host",
            "target": "host",
            "workspace": self.workspace_id,
            "shell": False,
            "operations": [
                "read_file",
                "write_file",
                "list_dir",
                "stat",
                "snapshot",
                "restore",
                "exec",
            ],
            "phase": 2,
            "read_only": False,
            "exec": "appcontainer",
        }

    def _call(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            data = self.caller.call(
                kind,
                workspace=self.workspace,
                payload=payload,
                grant_id=self.grant_id,
            )
        except HostCallError as exc:
            raise ExecutionError(f"{exc.code}: {exc.message}") from exc
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "")
            if code:
                raise ExecutionError(f"{code}: {exc}") from exc
            raise
        return dict(data or {})

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        capture_limit: int | None = None,
    ) -> Any:
        raise ExecutionError(
            "host 不接受任意 shell；请使用已注册工具链的 host.exec"
        )

    def exec_argv(
        self,
        *,
        toolchain: str,
        argv: list[str],
        cwd: str = ".",
        timeout_s: float = 60.0,
        capture_limit: int = 200_000,
    ) -> dict[str, Any]:
        return self._call(
            "host.exec",
            {
                "toolchain": str(toolchain),
                "argv": [str(item) for item in argv],
                "cwd": _relative_path(cwd),
                "timeout_s": float(timeout_s),
                "capture_limit": int(capture_limit),
            },
        )

    def install_request(
        self,
        *,
        manager: str,
        command: str,
        url: str = "",
        requires_admin: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        return self._call(
            "host.install",
            {
                "manager": str(manager),
                "command": str(command),
                "url": str(url),
                "requires_admin": bool(requires_admin),
                "reason": str(reason),
            },
        )

    def read_text(self, path: str, *, max_bytes: int) -> str:
        return self.access.read_text(
            _relative_path(path), max_bytes=int(max_bytes)
        )

    def list_dir(
        self, path: str, *, max_depth: int = 1, limit: int = 200
    ) -> list[tuple[str, str]]:
        if int(max_depth) != 1:
            raise ExecutionError("Phase 1 host.list_dir 只支持单层")
        data = self._call(
            "host.list_dir",
            {"path": _relative_path(path), "limit": int(limit)},
        )
        entries = data.get("entries")
        if not isinstance(entries, list):
            raise ExecutionError("host.list_dir 返回格式错误")
        return [
            (str(item.get("kind") or ""), str(item.get("path") or ""))
            for item in entries
            if isinstance(item, dict)
        ]

    def stat(self, path: str) -> dict[str, Any]:
        return self._call("host.stat", {"path": _relative_path(path)})

    def write_bytes(self, path: str, data: bytes) -> int:
        relative = _relative_path(path)
        upload = self._call("host.write_begin", {"path": relative})
        upload_id = str(upload.get("upload_id") or "")
        if not upload_id:
            raise ExecutionError("host.write_begin 未返回 upload_id")
        try:
            for offset in range(0, len(data), CHUNK_BYTES):
                chunk = data[offset : offset + CHUNK_BYTES]
                self._call(
                    "host.write_chunk",
                    {
                        "upload_id": upload_id,
                        "offset": offset,
                        "data_b64": base64.b64encode(chunk).decode("ascii"),
                    },
                )
            result = self._call(
                "host.write_commit", {"upload_id": upload_id}
            )
        except Exception:
            try:
                self._call(
                    "host.write_abort", {"upload_id": upload_id}
                )
            except Exception:  # noqa: BLE001
                pass
            raise
        return int(result.get("bytes") or len(data))

    def git(self, args: list[str], *, cwd: str | None = None) -> Any:
        raise ExecutionError("OP_NOT_ENABLED_IN_PHASE1: host.git")

    def apply_patch(
        self, patch_path: str, *, root: str, check: bool = False
    ) -> Any:
        raise ExecutionError("OP_NOT_ENABLED_IN_PHASE1: host.patch_apply")

    def snapshot(
        self, path: str, content: str, *, existed: bool = True
    ) -> str:
        data = self._call(
            "host.snapshot_save", {"path": _relative_path(path)}
        )
        return str(data.get("snapshot_id") or "")

    def restore(self, snapshot_id: str, path: str | None = None) -> Any:
        payload: dict[str, Any] = {"snapshot_id": str(snapshot_id)}
        if path:
            payload["path"] = _relative_path(path)
        return self._call("host.restore", payload)

    def worktree(
        self,
        action: str,
        *,
        path: str,
        branch: str = "",
        base: str = "HEAD",
    ) -> Any:
        raise ExecutionError("OP_NOT_ENABLED_IN_PHASE1: host.worktree")

    def close(self) -> None:
        close = getattr(self.caller, "close", None)
        if callable(close):
            close()
