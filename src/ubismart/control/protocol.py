"""兼容层：A2↔A3 协议对象已下沉到 `coding_agent.agent_protocol`。

保留此模块是为了不打断既有导入；新代码请直接使用 `agent_protocol`。
"""

from __future__ import annotations

from coding_agent.agent_protocol import (
    AGENT_API_VERSION,
    AGENT_CAPABILITIES,
    AgentMeta,
    Compatibility,
    check_compatibility,
)

__all__ = [
    "AGENT_API_VERSION",
    "AGENT_CAPABILITIES",
    "AgentMeta",
    "Compatibility",
    "check_compatibility",
]
