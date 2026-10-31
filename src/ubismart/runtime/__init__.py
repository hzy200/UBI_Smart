"""AgentRuntime —— 唯一编排入口。

所有前端（CLI / TUI / Web）都只跟这一层打交道：
它持有图、沙箱与安全策略，把 LangGraph 的原始流翻译成语义化的领域事件。
前端拿不到工具，也就无法绕过命令分级审批。

实现按职责拆在包里，但 `ubismart.runtime` 这个导入路径与公开符号保持不变：

    async for event in AgentRuntime(settings).run("列出文件", thread_id="x"):
        match event.type: ...

    core.py       编排、运行、会话与资源生命周期
    event_map.py  LangGraph update → 领域事件的翻译层
    stream.py     事件流、收尾、错误与成本汇总
"""

from __future__ import annotations

from .core import AgentRuntime, submit_steering_command
from .event_map import (
    _review_blocked,
    _verification_passed,
    _verification_verdict,
)
from .sessions import HistoryMessage

__all__ = [
    "AgentRuntime",
    "HistoryMessage",
    "_review_blocked",
    "_verification_passed",
    "_verification_verdict",
    "submit_steering_command",
]
