"""共享缓存锁：互斥、超时与 stale 接管。"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from ubismart.knowledge.cache_lock import CacheLock, CacheLockTimeout
from ubismart.knowledge.index_cache import IndexCache


def test_lock_is_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "a.lock"
    with CacheLock(path, timeout=0.1):
        with pytest.raises(CacheLockTimeout):
            with CacheLock(path, timeout=0.05):
                pass
    assert not path.exists()


def test_stale_lock_is_reclaimed(tmp_path: Path) -> None:
    path = tmp_path / "a.lock"
    path.write_text('{"token":"old"}', encoding="utf-8")
    old = time.time() - 120
    os.utime(path, (old, old))

    with CacheLock(path, timeout=0.1, stale_after=30):
        assert path.exists()

    assert not path.exists()


def test_put_file_returns_false_when_lock_unavailable(
    tmp_path: Path,
) -> None:
    cache = IndexCache(tmp_path / "cache", lock_timeout=0.05)
    cache.files_dir.mkdir(parents=True, exist_ok=True)

    with CacheLock(
        cache._lock_path("digest"),
        timeout=0.1,
        stale_after=30,
    ):
        assert cache.put_file("digest", {"symbols": []}) is False

    assert cache.get_file("digest") is None


def test_put_file_rechecks_after_acquiring_lock(tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "cache")
    assert cache.put_file("digest", {"symbols": ["def a"]})
    # 同一内容重复写：拿到锁后重查，不会覆盖已有条目。
    assert cache.put_file("digest", {"symbols": ["def b"]})
    assert cache.get_file("digest")["symbols"] == ["def a"]
