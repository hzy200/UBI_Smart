"""宿主文件系统：实现 `tools/files.py` 使用的 SandboxFs 子集。

写/删除/建目录全部经 A2L typed op；词法与 `..` 检查在 A3 做，
realpath/grant/ZX 校验由 A2L 完成。
"""

from __future__ import annotations

import base64
from collections.abc import MutableMapping
from typing import Any

from ubismart.execution.host_file_access import (
    CHUNK_BYTES,
    HostCallerProtocol,
    HostFileAccess,
    HostFileError,
    HostFileTooLarge,
    _relative,
)
from ubismart.sandbox.fs import FileStat
from ubismart.sandbox.pathguard import SandboxPathError


class HostFs:
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
        self.access = HostFileAccess(
            caller,
            workspace_id=workspace_id,
            root_handle=root_handle,
            grant_id=grant_id,
            cache=cache,
        )

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

    # ------------------------------------------------------------------
    # 路径
    # ------------------------------------------------------------------
    def lexical(self, path: str) -> str:
        return _relative(path)

    def resolve(self, path: str) -> str:
        relative = _relative(path)
        info = self.access.stat(relative)
        if not info.get("exists"):
            raise HostFileError(f"文件不存在：{relative}")
        return relative

    def relative_to_root(self, path: str) -> str:
        return _relative(path)

    def stat(self, path: str) -> FileStat:
        relative = _relative(path)
        try:
            data = self.access.stat(relative)
        except Exception as exc:  # noqa: BLE001 - 统一成 SandboxFsError
            raise HostFileError(str(exc)) from exc
        return FileStat(
            exists=bool(data.get("exists")),
            is_file=bool(data.get("is_file")),
            size=int(data.get("size") or 0),
            real_path=str(data.get("real_path") or relative),
        )

    # ------------------------------------------------------------------
    # 缓存
    # ------------------------------------------------------------------
    def cached_bytes(self, path: str) -> bytes | None:
        if self.cache is None:
            return None
        try:
            return self.cache.get(_relative(path))
        except SandboxPathError:
            return None

    def invalidate(self, path: str) -> None:
        if self.cache is None:
            return
        try:
            self.cache.pop(_relative(path), None)
        except SandboxPathError:
            return

    # ------------------------------------------------------------------
    # 读写
    # ------------------------------------------------------------------
    def read_for_update(
        self, path: str, *, max_bytes: int
    ) -> tuple[str, FileStat, str | None]:
        relative = _relative(path)
        info = self.stat(relative)
        if not info.exists:
            return relative, info, None
        if not info.is_file:
            raise HostFileError(f"不是普通文件：{relative}")
        if info.size > max_bytes:
            raise HostFileTooLarge(
                f"文件过大：{info.size} 字节（本次上限 {max_bytes} 字节）"
            )
        raw = self.access.read_bytes(
            relative, max_bytes=max_bytes, use_cache=False
        )
        if b"\x00" in raw:
            raise HostFileError(f"二进制文件，不支持读取：{relative}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HostFileError(
                f"非 UTF-8 文本，不支持读取：{relative}（{exc.reason}）"
            ) from exc
        return relative, info, text

    def write_bytes(self, path: str, data: bytes) -> int:
        relative = _relative(path)
        upload = self._call(
            "host.write_begin",
            {"path": relative, "expected_sha256": None},
        )
        upload_id = str(upload.get("upload_id") or "")
        if not upload_id:
            raise HostFileError("write_begin 未返回 upload_id")
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
        self.invalidate(relative)
        return int(result.get("bytes") or len(data))

    def write_text(self, path: str, content: str) -> int:
        return self.write_bytes(path, content.encode("utf-8"))

    def remove(self, path: str, *, known_file: bool = False) -> bool:
        relative = _relative(path)
        result = self._call("host.delete", {"path": relative})
        self.invalidate(relative)
        return bool(result.get("deleted"))

    def mkdir(self, path: str) -> str:
        relative = _relative(path)
        result = self._call("host.mkdir", {"path": relative})
        return str(result.get("path") or relative)
