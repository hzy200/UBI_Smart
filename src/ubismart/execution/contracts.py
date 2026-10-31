"""执行请求/响应与后端协议。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ubismart.workspace.model import WorkspaceKind

ExecutionTarget = Literal["auto", "host", "sandbox"]

EXECUTION_TARGETS: frozenset[str] = frozenset({"auto", "host", "sandbox"})


def normalize_execution_target(value: str | None) -> ExecutionTarget:
    """未知/空 target 按 `auto` 处理；路由层再做 kind 冲突检查。"""
    text = str(value or "").strip().lower()
    return text if text in EXECUTION_TARGETS else "auto"  # type: ignore[return-value]


class ExecutionError(RuntimeError):
    """执行契约的调用方错误。"""


class ExecutionConflict(ExecutionError):
    """同一个 op_id 被用于不同的请求。"""


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    op_id: str
    kind: str
    workspace_id: str = ""
    workspace_kind: WorkspaceKind = "sandbox"
    target: ExecutionTarget = "auto"
    grant_id: str = ""
    run_id: str = ""
    tool_call_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    timeout_s: float = 60.0
    capture_limit: int = 0


@dataclass(slots=True)
class ExecutionResult:
    op_id: str
    seq: int
    kind: str
    status: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    duration_ms: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: list[str] = field(default_factory=list)
    snapshot_ids: list[str] = field(default_factory=list)
    receipt_id: str = ""
    replayed: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "op_id": self.op_id,
            "seq": self.seq,
            "kind": self.kind,
            "status": self.status,
            "exit_code": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "duration_ms": self.duration_ms,
            "data": self.data,
            "artifacts": list(self.artifacts),
            "snapshot_ids": list(self.snapshot_ids),
            "receipt_id": self.receipt_id,
            "replayed": self.replayed,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ExecutionResult:
        return cls(
            op_id=str(payload.get("op_id") or ""),
            seq=int(payload.get("seq") or 0),
            kind=str(payload.get("kind") or ""),
            status=str(payload.get("status") or "error"),
            exit_code=payload.get("exit_code"),
            stdout=str(payload.get("stdout") or ""),
            stderr=str(payload.get("stderr") or ""),
            duration_ms=int(payload.get("duration_ms") or 0),
            data=dict(payload.get("data") or {}),
            artifacts=list(payload.get("artifacts") or []),
            snapshot_ids=list(payload.get("snapshot_ids") or []),
            receipt_id=str(payload.get("receipt_id") or ""),
            replayed=bool(payload.get("replayed", False)),
            error=str(payload.get("error") or ""),
        )


class ExecutionBackend(Protocol):
    """L4 适配器；只负责执行，不做审批/命令分级。"""

    def capabilities(self) -> dict[str, Any]: ...
    def run(
        self,
        command: str,
        *,
        cwd: str,
        timeout: float,
        capture_limit: int,
    ) -> Any: ...
    def read_text(self, path: str, *, max_bytes: int) -> str: ...
    def write_bytes(self, path: str, data: bytes) -> int: ...
    def list_dir(
        self, path: str, *, max_depth: int = 1, limit: int = 200
    ) -> list[tuple[str, str]]: ...
    def stat(self, path: str) -> Any: ...
