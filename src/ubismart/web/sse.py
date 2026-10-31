"""SSE 帧、运行缓冲与断线重放的纯函数。

这些函数不依赖 FastAPI，也不持有应用状态，便于单独测试。
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any

from coding_agent.events import ApprovalRequested, Event, SteeringApplied


@dataclass
class StreamEntry:
    """一次运行的事件缓冲；HTTP 连接只是订阅者，不再拥有运行本身。"""

    key: tuple[str, str]
    public_thread: str
    runtime: Any
    events: list[tuple[int, Event]] = field(default_factory=list)
    chars: int = 0
    seq: int = 0
    done: bool = False
    suspended: bool = False
    updated: float = field(default_factory=time.monotonic)
    task: Any = None
    subscribers: list[asyncio.Queue] = field(default_factory=list)


def sse(event: Event) -> str:
    """一条领域事件 → 一个 SSE 数据帧。事件本身就是 pydantic 模型，直接序列化。"""
    return f"data: {event.model_dump_json()}\n\n"


def sse_with_id(event: Event, seq: int) -> str:
    """缓冲模式帧：带事件序号，便于 `Last-Event-ID` 断线重放。"""
    return f"id: {seq}\ndata: {event.model_dump_json()}\n\n"


def is_suspended(event: Event) -> bool:
    return isinstance(event, ApprovalRequested) or (
        isinstance(event, SteeringApplied) and event.action == "pause"
    )


def events_after(
    entry: StreamEntry, last: int
) -> list[tuple[int, Event]]:
    """只从缓冲尾部取 `seq > last` 的事件。

    事件按 seq 单调追加，裁剪只发生在头部；从尾部反向扫描到游标即可，
    稳态每次只处理新事件。旧实现每次循环都 `list(entry.events)` 全量
    重扫，长会话下是 O(n²)。
    """
    fresh: list[tuple[int, Event]] = []
    for seq, event in reversed(entry.events):
        if seq <= last:
            break
        fresh.append((seq, event))
    fresh.reverse()
    return fresh


def event_key(user: str, public_thread: str) -> str:
    """传输层 key 必须带用户，避免多租户 thread 撞车。"""
    return f"{user}:{public_thread}"


def event_is_terminal(payload: str) -> bool:
    """跨进程重放没有本地 StreamEntry，只能从事件载荷判断收尾。"""
    try:
        data = json.loads(payload)
    except ValueError:
        return False
    kind = data.get("type")
    if kind in {"run_finished", "run_failed", "approval_requested"}:
        return True
    return kind == "steering_applied" and data.get("action") == "pause"
