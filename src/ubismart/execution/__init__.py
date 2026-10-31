"""A3→A4 执行契约（Phase C）。

`ExecutionService` 是编排面调用执行面的唯一入口；工具与编排行为在 C2/C3
逐步迁移，当前先提供进程内适配与 run/文件读写契约。
"""

from ubismart.execution.contracts import (
    ExecutionBackend,
    ExecutionConflict,
    ExecutionRequest,
    ExecutionResult,
    ExecutionTarget,
    normalize_execution_target,
)
from ubismart.execution.file_access import FileAccess, SandboxFileAccess
from ubismart.execution.host_backend import (
    HostCaller,
    HostCallError,
    HostExecutionBackend,
)
from ubismart.execution.host_file_access import HostFileAccess
from ubismart.execution.sandbox_backend import SandboxExecutionBackend
from ubismart.execution.sandbox_proxy import ExecutionSandbox
from ubismart.execution.service import ExecutionService

__all__ = [
    "ExecutionBackend",
    "ExecutionConflict",
    "ExecutionRequest",
    "ExecutionResult",
    "ExecutionService",
    "ExecutionSandbox",
    "ExecutionTarget",
    "FileAccess",
    "HostCallError",
    "HostCaller",
    "HostExecutionBackend",
    "HostFileAccess",
    "SandboxExecutionBackend",
    "SandboxFileAccess",
    "normalize_execution_target",
]
