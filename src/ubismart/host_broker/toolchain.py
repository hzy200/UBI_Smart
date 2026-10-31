"""工具链能力注册：模型只看到摘要，看不到宿主绝对路径。

Phase 1 只登记能力，不执行。真正的 launcher/环境适配在 P3 单独实现。
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_KIND_RE = re.compile(r"^[a-z0-9_-]{1,32}$")
_COMMAND_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _validate_output_root(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    if not text or text.startswith("/"):
        raise ValueError("output_root 必须是非空相对目录")
    if re.match(r"^[A-Za-z]:", text):
        raise ValueError("output_root 不允许盘符")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"非法 output_root：{value!r}")
    return "/".join(parts)


@dataclass(frozen=True, slots=True)
class ToolchainRef:
    id: str
    kind: str
    version: str = ""
    output_roots: tuple[str, ...] = ()
    # root/commands 只在 A2L 内部使用，summary 不暴露绝对路径。
    root: str = ""
    commands: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(str(self.id or "")):
            raise ValueError(f"非法 toolchain id：{self.id!r}")
        if not _KIND_RE.fullmatch(str(self.kind or "")):
            raise ValueError(f"非法 toolchain kind：{self.kind!r}")
        roots = tuple(_validate_output_root(item) for item in self.output_roots)
        if isinstance(self.commands, str):
            commands = (self.commands,)
        else:
            commands = tuple(self.commands)
        normalized: list[str] = []
        for command in commands:
            name = str(command or "").strip()
            if not _COMMAND_RE.fullmatch(name):
                raise ValueError(f"非法 toolchain command：{command!r}")
            if name not in normalized:
                normalized.append(name)
        root = str(self.root or "").strip()
        if root:
            expanded = os.path.expandvars(os.path.expanduser(root))
            if not os.path.isabs(expanded):
                raise ValueError(f"toolchain root 必须是绝对路径：{root!r}")
            if expanded.startswith(("\\\\", "//")):
                raise ValueError("toolchain root 不允许 UNC 路径")
            root = os.path.normpath(expanded)
            if os.path.dirname(root) == root:
                raise ValueError("toolchain root 不允许文件系统根")
            if os.path.normcase(root) == os.path.normcase(
                os.path.normpath(str(Path.home()))
            ):
                raise ValueError("toolchain root 不允许用户 home 根")
        if normalized and not root:
            raise ValueError("注册 toolchain commands 时必须提供 root")
        object.__setattr__(self, "output_roots", roots)
        object.__setattr__(self, "commands", tuple(normalized))
        object.__setattr__(self, "root", root)
        object.__setattr__(self, "id", str(self.id))
        object.__setattr__(self, "kind", str(self.kind))
        object.__setattr__(self, "version", str(self.version or ""))

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "version": self.version,
            "output_roots": list(self.output_roots),
            "commands": list(self.commands),
        }


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.normcase(path), os.path.normcase(root)]
        ) == os.path.normcase(root)
    except ValueError:
        return False


class ToolchainRegistry:
    def __init__(self, refs: Iterable[ToolchainRef] | None = None) -> None:
        self._refs: dict[str, ToolchainRef] = {}
        self._executables: dict[tuple[str, str], str] = {}
        for ref in refs or ():
            self.register(ref)

    def _resolve_executable(self, ref: ToolchainRef, command: str) -> str:
        root = ref.root
        if not root or not os.path.isdir(root):
            raise ValueError(f"toolchain root 不存在或不是目录：{root!r}")
        found = shutil.which(command, path=root)
        if not found:
            candidate = Path(root) / command
            if candidate.is_file():
                found = str(candidate)
        if not found:
            raise ValueError(
                f"toolchain {ref.id} 找不到命令 {command!r}（root={root}）"
            )
        resolved = os.path.realpath(found)
        if not _within(resolved, os.path.realpath(root)):
            raise ValueError(
                f"toolchain {ref.id} 的命令 {command!r} 解析到 root 之外"
            )
        return resolved

    def register(self, ref: ToolchainRef) -> None:
        if ref.id in self._refs:
            raise ValueError(f"重复的 toolchain id：{ref.id}")
        resolved: dict[str, str] = {}
        for command in ref.commands:
            resolved[command] = self._resolve_executable(ref, command)
        self._refs[ref.id] = ref
        for command, executable in resolved.items():
            self._executables[(ref.id, command)] = executable

    def get(self, toolchain_id: str) -> ToolchainRef | None:
        return self._refs.get(str(toolchain_id or ""))

    def resolve(self, toolchain_id: str, command: str) -> str | None:
        """把已注册命令解析成绝对可执行文件；未注册返回 None。"""
        return self._executables.get(
            (str(toolchain_id or ""), str(command or ""))
        )

    def exec_roots(self) -> list[str]:
        return sorted({ref.root for ref in self._refs.values() if ref.root})

    def summary(self) -> dict[str, Any]:
        return {
            "toolchains": [
                ref.summary()
                for ref in sorted(self._refs.values(), key=lambda item: item.id)
            ]
        }

    def clear(self) -> None:
        self._refs.clear()
        self._executables.clear()

    def __len__(self) -> int:
        return len(self._refs)

    def __iter__(self):
        return iter(self._refs.values())
