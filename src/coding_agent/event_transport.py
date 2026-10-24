"""共享事件传输（Phase B1/B3）。

单实例默认用内存适配器；多实例用 Redis Streams：按 thread 一个 stream，
事件字段为 ``seq`` + ``data``，短 TTL/近似长度上限，A2 从 stream 重放。
MySQL 不是事件传输，也不承担热重放。
"""

from __future__ import annotations

import threading
from collections import deque
from typing import Protocol

DEFAULT_MAXLEN = 10_000


class EventTransport(Protocol):
    def publish(self, thread_id: str, seq: int, payload: str) -> None: ...
    def replay(
        self, thread_id: str, after: int = 0, *, limit: int = 2_000
    ) -> list[tuple[int, str]]: ...
    def latest_seq(self, thread_id: str) -> int: ...
    def close(self) -> None: ...


class MemoryEventTransport:
    """单实例默认；保留最近 maxlen 条事件，按 seq 过滤。"""

    def __init__(self, *, maxlen: int = DEFAULT_MAXLEN) -> None:
        self._maxlen = max(1, int(maxlen))
        self._streams: dict[str, deque[tuple[int, str]]] = {}
        self._lock = threading.Lock()

    def publish(self, thread_id: str, seq: int, payload: str) -> None:
        with self._lock:
            stream = self._streams.setdefault(
                str(thread_id), deque(maxlen=self._maxlen)
            )
            stream.append((int(seq), str(payload)))

    def replay(
        self, thread_id: str, after: int = 0, *, limit: int = 2_000
    ) -> list[tuple[int, str]]:
        with self._lock:
            stream = list(self._streams.get(str(thread_id), ()))
        return [
            (seq, payload)
            for seq, payload in stream
            if seq > after
        ][: max(1, int(limit))]

    def latest_seq(self, thread_id: str) -> int:
        with self._lock:
            stream = self._streams.get(str(thread_id))
            return int(stream[-1][0]) if stream else 0

    def close(self) -> None:
        with self._lock:
            self._streams.clear()


class RedisStreamEventTransport:
    """Redis Streams 适配器；客户端可注入以便单测。"""

    def __init__(
        self,
        url: str = "",
        *,
        client: object | None = None,
        prefix: str = "coding-agent:events",
        maxlen: int = DEFAULT_MAXLEN,
        ttl_s: int = 3600,
    ) -> None:
        if client is None:
            try:
                import redis
            except ImportError as exc:  # pragma: no cover - 取决于可选依赖
                raise RuntimeError(
                    '未安装 redis。请运行：pip install -e ".[events]"'
                ) from exc
            client = redis.Redis.from_url(url, decode_responses=True)
        self._client = client
        self.prefix = prefix.rstrip(":")
        self.maxlen = max(1, int(maxlen))
        self.ttl_s = max(0, int(ttl_s))

    def _key(self, thread_id: str) -> str:
        safe = "".join(
            ch for ch in str(thread_id) if ch.isalnum() or ch in "_:-"
        )[:128] or "unknown"
        return f"{self.prefix}:{safe}"

    def publish(self, thread_id: str, seq: int, payload: str) -> None:
        key = self._key(thread_id)
        self._client.xadd(
            key,
            {"seq": int(seq), "data": str(payload)},
            maxlen=self.maxlen,
            approximate=True,
        )
        if self.ttl_s:
            self._client.expire(key, self.ttl_s)

    def replay(
        self, thread_id: str, after: int = 0, *, limit: int = 2_000
    ) -> list[tuple[int, str]]:
        rows = self._client.xrange(self._key(thread_id), min="-", max="+")
        found: list[tuple[int, str]] = []
        for _stream_id, fields in rows:
            try:
                seq = int(fields.get("seq", 0))
            except (TypeError, ValueError):
                continue
            if seq > after:
                found.append((seq, str(fields.get("data", ""))))
        found.sort(key=lambda item: item[0])
        return found[: max(1, int(limit))]

    def latest_seq(self, thread_id: str) -> int:
        rows = self._client.xrevrange(self._key(thread_id), max="+", min="-", count=1)
        if not rows:
            return 0
        try:
            return int(rows[0][1].get("seq", 0))
        except (TypeError, ValueError, IndexError):
            return 0

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def build_event_transport(settings) -> EventTransport:
    """按配置构建传输；默认内存，redis 需要显式 URL。"""
    choice = str(getattr(settings, "event_transport", "memory") or "memory")
    choice = choice.strip().lower()
    if choice == "memory":
        return MemoryEventTransport(
            maxlen=int(getattr(settings, "event_transport_maxlen", DEFAULT_MAXLEN))
        )
    if choice == "redis":
        url = str(getattr(settings, "event_transport_url", "") or "").strip()
        if not url:
            raise ValueError(
                "AGENT_EVENT_TRANSPORT=redis 需要 AGENT_EVENT_TRANSPORT_URL"
            )
        return RedisStreamEventTransport(
            url,
            prefix=str(
                getattr(
                    settings,
                    "event_transport_prefix",
                    "coding-agent:events",
                )
            ),
            maxlen=int(getattr(settings, "event_transport_maxlen", DEFAULT_MAXLEN)),
            ttl_s=int(getattr(settings, "event_transport_ttl_s", 3600)),
        )
    raise ValueError(
        f"未知 AGENT_EVENT_TRANSPORT={choice!r}（可选 memory/redis）"
    )
