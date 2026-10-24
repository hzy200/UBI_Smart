"""ExecutionService：幂等执行、运行序号与请求/响应映射。"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coding_agent.execution.contracts import (
    ExecutionBackend,
    ExecutionConflict,
    ExecutionError,
    ExecutionRequest,
    ExecutionResult,
)
from coding_agent.sandbox.pathguard import ensure_inside

DEFAULT_MAX_READ_BYTES = 1024 * 1024


@dataclass(slots=True)
class _JournalEntry:
    request_hash: str
    run_id: str
    result: dict[str, Any]


class ExecutionJournal:
    """按 op_id 去重的 JSONL 回执日志；同一工作区可跨进程复用。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: dict[str, _JournalEntry] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                row = json.loads(line)
                op_id = str(row["op_id"])
                result = dict(row["result"])
                self._entries[op_id] = _JournalEntry(
                    request_hash=str(row.get("request_hash") or ""),
                    run_id=str(row.get("run_id") or ""),
                    result=result,
                )
            except (KeyError, TypeError, ValueError):
                continue

    def get(self, op_id: str) -> _JournalEntry | None:
        with self._lock:
            return self._entries.get(op_id)

    def put(
        self,
        op_id: str,
        request_hash: str,
        result: ExecutionResult,
        run_id: str = "",
    ) -> None:
        row = {
            "op_id": op_id,
            "request_hash": request_hash,
            "run_id": str(run_id or ""),
            "result": result.to_dict(),
        }
        with self._lock:
            self._entries[op_id] = _JournalEntry(
                request_hash=request_hash,
                run_id=str(run_id or ""),
                result=result.to_dict(),
            )
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
                handle.flush()

    def max_seq(self, run_id: str) -> int:
        with self._lock:
            return max(
                (
                    int(entry.result.get("seq") or 0)
                    for entry in self._entries.values()
                    if entry.run_id == str(run_id or "")
                ),
                default=0,
            )


class ExecutionService:
    """L3 调用的唯一执行入口；不在这里做审批/命令分级。"""

    def __init__(
        self,
        backend: ExecutionBackend,
        workspace: str,
        *,
        journal_path: str | Path | None = None,
    ) -> None:
        self.backend = backend
        self.workspace = str(workspace)
        self.journal = ExecutionJournal(
            Path(journal_path)
            if journal_path
            else Path(self.workspace) / ".agent" / "execution" / "receipts.jsonl"
        )
        self._seq: dict[str, int] = {}
        self._seq_lock = threading.Lock()

    def capabilities(self) -> dict[str, Any]:
        return {
            "service": "execution",
            "version": "1",
            **self.backend.capabilities(),
            "journal": str(self.journal.path),
        }

    def _next_seq(self, run_id: str) -> int:
        key = str(run_id or "")
        with self._seq_lock:
            if key not in self._seq:
                self._seq[key] = self.journal.max_seq(key)
            self._seq[key] += 1
            return self._seq[key]

    @staticmethod
    def _fingerprint(request: ExecutionRequest) -> str:
        raw = json.dumps(
            {
                "kind": request.kind,
                "workspace_id": request.workspace_id,
                "payload": request.payload,
                "timeout_s": request.timeout_s,
                "capture_limit": request.capture_limit,
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def run_raw(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        capture_limit: int | None = None,
    ) -> Any:
        """兼容旧工具的原始执行入口：走同一 L4 backend，不写契约回执。

        工具迁移到显式 op_id 前，用它保持 ExecResult 语义不变。
        """
        raw = getattr(self.backend, "run_raw", None)
        if callable(raw):
            return raw(
                command,
                cwd=cwd,
                timeout=timeout,
                capture_limit=capture_limit,
            )
        return self.backend.run(
            command,
            cwd=cwd or self.workspace,
            timeout=timeout if timeout is not None else 60.0,
            capture_limit=capture_limit or 0,
        )

    def execute(self, request: ExecutionRequest) -> ExecutionResult:
        if not str(request.op_id or "").strip():
            raise ExecutionError("op_id 不能为空")
        if not str(request.kind or "").strip():
            raise ExecutionError("kind 不能为空")
        fingerprint = self._fingerprint(request)
        known = self.journal.get(request.op_id)
        if known is not None:
            if known.request_hash != fingerprint:
                raise ExecutionConflict(
                    f"op_id {request.op_id!r} 已用于不同请求"
                )
            result = ExecutionResult.from_dict(known.result)
            result.replayed = True
            return result

        seq = self._next_seq(request.run_id)
        result = self._dispatch(request, seq)
        result.receipt_id = (
            f"{request.run_id or 'run'}:{request.op_id}"
        )
        self.journal.put(
            request.op_id, fingerprint, result, request.run_id
        )
        return result

    def _dispatch(
        self, request: ExecutionRequest, seq: int
    ) -> ExecutionResult:
        kind = request.kind
        payload = dict(request.payload or {})
        base = ExecutionResult(
            op_id=request.op_id,
            seq=seq,
            kind=kind,
            status="error",
        )
        try:
            if kind == "run":
                command = str(payload.get("command") or "")
                if not command.strip():
                    raise ExecutionError("command 不能为空")
                cwd = str(payload.get("cwd") or self.workspace)
                if self.workspace.startswith("/") and cwd.startswith("/"):
                    cwd = ensure_inside(
                        cwd, self.workspace, cwd=self.workspace
                    )
                raw = self.backend.run(
                    command,
                    cwd=cwd,
                    timeout=(
                        float(payload["timeout_s"])
                        if payload.get("timeout_s") is not None
                        else request.timeout_s
                    ),
                    capture_limit=int(
                        payload.get(
                            "capture_limit", request.capture_limit
                        )
                    ),
                )
                base.exit_code = int(raw.exit_code)
                base.stdout = raw.stdout
                base.stderr = raw.stderr
                base.duration_ms = int(raw.duration_ms)
                if getattr(raw, "timed_out", False):
                    base.status = "timeout"
                elif raw.ok:
                    base.status = "ok"
                else:
                    base.status = "failed"
                return base

            if kind == "read_file":
                path = str(payload.get("path") or "")
                if not path:
                    raise ExecutionError("path 不能为空")
                base.data = {
                    "content": self.backend.read_text(
                        path,
                        max_bytes=int(
                            payload.get(
                                "max_bytes", DEFAULT_MAX_READ_BYTES
                            )
                        ),
                    )
                }
                base.status = "ok"
                return base

            if kind == "write_file":
                path = str(payload.get("path") or "")
                if not path:
                    raise ExecutionError("path 不能为空")
                content = str(payload.get("content") or "")
                written = self.backend.write_bytes(
                    path, content.encode("utf-8")
                )
                base.data = {"bytes": written}
                base.status = "ok"
                return base

            if kind == "list_dir":
                path = str(payload.get("path") or "")
                entries = self.backend.list_dir(
                    path,
                    max_depth=int(payload.get("max_depth", 1)),
                    limit=int(payload.get("limit", 200)),
                )
                base.data = {
                    "entries": [
                        {"kind": entry[0], "path": entry[1]}
                        for entry in entries
                    ]
                }
                base.status = "ok"
                return base

            if kind == "stat":
                path = str(payload.get("path") or "")
                base.data = dict(self.backend.stat(path))
                base.status = "ok"
                return base

            if kind == "git":
                args = payload.get("args")
                if not isinstance(args, list) or not args:
                    raise ExecutionError("git args 必须是非空数组")
                raw = self.backend.git(
                    [str(arg) for arg in args],
                    cwd=(
                        str(payload.get("cwd"))
                        if payload.get("cwd")
                        else None
                    ),
                )
                base.exit_code = int(raw.exit_code)
                base.stdout = raw.stdout
                base.stderr = raw.stderr
                base.duration_ms = int(raw.duration_ms)
                base.status = "ok" if raw.ok else "failed"
                return base

            if kind == "snapshot":
                path = str(payload.get("path") or "")
                if not path:
                    raise ExecutionError("path 不能为空")
                info = dict(self.backend.stat(path))
                existed = bool(info.get("exists")) and bool(
                    info.get("is_file")
                )
                content = (
                    self.backend.read_text(
                        path, max_bytes=DEFAULT_MAX_READ_BYTES
                    )
                    if existed
                    else ""
                )
                snapshot_id = self.backend.snapshot(
                    path, content, existed=existed
                )
                base.data = {"snapshot_id": snapshot_id}
                base.snapshot_ids = [snapshot_id]
                base.status = "ok"
                return base

            if kind == "restore":
                snapshot_id = str(payload.get("snapshot_id") or "")
                if not snapshot_id:
                    raise ExecutionError("snapshot_id 不能为空")
                base.data = dict(
                    self.backend.restore(
                        snapshot_id,
                        str(payload.get("path") or "") or None,
                    )
                )
                base.status = "ok"
                return base

            if kind == "patch_apply":
                patch_path = str(payload.get("patch_path") or "")
                if not patch_path:
                    raise ExecutionError("patch_path 不能为空")
                raw = self.backend.apply_patch(
                    patch_path,
                    root=str(payload.get("root") or self.workspace),
                    check=bool(payload.get("check", False)),
                )
                base.exit_code = int(raw.exit_code)
                base.stdout = raw.stdout
                base.stderr = raw.stderr
                base.duration_ms = int(raw.duration_ms)
                base.status = "ok" if raw.ok else "failed"
                base.data = {
                    "patch": patch_path,
                    "check": bool(payload.get("check", False)),
                }
                return base

            if kind in {"worktree_create", "worktree_remove"}:
                raw = self.backend.worktree(
                    "create" if kind == "worktree_create" else "remove",
                    path=str(payload.get("path") or ""),
                    branch=str(payload.get("branch") or ""),
                    base=str(payload.get("base") or "HEAD"),
                )
                base.exit_code = int(raw.exit_code)
                base.stdout = raw.stdout
                base.stderr = raw.stderr
                base.duration_ms = int(raw.duration_ms)
                base.status = "ok" if raw.ok else "failed"
                return base

            base.status = "unsupported"
            base.error = f"执行契约尚未支持 kind={kind!r}"
            return base
        except ExecutionConflict:
            raise
        except Exception as exc:  # noqa: BLE001 - 失败要作为回执返回
            base.status = "error"
            base.error = f"{type(exc).__name__}: {exc}"
            return base

    def record_terminal(
        self,
        request: ExecutionRequest,
        status: str,
        *,
        error: str = "",
    ) -> ExecutionResult:
        """记录没有正常回执的终态（unknown/cancelled）。

        用于工具在返回前被中断：结果不可知时写 `unknown`，明确取消写
        `cancelled`；后续同 op_id 请求命中该回执，不会被自动重放。
        """
        if status not in {"unknown", "cancelled"}:
            raise ExecutionError(f"非法终态：{status}")
        fingerprint = self._fingerprint(request)
        known = self.journal.get(request.op_id)
        if known is not None:
            if known.request_hash != fingerprint:
                raise ExecutionConflict(
                    f"op_id {request.op_id!r} 已用于不同请求"
                )
            result = ExecutionResult.from_dict(known.result)
            result.replayed = True
            return result
        seq = self._next_seq(request.run_id)
        result = ExecutionResult(
            op_id=request.op_id,
            seq=seq,
            kind=request.kind,
            status=status,
            error=error,
            receipt_id=f"{request.run_id or 'run'}:{request.op_id}",
        )
        self.journal.put(
            request.op_id, fingerprint, result, request.run_id
        )
        return result

    def record_unknown(
        self, request: ExecutionRequest, *, error: str = ""
    ) -> ExecutionResult:
        return self.record_terminal(request, "unknown", error=error)

    def record_cancelled(
        self, request: ExecutionRequest, *, error: str = ""
    ) -> ExecutionResult:
        return self.record_terminal(request, "cancelled", error=error)

    def close(self) -> None:
        close = getattr(self.backend, "close", None)
        if callable(close):
            close()
