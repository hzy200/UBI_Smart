"""兼容代理：让既有工具经 ExecutionService 的 L4 backend 执行命令。

工具仍拿到 `sandbox.run(...) -> ExecResult` 的形状，但实际调用路径已经是
A4 backend；显式 op_id/幂等由 `ExecutionService.execute()` 提供给新代码。
"""

from __future__ import annotations

from typing import Any

from ubismart.execution.context import current_tool_execution
from ubismart.execution.contracts import ExecutionRequest
from ubismart.execution.service import ExecutionService
from ubismart.sandbox.wsl_exec import ExecResult


class ExecutionSandbox:
    def __init__(self, real: Any, execution: ExecutionService) -> None:
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_execution", execution)

    @property
    def execution(self) -> ExecutionService:
        return self._execution

    @property
    def settings(self) -> Any:
        return self._real.settings

    @settings.setter
    def settings(self, value: Any) -> None:
        self._real.settings = value

    def run(
        self,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float | None = None,
        capture_limit: int | None = None,
    ) -> Any:
        context = current_tool_execution()
        if context is None:
            # 运行时探针/合并等没有工具调用上下文，保持原始 ExecResult。
            return self._execution.run_raw(
                command,
                cwd=cwd,
                timeout=timeout,
                capture_limit=capture_limit,
            )
        result = self._execution.execute(
            ExecutionRequest(
                op_id=context.next_op_id(),
                kind="run",
                workspace_id=self._execution.workspace,
                workspace_kind=self._execution.workspace_kind,
                target="auto",
                run_id=context.run_id,
                tool_call_id=context.call_id,
                payload={
                    "command": command,
                    "cwd": cwd,
                    "timeout_s": timeout,
                },
                timeout_s=timeout if timeout is not None else 60.0,
                capture_limit=capture_limit or 0,
            )
        )
        if result.status == "error":
            # 旧工具依赖异常传播，保留语义。
            raise RuntimeError(result.error or "执行失败")
        exit_code = (
            int(result.exit_code)
            if result.exit_code is not None
            else (0 if result.status == "ok" else 1)
        )
        return ExecResult(
            command=command,
            exit_code=exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            timed_out=result.status == "timeout",
        )

    def cancel_active(self) -> int:
        """把 /cancel 转给真实沙箱；没有该能力时返回 0。"""
        cancel = getattr(self._real, "cancel_active", None)
        return int(cancel() if callable(cancel) else 0)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_real", "_execution"}:
            object.__setattr__(self, name, value)
        else:
            setattr(self._real, name, value)
