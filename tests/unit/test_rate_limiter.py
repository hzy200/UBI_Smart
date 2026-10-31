"""固定窗口限流：内存版与 Redis 共享版。"""

from __future__ import annotations

from ubismart.web.middleware import (
    FixedWindowRateLimiter,
    RedisFixedWindowRateLimiter,
)


class _Redis:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.fail = False

    def incr(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("down")
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    def ttl(self, key: str) -> int:
        if self.fail:
            raise ConnectionError("down")
        return self.ttls.get(key, -1)


def test_memory_limiter_window_and_retry() -> None:
    limiter = FixedWindowRateLimiter(per_minute=2)
    assert limiter.allow("a", now=0.0)
    assert limiter.allow("a", now=1.0)
    assert not limiter.allow("a", now=2.0)
    assert limiter.retry_after("a", now=2.0) >= 58
    # 下一个窗口恢复
    assert limiter.allow("a", now=61.0)
    assert limiter.retry_after("missing", now=0.0) == 0


def test_redis_limiter_shared_window() -> None:
    client = _Redis()
    limiter = RedisFixedWindowRateLimiter(
        client, per_minute=2, prefix="p"
    )
    assert limiter.allow("a", now=0.0)
    assert limiter.allow("a", now=1.0)
    assert not limiter.allow("a", now=2.0)
    key = "p:a:0"
    assert client.ttls[key] == 61
    assert limiter.retry_after("a", now=2.0) == 61
    # 下一分钟是新的 Redis key
    assert limiter.allow("a", now=61.0)


def test_redis_limiter_fails_closed() -> None:
    client = _Redis()
    client.fail = True
    limiter = RedisFixedWindowRateLimiter(client, per_minute=10)
    assert limiter.allow("a", now=0.0) is False
    assert limiter.retry_after("a", now=0.0) == 60
