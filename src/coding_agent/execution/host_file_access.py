"""把 A2L typed ops 适配成工具层使用的 FileAccess。

模型侧路径统一为工作区相对 POSIX 路径；绝对路径不离开 A2L。
"""

from __future__ import annotations

import os
from typing import Any, Protocol


class HostCallerProtocol(Protocol):
    def call(
        self,
        kind: str,
        *,
        workspace: dict[str, Any],
        payload: dict[str, Any],
        grant_id: str = "",
    ) -> dict[str, Any]: ...


def _relative(path: str) -> str:
    text = str(path or ".").strip().replace("\\", "/")
    if text.startswith("/") or os.path.isabs(text) or (
        len(text) > 1 and text[1] == ":"
    ):
        raise ValueError("host 工作区只接受相对路径")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise ValueError("host 工作区不允许 .. 路径")
    return "/".join(parts) or "."


class HostFileAccess:
    root = ""

    def __init__(
        self,
        caller: HostCallerProtocol,
        *,
        workspace_id: str,
        root_handle: str,
        grant_id: str = "",
    ) -> None:
        self.caller = caller
        self.workspace = {
            "workspace_id": str(workspace_id),
            "kind": "host",
            "root_handle": str(root_handle),
        }
        self.grant_id = str(grant_id)

    def _call(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        return dict(
            self.caller.call(
                kind,
                workspace=self.workspace,
                payload=payload,
                grant_id=self.grant_id,
            )
            or {}
        )

    def lexical(self, path: str) -> str:
        return _relative(path)

    def relative_to_root(self, path: str) -> str:
        return _relative(path)

    def stat(self, path: str) -> dict[str, Any]:
        return self._call("host.stat", {"path": _relative(path)})

    def read_text(self, path: str, *, max_bytes: int) -> str:
        data = self._call(
            "host.read_text",
            {"path": _relative(path), "max_bytes": int(max_bytes)},
        )
        return str(data.get("text") or "")

    def list_dir(
        self, path: str, *, max_depth: int = 1, limit: int = 200
    ) -> list[tuple[str, str]]:
        if int(max_depth) != 1:
            raise ValueError("Phase 1 host.list_dir 只支持单层")
        data = self._call(
            "host.list_dir",
            {"path": _relative(path), "limit": int(limit)},
        )
        entries = data.get("entries")
        if not isinstance(entries, list):
            raise ValueError("host.list_dir 返回格式错误")
        return [
            (str(item.get("kind") or ""), str(item.get("path") or ""))
            for item in entries
            if isinstance(item, dict)
        ]

    def find_files(
        self,
        pattern: str,
        *,
        path: str | None = None,
        limit: int = 200,
    ) -> list[str]:
        data = self._call(
            "host.find_files",
            {
                "pattern": str(pattern or ""),
                "path": _relative(path or "."),
                "limit": int(limit),
            },
        )
        files = data.get("files")
        if not isinstance(files, list):
            raise ValueError("host.find_files 返回格式错误")
        return [str(item) for item in files]
