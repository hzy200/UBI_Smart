"""工作区引用：跨层传递的是句柄，不是宿主绝对路径。

`host` 工作区由客户端 A2L 持有绝对路径；A2/A3 只拿
`workspace_id + kind + root_handle`。`root_handle` 是不透明 ID，不允许
编码路径（否则会绕开“绝对路径不上云”的约束）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

WorkspaceKind = Literal["host", "sandbox"]

WORKSPACE_KINDS: frozenset[str] = frozenset({"host", "sandbox"})

# 句柄只允许 URL-safe 字符；逗号、冒号、斜杠都不允许，避免被当成路径。
_HANDLE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def normalize_workspace_kind(value: str | None) -> WorkspaceKind:
    """未知/空值按最保守的 `sandbox` 处理（与旧行为一致）。"""
    text = str(value or "").strip().lower()
    return text if text in WORKSPACE_KINDS else "sandbox"  # type: ignore[return-value]


@dataclass(frozen=True, slots=True)
class WorkspaceRef:
    """一次执行所属的工作区。"""

    workspace_id: str
    kind: WorkspaceKind = "sandbox"
    root_handle: str = ""
    display_name: str = ""

    def __post_init__(self) -> None:
        workspace_id = str(self.workspace_id or "").strip()
        if not workspace_id:
            raise ValueError("workspace_id 不能为空")
        kind = normalize_workspace_kind(self.kind)
        handle = str(self.root_handle or "").strip()
        if kind == "host" and not handle:
            raise ValueError("host 工作区必须提供 root_handle")
        if handle and not _HANDLE_RE.fullmatch(handle):
            raise ValueError(
                "root_handle 只能是 URL-safe 不透明 ID，不允许包含路径字符"
            )
        object.__setattr__(self, "workspace_id", workspace_id)
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "root_handle", handle)
        object.__setattr__(self, "display_name", str(self.display_name or ""))

    def to_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "kind": self.kind,
            "root_handle": self.root_handle,
            "display_name": self.display_name,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> WorkspaceRef:
        return cls(
            workspace_id=str(payload.get("workspace_id") or ""),
            kind=normalize_workspace_kind(payload.get("kind")),
            root_handle=str(payload.get("root_handle") or ""),
            display_name=str(payload.get("display_name") or ""),
        )
