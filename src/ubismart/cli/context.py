"""CLI 命令模块的晚绑定入口。

测试会 monkeypatch `ubismart.cli.app.console` 与 `.get_settings`。
命令模块若在 import 时直接绑定这两个名字，补丁就只作用在 app 模块上，
命令仍走旧对象。这里用代理把每次访问转发到 app 模块的当前值。
"""

from __future__ import annotations

import sys
from typing import Any


def _app() -> Any:
    return sys.modules["ubismart.cli.app"]


class _ConsoleProxy:
    def __getattr__(self, name: str) -> Any:
        return getattr(_app().console, name)


console = _ConsoleProxy()


def get_settings() -> Any:
    return _app().get_settings()


def AgentRuntime(*args: Any, **kwargs: Any) -> Any:
    """晚绑定的 AgentRuntime：测试会整体替换 `cli.app.AgentRuntime`。"""
    return _app().AgentRuntime(*args, **kwargs)
