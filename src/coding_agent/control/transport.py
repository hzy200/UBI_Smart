"""兼容层：共享事件传输已下沉到 `coding_agent.protocol.event_transport`。

保留此模块是为了不打断既有导入；新代码请直接使用 `event_transport`。
"""

from __future__ import annotations

from coding_agent.protocol.event_transport import (
    DEFAULT_MAXLEN,
    EventTransport,
    MemoryEventTransport,
    RedisStreamEventTransport,
    build_event_transport,
)

__all__ = [
    "DEFAULT_MAXLEN",
    "EventTransport",
    "MemoryEventTransport",
    "RedisStreamEventTransport",
    "build_event_transport",
]
