"""host_exec：在 AppContainer 内运行已注册工具链的受控命令。

与 `shell_exec` 的区别：
- 不接受 shell 字符串，只接受 argv 数组，不经过命令解释器；
- executable 必须是 A2L 显式注册的 toolchain 命令；
- 只在 host 工作区、AppContainer 档可用；否则 A2L 直接拒绝。
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from ubismart.security.secrets import redact_secrets
from ubismart.tools.artifacts import CallArtifact, pack

HOST_EXEC_TOOL_NAME = "host_exec"

HOST_EXEC_DESCRIPTION = """\
在宿主 AppContainer 内运行一条已注册工具链命令（argv 数组，不经 shell）。

何时使用：需要在 host 工作区运行编译器/测试等已注册工具链命令。
何时不用：修改文件请用文件工具；未注册的命令会被 A2L 拒绝。

注意：
- toolchain 与可执行命令必须已由 A2L 显式注册；
- 只有声明过的 output_roots 可写，其余工作区路径只读；
- 默认无网络；超时会终止进程。
"""


class HostExecInput(BaseModel):
    toolchain: str = Field(description="A2L 注册的 toolchain id")
    argv: list[str] = Field(
        description="完整 argv；argv[0] 必须是该工具链注册的命令"
    )
    cwd: str = Field(default=".", description="工作区相对目录")
    reason: str = Field(description="执行意图，一句话说明")


def build_host_exec_tool(backend: Any) -> BaseTool:
    def _run(
        toolchain: str,
        argv: list[str],
        reason: str,
        cwd: str = ".",
    ) -> str:
        try:
            data = backend.exec_argv(
                toolchain=toolchain,
                argv=argv,
                cwd=cwd,
            )
        except Exception as exc:  # noqa: BLE001 - 边界处回灌错误
            artifact = CallArtifact(
                tool=HOST_EXEC_TOOL_NAME, ok=False
            )
            return pack(f"[host.exec] {reason}\n执行失败：{exc}", artifact)
        stdout = redact_secrets(str(data.get("stdout") or ""))
        stderr = redact_secrets(str(data.get("stderr") or ""))
        exit_code = int(data.get("exit_code") or 0)
        timed_out = bool(data.get("timed_out"))
        status = "超时" if timed_out else f"exit={exit_code}"
        body = f"[host.exec {toolchain}] {reason}\n{status}"
        if stdout:
            body += f"\n--- stdout ---\n{stdout}"
        if stderr:
            body += f"\n--- stderr ---\n{stderr}"
        artifact = CallArtifact(
            tool=HOST_EXEC_TOOL_NAME,
            ok=bool(exit_code == 0 and not timed_out),
        )
        return pack(body, artifact)

    return StructuredTool.from_function(
        func=_run,
        name=HOST_EXEC_TOOL_NAME,
        description=HOST_EXEC_DESCRIPTION,
        args_schema=HostExecInput,
    )
