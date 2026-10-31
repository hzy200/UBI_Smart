"""把 A2L typed ops 适配成工具层使用的 FileAccess。

模型侧路径统一为工作区相对 POSIX 路径；绝对路径不离开 A2L。
读走分块协议，避免 2–10 MB 文件撑爆 1 MiB 的帧上限。
"""

from __future__ import annotations

import base64
import os
from collections.abc import MutableMapping
from typing import Any, Protocol

from coding_agent.sandbox.fs import SandboxFsError, SandboxFsTooLarge
from coding_agent.sandbox.pathguard import SandboxPathError

# 单帧上限 1 MiB；512 KiB 原始字节 base64 后约 683 KiB，仍留有余量。
# 与服务端 ops.MAX_CHUNK_BYTES 对齐：1 MB 文件从 5 次 RPC 降到 3 次。
CHUNK_BYTES = 512 * 1024
DEFAULT_READ_BYTES = 1024 * 1024


class HostCallerProtocol(Protocol):
    def call(
        self,
        kind: str,
        *,
        workspace: dict[str, Any],
        payload: dict[str, Any],
        grant_id: str = "",
    ) -> dict[str, Any]: ...


class HostFileError(SandboxFsError):
    """A2L 文件操作失败。"""


class HostFileTooLarge(SandboxFsTooLarge):
    """超过本次读取上限；调用方可提示分段读取。"""


def _relative(path: str) -> str:
    text = str(path or ".").strip().replace("\\", "/")
    if text.startswith("/") or os.path.isabs(text) or (
        len(text) > 1 and text[1] == ":"
    ):
        raise SandboxPathError("host 工作区只接受相对路径")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise SandboxPathError("host 工作区不允许 .. 路径")
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
        cache: MutableMapping[str, bytes] | None = None,
    ) -> None:
        self.caller = caller
        self.workspace = {
            "workspace_id": str(workspace_id),
            "kind": "host",
            "root_handle": str(root_handle),
        }
        self.grant_id = str(grant_id)
        self.cache = cache

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
        relative = _relative(path)
        data = self._call("host.stat", {"path": relative})
        if data.get("exists"):
            data.setdefault("real_path", relative)
        return data

    def read_bytes(
        self,
        path: str,
        *,
        max_bytes: int = DEFAULT_READ_BYTES,
        use_cache: bool = True,
    ) -> bytes:
        relative = _relative(path)
        cached = (
            self.cache.get(relative)
            if use_cache and self.cache is not None
            else None
        )
        if cached is not None:
            if len(cached) > max_bytes:
                raise HostFileTooLarge(
                    f"文件过大：{len(cached)} 字节（本次上限 {max_bytes} 字节）"
                )
            return cached
        info = self.stat(relative)
        if not info.get("exists"):
            raise HostFileError(f"文件不存在：{relative}")
        if not info.get("is_file"):
            raise HostFileError(f"不是普通文件：{relative}")
        size = int(info.get("size") or 0)
        if size > max_bytes:
            raise HostFileTooLarge(
                f"文件过大：{size} 字节（本次上限 {max_bytes} 字节）"
            )
        chunks: list[bytes] = []
        offset = 0
        while offset < size:
            length = min(CHUNK_BYTES, max_bytes - offset)
            data = self._call(
                "host.read_chunk",
                {"path": relative, "offset": offset, "length": length},
            )
            try:
                chunk = base64.b64decode(
                    str(data.get("data_b64") or ""), validate=True
                )
            except (ValueError, TypeError) as exc:
                raise HostFileError(f"读取结果解码失败：{exc}") from exc
            if not chunk:
                break
            chunks.append(chunk)
            offset = int(data.get("next") or offset + len(chunk))
            if data.get("eof"):
                break
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            raise HostFileTooLarge(
                f"文件过大：{len(raw)} 字节（本次上限 {max_bytes} 字节）"
            )
        if self.cache is not None and use_cache:
            self.cache[relative] = raw
        return raw

    def read_text(self, path: str, *, max_bytes: int) -> str:
        data = self.read_bytes(path, max_bytes=max_bytes)
        if b"\x00" in data:
            raise HostFileError(f"二进制文件，不支持读取：{path}")
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HostFileError(
                f"非 UTF-8 文本，不支持读取：{path}（{exc.reason}）"
            ) from exc

    def list_dir(
        self, path: str, *, max_depth: int = 1, limit: int = 200
    ) -> list[tuple[str, str]]:
        if int(max_depth) != 1:
            raise ValueError("host.list_dir 只支持单层")
        data = self._call(
            "host.list_dir",
            {"path": _relative(path), "limit": int(limit)},
        )
        entries = data.get("entries")
        if not isinstance(entries, list):
            raise HostFileError("host.list_dir 返回格式错误")
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
            raise HostFileError("host.find_files 返回格式错误")
        return [str(item) for item in files]
