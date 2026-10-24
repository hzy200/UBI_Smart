"""A3→A4 执行契约（Phase C）。

`ExecutionService` 是编排面调用执行面的唯一入口；工具与编排行为在 C2/C3
逐步迁移，当前先提供进程内适配与 run/文件读写契约。
"""

from coding_agent.execution.contracts import (
    ExecutionBackend,
    ExecutionConflict,
    ExecutionRequest,
    ExecutionResult,
)
from coding_agent.execution.sandbox_backend import SandboxExecutionBackend
from coding_agent.execution.sandbox_proxy import ExecutionSandbox
from coding_agent.execution.service import ExecutionService

__all__ = [
    "ExecutionBackend",
    "ExecutionConflict",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionService",
    "ExecutionSandbox",
    "SandboxExecutionBackend",
]
