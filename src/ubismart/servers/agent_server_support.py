"""A3 Agent Server 的运行状态与 SSE 原语（P1.2）。

独立于 HTTP 路由，便于同进程回退与独立 Agent Server 复用同一套
RunState / SSE 语义，避免两套实现分叉。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from ubismart.events import ApprovalRequested, Event, SteeringApplied
from ubismart.runtime import AgentRuntime


class _ContextSecretStore:
    """运行期密钥只在 A3 内存；A2 每次请求短期注入。"""

    def __init__(self, keys: dict[str, str] | None = None) -> None:
        self._keys = dict(keys or {})

    def get(self, name: str) -> str:
        return self._keys.get(str(name), "")

    def set(self, name: str, value: str) -> None:
        self._keys[str(name)] = str(value)

    def delete(self, name: str) -> None:
        self._keys.pop(str(name), None)

    def has(self, name: str) -> bool:
        return bool(self.get(name))


@dataclass
class RunState:
    key: tuple[str, str]
    thread_id: str
    runtime: AgentRuntime
    events: deque[tuple[int, Event]] = field(default_factory=deque)
    chars: int = 0
    seq: int = 0
    done: bool = False
    suspended: bool = False
    updated: float = field(default_factory=time.monotonic)
    task: Any = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)


def _sse(event: Event) -> str:
    return f"data: {event.model_dump_json()}\n\n"


def _sse_with_id(event: Event, seq: int) -> str:
    return f"id: {seq}\ndata: {event.model_dump_json()}\n\n"


def _is_suspended(event: Event) -> bool:
    return isinstance(event, ApprovalRequested) or (
        isinstance(event, SteeringApplied) and event.action == "pause"
    )


def _terminal_payload(payload: str) -> bool:
    try:
        data = json.loads(payload)
    except ValueError:
        return False
    kind = data.get("type")
    if kind in {"run_finished", "run_failed", "approval_requested"}:
        return True
    return kind == "steering_applied" and data.get("action") == "pause"


def _safe_tenant(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]", "", str(value or ""))[:64]
    return text or "local"
