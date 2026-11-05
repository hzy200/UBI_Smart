"""工具注册入口。

`build_tools` 延迟导入：文档解析的隔离子进程只会 `import
ubismart.tools.documents.extract`，如果包初始化就拉起 registry，会连带导入
vision → llm → langchain_openai → transformers（实测约 8 秒）。spawn 子进程
每次都要付这笔启动成本，满载 CI 上会直接撞上解析超时。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ubismart.tools.registry import build_tools

__all__ = ["build_tools"]


def __getattr__(name: str) -> object:
    if name == "build_tools":
        from ubismart.tools.registry import build_tools

        globals()[name] = build_tools
        return build_tools
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
