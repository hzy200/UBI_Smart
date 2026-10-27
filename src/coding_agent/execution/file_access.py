"""文件访问抽象：让工具层不直接依赖 WSL 或宿主路径。

Phase 1 只接 `file_read` / `list_dir` / `find_files` 三个只读工具。
`SandboxFileAccess` 是现有 `SandboxFs` 的适配器；`HostFileAccess` 在
A2L 后端落地时实现同一协议，工具层不感知底层是 WSL 还是宿主。
"""

from __future__ import annotations

import fnmatch
import os
import posixpath
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class FileAccess(Protocol):
    """工具层需要的最小只读文件接口。

    路径语义由实现决定：模型侧统一使用工作区相对路径，绝对路径只在
    `SandboxFileAccess` / `HostFileAccess` 内部解析。
    """

    root: str

    def lexical(self, path: str) -> str: ...

    def stat(self, path: str) -> Any: ...

    def read_text(self, path: str, *, max_bytes: int) -> str: ...

    def list_dir(
        self, path: str, *, max_depth: int = 1, limit: int = 200
    ) -> list[tuple[str, str]]: ...

    def find_files(
        self,
        pattern: str,
        *,
        path: str | None = None,
        limit: int = 200,
    ) -> list[str]: ...

    def relative_to_root(self, path: str) -> str: ...


class SandboxFileAccess:
    """把现有 `SandboxFs` 适配成 `FileAccess`。

    `find_files` 用纯 Python 遍历，不启动 shell。Phase 1 不把它接到热路径；
    大仓库的性能优化留到后续（可换 ripgrep 或 A2L 原生遍历）。
    """

    def __init__(self, fs: Any, root: str) -> None:
        self._fs = fs
        self.root = str(root)

    def lexical(self, path: str) -> str:
        return self._fs.lexical(path)

    def stat(self, path: str) -> Any:
        return self._fs.stat(path)

    def read_text(self, path: str, *, max_bytes: int) -> str:
        return self._fs.read_text(path, max_bytes=max_bytes)

    def list_dir(
        self, path: str, *, max_depth: int = 1, limit: int = 200
    ) -> list[tuple[str, str]]:
        return self._fs.list_dir(path, max_depth=max_depth, limit=limit)

    def relative_to_root(self, path: str) -> str:
        root = os.path.normpath(self.root)
        target = os.path.normpath(str(path))
        if os.path.normcase(target) == os.path.normcase(root):
            return "."
        prefix = root + os.sep
        if not os.path.normcase(target).startswith(
            os.path.normcase(prefix)
        ):
            raise ValueError(f"路径不在工作区内：{path}")
        return os.path.relpath(target, root).replace(os.sep, "/")

    def find_files(
        self,
        pattern: str,
        *,
        path: str | None = None,
        limit: int = 200,
    ) -> list[str]:
        pattern = str(pattern or "").strip()
        if not pattern:
            return []
        cap = max(1, int(limit))
        start = self._fs.lexical(path or ".")
        found: list[str] = []
        # (目录的绝对/沙箱路径, 相对前缀, 深度)。不跟随符号链接，避免环。
        stack: list[tuple[str, str, int]] = [(start, "", 0)]
        while stack and len(found) < cap:
            current, prefix, depth = stack.pop()
            if depth >= 12:
                continue
            try:
                entries = self._fs.list_dir(
                    current, max_depth=1, limit=max(cap, 200)
                )
            except Exception:  # noqa: BLE001 - 不可读目录跳过，不中断查找
                continue
            for kind, rel in entries:
                rel_path = f"{prefix}/{rel}" if prefix else rel
                if kind == "d":
                    stack.append(
                        (posixpath.join(current, rel), rel_path, depth + 1)
                    )
                elif kind == "f":
                    name = rel.rsplit("/", 1)[-1]
                    if fnmatch.fnmatch(rel_path, pattern) or fnmatch.fnmatch(
                        name, pattern
                    ):
                        found.append(rel_path)
                        if len(found) >= cap:
                            break
        found.sort()
        return found[:cap]
