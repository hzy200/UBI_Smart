"""内容寻址的增量索引缓存：只重解析变化内容，可跨 clone/机器复用。"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

from ubismart.config import Settings
from ubismart.knowledge.index_cache import (
    IndexCache,
    build_index_incremental,
)


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_only_changed_files_are_reparsed(tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "cache")
    sources = {
        "pkg/a.py": "def a():\n    return 1\n",
        "pkg/b.py": "def b():\n    return 2\n",
    }

    first, first_stats = build_index_incremental(sources, cache)
    assert first_stats["changed"] == 2
    assert first_stats["reused"] == 0
    assert first.symbols["pkg/a.py"] == ["def a"]

    second, second_stats = build_index_incremental(sources, cache)
    assert second_stats["changed"] == 0
    assert second_stats["reused"] == 2
    assert second.symbols["pkg/b.py"] == ["def b"]

    changed = dict(sources)
    changed["pkg/b.py"] = "def b2():\n    return 3\n"
    third, third_stats = build_index_incremental(changed, cache)
    assert third_stats["changed"] == 1
    assert third_stats["reused"] == 1
    assert third.symbols["pkg/b.py"] == ["def b2"]


def test_content_is_reused_after_rename(tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "cache")
    source = "def moved():\n    return 1\n"

    build_index_incremental({"pkg/a.py": source}, cache)
    index, stats = build_index_incremental({"pkg/renamed.py": source}, cache)

    assert stats["changed"] == 0
    assert stats["reused"] == 1
    assert index.symbols["pkg/renamed.py"] == ["def moved"]


def test_cache_dir_can_be_configured(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        arch_index_cache_dir=str(tmp_path / "shared"),
    )
    cache = IndexCache.from_settings(settings, "/ws")

    assert cache.directory == tmp_path / "shared"


def test_prune_removes_oldest_entries(tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "cache", max_entries=2)
    now = time.time()
    for index in range(3):
        digest = _hash(f"source-{index}")
        assert cache.put_file(digest, {"symbols": []})
        os.utime(cache._path(digest), (now + index, now + index))

    stats = cache.prune()

    assert stats["removed"] == 1
    assert len(list(cache.files_dir.glob("*.json"))) == 2
    assert not cache._path(_hash("source-0")).exists()


def test_prune_removes_expired_entries(tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "cache", ttl_days=1)
    digest = _hash("old")
    cache.put_file(digest, {"symbols": []})
    old = time.time() - 2 * 86_400
    os.utime(cache._path(digest), (old, old))

    stats = cache.prune()

    assert stats["removed"] == 1
    assert stats["kept"] == 0


def test_prune_respects_byte_budget(tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "cache", max_bytes=1)
    cache.put_file(_hash("a"), {"symbols": ["def a"]})

    stats = cache.prune()

    assert stats["removed"] == 1
    assert stats["bytes"] == 0


def test_settings_control_cache_limits(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        arch_index_cache_dir=str(tmp_path / "shared"),
        arch_index_cache_max_entries=7,
        arch_index_cache_max_bytes=1234,
        arch_index_cache_ttl_days=3,
    )
    cache = IndexCache.from_settings(settings)

    assert cache.max_entries == 7
    assert cache.max_bytes == 1234
    assert cache.ttl_days == 3


def test_version_mismatch_forces_reparse(tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "cache")
    source = "def a():\n    pass\n"
    digest = _hash(source)
    assert cache.put_file(digest, {"symbols": ["def stale"]})
    path = cache._path(digest)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["version"] = 0
    path.write_text(json.dumps(payload), encoding="utf-8")

    assert cache.get_file(digest) is None

    index, stats = build_index_incremental({"a.py": source}, cache)

    assert stats["changed"] == 1
    assert index.symbols["a.py"] == ["def a"]


def test_corrupt_cache_entry_falls_back(tmp_path: Path) -> None:
    cache = IndexCache(tmp_path / "cache")
    source = "def a():\n    pass\n"
    digest = _hash(source)
    assert cache.put_file(digest, {"symbols": ["def a"]})
    cache._path(digest).write_text("{not json", encoding="utf-8")

    index, stats = build_index_incremental({"a.py": source}, cache)

    assert stats["changed"] == 1
    assert index.symbols["a.py"] == ["def a"]
