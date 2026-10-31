"""工具调用级的执行上下文（Phase C2）。

工具本身没有 `op_id`，但图节点知道 `run_id + tool_call_id`。这里用
ContextVar 把二者传进同步工具，`ExecutionSandbox` 据此生成稳定的 op_id：
同一个工具调用重试/恢复时序号从 1 重新开始，命中执行回执，避免重复副作用。
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class ToolExecution:
    run_id: str
    call_id: str
    _counter: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def next_op_id(self) -> str:
        with self._lock:
            self._counter += 1
            return f"{self.run_id or 'run'}:{self.call_id or 'call'}:{self._counter}"


_CURRENT: ContextVar[ToolExecution | None] = ContextVar(
    "coding_agent_tool_execution", default=None
)


@contextmanager
def tool_execution_context(
    run_id: str, call_id: str
) -> Iterator[ToolExecution]:
    ctx = ToolExecution(str(run_id or ""), str(call_id or ""))
    token = _CURRENT.set(ctx)
    try:
        yield ctx
    finally:
        _CURRENT.reset(token)


def current_tool_execution() -> ToolExecution | None:
    return _CURRENT.get()
