"""worker 结果的崩溃恢复日志。

scheduler 的结果只活在内存与 checkpoint 的节点边界上：进程在调度中途被杀
时，已经跑完的 explore/review 结果会一起丢，resume 后只能重跑。这里把
**成功完成的只读结果**写到宿主审计目录，下次调度初始化时按键复用。

键的设计（全部命中才复用）：
- `thread_id`：会话；
- `request_hash`：本轮用户请求；
- `task_hash`：任务本身的稳定字段（goal/inputs/依赖/预算等）；
- `workspace_hash`：git HEAD + 是否干净 + 文件量级。

默认只在「git 仓库、工作区干净、拿得到 HEAD」时启用。这样输入与代码状态
都一致，复用才安全。implement 永不进入这里：补丁/worktree 可能已经变化。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ubismart.platform.filelock import FileLock
from ubismart.security.secrets import redact_secrets

MAX_JOURNAL_BYTES = 10 * 1024 * 1024
ALLOWED_STATUSES = frozenset({"success", "partial"})
# 这些字段是“本次运行”的计量，不是结论；复用时必须清零，避免把历史
# token 重复计入当前 run 的成本统计。
_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "duration_ms",
    "llm_ms",
    "tool_ms",
    "queue_wait_ms",
    "merge_wait_ms",
    "budget_used",
    "tool_calls",
    "proposed_tasks",
)
# task 里会随调度反复变化的字段，不参与指纹。
_TRANSIENT_TASK_FIELDS = frozenset(
    {"retry_context", "repo_map", "seed_from_workspace", "baseline_paths"}
)
_FINGERPRINT_TASK_FIELDS = (
    "id",
    "role",
    "goal",
    "inputs",
    "acceptance",
    "depends_on",
    "depends_mode",
    "write_scope",
    "max_rounds",
    "max_repairs",
    "timeout",
    "max_tokens",
    "stage",
    "output_format",
    "output_path",
)


def _digest(payload: Any) -> str:
    try:
        text = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, default=str
        )
    except (TypeError, ValueError):
        text = repr(payload)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:16]


def request_fingerprint(request: str) -> str:
    return _digest({"request": str(request or "")})


def task_fingerprint(task: dict[str, Any]) -> str:
    payload = {
        key: task.get(key)
        for key in _FINGERPRINT_TASK_FIELDS
        if key not in _TRANSIENT_TASK_FIELDS
    }
    return _digest(payload)


def workspace_fingerprint(summary: dict[str, Any]) -> str:
    """工作区状态指纹；只取廉价且稳定的字段。"""
    payload = {
        "git": bool(summary.get("git")),
        "git_root": bool(summary.get("git_root")),
        "git_clean": bool(summary.get("git_clean")),
        "git_head": str(summary.get("git_head") or ""),
        "file_count": int(summary.get("file_count") or 0),
        "test_file_count": int(summary.get("test_file_count") or 0),
    }
    return _digest(payload)


def parse_roles(raw: str, *, default: tuple[str, ...]) -> set[str]:
    roles = {
        item.strip().lower()
        for item in str(raw or "").split(",")
        if item.strip()
    }
    return roles or set(default)


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact_secrets(value)
    if isinstance(value, dict):
        return {str(key): _redact_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    return value


def _trim_result(result: dict[str, Any]) -> dict[str, Any]:
    """只保留结论字段；计量与工具轨迹属于上一次运行。"""
    trimmed = {
        key: value
        for key, value in result.items()
        if key not in _USAGE_FIELDS
    }
    trimmed["input_tokens"] = 0
    trimmed["output_tokens"] = 0
    trimmed["duration_ms"] = 0
    trimmed["llm_ms"] = 0
    trimmed["tool_ms"] = 0
    trimmed["tool_calls"] = []
    trimmed["proposed_tasks"] = []
    trimmed["budget_used"] = {}
    return _redact_value(trimmed)


class WorkerJournal:
    """JSONL 结果日志；写入失败只降级为「没有日志」。"""

    def __init__(
        self,
        path: str | Path | None,
        *,
        enabled: bool = True,
        max_bytes: int = MAX_JOURNAL_BYTES,
    ) -> None:
        self.path = Path(path) if path else None
        self.enabled = bool(enabled and self.path)
        self.max_bytes = max(0, int(max_bytes or 0))
        self._cache: (
            dict[tuple[str, str, str, str], dict[str, Any]] | None
        ) = None
        self._cache_state: tuple[int, int] | None = None

    def _file_state(self) -> tuple[int, int] | None:
        if self.path is None:
            return None
        try:
            stat = self.path.stat()
        except OSError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _key(
        self,
        thread_id: str,
        request_hash: str,
        task_hash: str,
        workspace_hash: str,
    ) -> tuple[str, str, str, str]:
        return (thread_id, request_hash, task_hash, workspace_hash)

    def record(
        self,
        *,
        thread_id: str,
        request_hash: str,
        task_hash: str,
        workspace_hash: str,
        role: str,
        status: str,
        attempt: int,
        result: dict[str, Any],
    ) -> None:
        if not self.enabled or self.path is None:
            return
        if status not in ALLOWED_STATUSES:
            return
        entry = {
            "thread_id": str(thread_id),
            "request_hash": str(request_hash),
            "task_hash": str(task_hash),
            "workspace_hash": str(workspace_hash),
            "role": str(role),
            "status": str(status),
            "attempt": max(1, int(attempt or 1)),
            "result": _trim_result(result),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock = self.path.with_suffix(self.path.suffix + ".lock")
            with FileLock(lock):
                if (
                    self.path.exists()
                    and self.max_bytes > 0
                    and self.path.stat().st_size > self.max_bytes
                ):
                    self.path.replace(
                        self.path.with_suffix(".1.jsonl")
                    )
                    self._cache = None
                    self._cache_state = None
                line = json.dumps(
                    entry,
                    ensure_ascii=False,
                    default=str,
                    separators=(",", ":"),
                )
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                if self._cache is not None:
                    self._cache[
                        self._key(
                            entry["thread_id"],
                            entry["request_hash"],
                            entry["task_hash"],
                            entry["workspace_hash"],
                        )
                    ] = entry
                    self._cache_state = self._file_state()
        except OSError:
            self.enabled = False
            self._cache = None
            self._cache_state = None

    def _load(self) -> dict[tuple[str, str, str, str], dict[str, Any]]:
        cache: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        if self.path is None or not self.path.exists():
            return cache
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(entry, dict):
                        continue
                    key = self._key(
                        str(entry.get("thread_id", "")),
                        str(entry.get("request_hash", "")),
                        str(entry.get("task_hash", "")),
                        str(entry.get("workspace_hash", "")),
                    )
                    # 后写的覆盖先写的：同一个键以最新一次成功为准。
                    cache[key] = entry
        except OSError:
            return {}
        return cache

    def lookup(
        self,
        *,
        thread_id: str,
        request_hash: str,
        task_hash: str,
        workspace_hash: str,
        allow_statuses: set[str] | frozenset[str] = ALLOWED_STATUSES,
    ) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        state = self._file_state()
        if self._cache is None or state != self._cache_state:
            self._cache = self._load()
            self._cache_state = state
        entry = self._cache.get(
            self._key(
                thread_id, request_hash, task_hash, workspace_hash
            )
        )
        if not entry or str(entry.get("status", "")) not in allow_statuses:
            return None
        return entry
