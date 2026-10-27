"""工具注册表。

`allow_write=False`（默认）时只有只读能力：shell 限 L0，文件工具只有 file_read。
`allow_write=True` 时放开 L1 与 file_write / file_edit。

文件修改一律走 file_* 工具，不走 shell —— 这样才有精确替换、diff 与备份。

**放行与否不在这里决定**，而是由 approval_gate + tools 节点依据 SessionPolicy
逐次判定；这里只决定「暴露哪些工具」。新工具记得在
`graph/nodes/approve.py::tool_level` 登记等级，否则按最危险的 L3 处理。
"""

from __future__ import annotations

import time
from collections.abc import MutableMapping
from typing import Any

from langchain_core.tools import BaseTool

from coding_agent.config import Settings, get_settings
from coding_agent.sandbox.backend import build_sandbox
from coding_agent.tools.artifacts import pack, unpack
from coding_agent.tools.code_intel import build_code_intel_tool
from coding_agent.tools.deps import build_deps_tools
from coding_agent.tools.files import build_file_tools
from coding_agent.tools.git import build_git_tools
from coding_agent.tools.memory import build_memory_tools
from coding_agent.tools.search import build_search_tools
from coding_agent.tools.shell import build_shell_tool
from coding_agent.tools.symbols import build_symbol_tool
from coding_agent.tools.testrun import build_test_tool


def build_tools(
    settings: Settings | None = None,
    *,
    allow_write: bool = False,
    allow_execution: bool = True,
    sandbox: Any | None = None,
    write_scope: list[str] | None = None,
    read_cache: MutableMapping[str, bytes] | None = None,
    file_access: Any | None = None,
    host_mode: bool = False,
) -> list[BaseTool]:
    """构造本次会话的全部工具。

    注意：放行与否**不在这里决定**，而是由 approval_gate + tools 节点依据
    SessionPolicy 逐次判定。这里只决定「暴露哪些工具」。

    `allow_execution=False`（只读会话）时，符号检索退回 AST，code_intel 不启动
    LSP 进程；这是“只读 = 不可运行”的防线。

    `sandbox` 由调用方传入以便与 verify、runtime 共用一份实例：环境探测的结果
    缓存在实例上，每多建一份就多探一遍。
    """
    settings = settings or get_settings()
    sandbox = sandbox or build_sandbox(settings)

    if host_mode:
        # Phase 1 host 只暴露只读文件/检索；shell、git、依赖、测试、记忆都不碰。
        host_tools: list[BaseTool] = []
        host_tools += build_file_tools(
            settings,
            sandbox,
            allow_write=False,
            read_cache=read_cache,
            file_access=file_access,
        )
        host_tools += [
            tool
            for tool in build_search_tools(
                settings, sandbox, file_access=file_access
            )
            if tool.name == "find_files"
        ]
        return [_instrument(tool) for tool in host_tools]

    tools: list[BaseTool] = [
        build_shell_tool(settings, sandbox, read_cache=read_cache)
    ]
    # 其余工具与 shell 共用同一个 sandbox 实例，工作区边界一致
    tools += build_file_tools(
        settings,
        sandbox,
        allow_write=allow_write,
        write_scope=write_scope,
        read_cache=read_cache,
        file_access=file_access,
    )
    tools += build_memory_tools(
        settings,
        sandbox,
        allow_write=allow_write,
        read_cache=read_cache,
    )
    tools += build_git_tools(settings, sandbox)
    tools += build_deps_tools(settings, sandbox)
    tools += build_search_tools(
        settings, sandbox, file_access=file_access
    )
    tools.append(
        build_symbol_tool(
            settings, sandbox, allow_lsp=allow_execution
        )
    )
    tools.append(
        build_code_intel_tool(
            settings, sandbox, allow_lsp=allow_execution
        )
    )
    tools.append(build_test_tool(settings, sandbox))
    return [_instrument(tool) for tool in tools]


def _instrument(tool: BaseTool) -> BaseTool:
    """给每个工具补上 `duration_ms`，让直接 `tool.invoke` 的调用方也能拿到。

    tools 节点会再测一次（包含排队/线程开销）并覆盖；这里测的是工具自身
    执行时间，主要用于评测、脚本和没有经过图的直调。
    """
    original = getattr(tool, "func", None)
    if original is None:
        return tool

    def timed(*args, **kwargs):  # noqa: ANN002, ANN003
        started = time.perf_counter()
        raw = original(*args, **kwargs)
        text, artifact = unpack(raw)
        if not isinstance(artifact, dict):
            return raw
        # 基类字段的默认值是 None，`setdefault` 会把 None 当成“已设置”。
        if not artifact.get("duration_ms"):
            artifact["duration_ms"] = int(
                (time.perf_counter() - started) * 1000
            )
        return pack(text, artifact)

    tool.func = timed  # type: ignore[attr-defined]
    return tool
