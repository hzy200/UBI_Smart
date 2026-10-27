"""工具链能力注册：模型只看到摘要，看不到宿主绝对路径。

Phase 1 只登记能力，不执行。真正的 launcher/环境适配在 P3 单独实现。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_KIND_RE = re.compile(r"^[a-z0-9_-]{1,32}$")


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

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(str(self.id or "")):
            raise ValueError(f"非法 toolchain id：{self.id!r}")
        if not _KIND_RE.fullmatch(str(self.kind or "")):
            raise ValueError(f"非法 toolchain kind：{self.kind!r}")
        roots = tuple(_validate_output_root(item) for item in self.output_roots)
        object.__setattr__(self, "output_roots", roots)
        object.__setattr__(self, "id", str(self.id))
        object.__setattr__(self, "kind", str(self.kind))
        object.__setattr__(self, "version", str(self.version or ""))

    def summary(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "version": self.version,
            "output_roots": list(self.output_roots),
        }


class ToolchainRegistry:
    def __init__(self, refs: Iterable[ToolchainRef] | None = None) -> None:
        self._refs: dict[str, ToolchainRef] = {}
        for ref in refs or ():
            self.register(ref)

    def register(self, ref: ToolchainRef) -> None:
        if ref.id in self._refs:
            raise ValueError(f"重复的 toolchain id：{ref.id}")
        self._refs[ref.id] = ref

    def get(self, toolchain_id: str) -> ToolchainRef | None:
        return self._refs.get(str(toolchain_id or ""))

    def summary(self) -> dict[str, Any]:
        return {
            "toolchains": [
                ref.summary()
                for ref in sorted(self._refs.values(), key=lambda item: item.id)
            ]
        }

    def clear(self) -> None:
        self._refs.clear()

    def __len__(self) -> int:
        return len(self._refs)

    def __iter__(self):
        return iter(self._refs.values())
