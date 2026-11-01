"""Python 索引的增量缓存（内容寻址，可跨机器共享）。

缓存键是**文件内容哈希**，不是路径：同一个文件在不同 clone/机器上只要内容
一致就命中；文件改名但内容不变也能复用。默认目录是宿主的
`~/.codingagent/index-cache`，可通过 `AGENT_ARCH_INDEX_CACHE_DIR` 指向共享盘。

缓存只存 imports/symbols/dynamic/unparsed/test，不存源码正文。
按条目数/字节数/TTL 做 best-effort 淘汰；并发写/删只可能造成重新解析。
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from ubismart.codeintel.python_index import (
    PythonIndex,
    build_index_from_entries,
    parse_python_source,
)
from ubismart.knowledge.cache_lock import CacheLock, CacheLockTimeout

DEFAULT_CACHE_DIR = Path.home() / ".codingagent" / "index-cache"
_VERSION = 1
DEFAULT_MAX_ENTRIES = 20_000
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_TTL_DAYS = 30


def _collect_records(
    files_dir: Path,
) -> list[tuple[Path, float, int]] | None:
    try:
        paths = list(files_dir.glob("*.json"))
    except OSError:
        return None
    records: list[tuple[Path, float, int]] = []
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        records.append((path, stat.st_mtime, stat.st_size))
    return records


def _remove_expired(
    records: list[tuple[Path, float, int]],
    *,
    now: float,
    ttl_seconds: int,
) -> tuple[int, list[tuple[Path, float, int]]]:
    removed = 0
    kept: list[tuple[Path, float, int]] = []
    for path, mtime, size in records:
        if ttl_seconds > 0 and now - mtime > ttl_seconds:
            try:
                path.unlink()
                removed += 1
            except OSError:
                kept.append((path, mtime, size))
        else:
            kept.append((path, mtime, size))
    return removed, kept


def _trim_entries(
    kept: list[tuple[Path, float, int]], max_entries: int
) -> tuple[int, list[tuple[Path, float, int]]]:
    kept = sorted(kept, key=lambda item: item[1])
    removed = 0
    if max_entries > 0 and len(kept) > max_entries:
        drop = len(kept) - max_entries
        for path, _mtime, _size in kept[:drop]:
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        kept = kept[drop:]
    return removed, kept


def _trim_bytes(
    kept: list[tuple[Path, float, int]], max_bytes: int
) -> tuple[int, int]:
    removed = 0
    total = sum(size for _path, _mtime, size in kept)
    if max_bytes > 0 and total > max_bytes:
        while kept and total > max_bytes:
            path, _mtime, size = kept.pop(0)
            try:
                path.unlink()
                removed += 1
                total -= size
            except OSError:
                pass
    return removed, total


class IndexCache:
    def __init__(
        self,
        directory: str | Path | None = None,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        max_bytes: int = DEFAULT_MAX_BYTES,
        ttl_days: int = DEFAULT_TTL_DAYS,
        lock_timeout: float = 5.0,
        lock_stale_after: float = 60.0,
    ) -> None:
        base = (
            Path(directory).expanduser()
            if directory
            else DEFAULT_CACHE_DIR
        )
        self.directory = base
        self.files_dir = base / "files"
        self.max_entries = max(0, int(max_entries))
        self.max_bytes = max(0, int(max_bytes))
        self.ttl_days = max(0, int(ttl_days))
        self.lock_timeout = max(0.0, float(lock_timeout))
        self.lock_stale_after = max(1.0, float(lock_stale_after))
        self._writes = 0

    @classmethod
    def from_settings(cls, settings: Any, root: str = "") -> IndexCache:
        raw = str(
            getattr(settings, "arch_index_cache_dir", "") or ""
        ).strip()
        return cls(
            raw or None,
            max_entries=int(
                getattr(
                    settings, "arch_index_cache_max_entries", DEFAULT_MAX_ENTRIES
                )
                or DEFAULT_MAX_ENTRIES
            ),
            max_bytes=int(
                getattr(
                    settings, "arch_index_cache_max_bytes", DEFAULT_MAX_BYTES
                )
                or DEFAULT_MAX_BYTES
            ),
            ttl_days=int(
                getattr(
                    settings, "arch_index_cache_ttl_days", DEFAULT_TTL_DAYS
                )
                or 0
            ),
            lock_timeout=float(
                getattr(settings, "arch_index_cache_lock_timeout_s", 5.0)
                or 5.0
            ),
            lock_stale_after=float(
                getattr(settings, "arch_index_cache_lock_stale_s", 60.0)
                or 60.0
            ),
        )

    def _path(self, content_hash: str) -> Path:
        safe = "".join(
            char
            for char in str(content_hash)
            if char.isalnum() or char in "._-"
        )[:80]
        return self.files_dir / f"{safe}.json"

    def get_file(self, content_hash: str) -> dict[str, Any] | None:
        path = self._path(content_hash)
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            payload = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        if int(payload.get("version", -1) or -1) != _VERSION:
            return None
        if str(payload.get("hash", "")) != str(content_hash):
            return None
        entry = payload.get("entry")
        return entry if isinstance(entry, dict) else None

    def _lock_path(self, content_hash: str) -> Path:
        return self._path(content_hash).with_suffix(".lock")

    def put_file(
        self, content_hash: str, entry: dict[str, Any]
    ) -> bool:
        try:
            with CacheLock(
                self._lock_path(content_hash),
                timeout=self.lock_timeout,
                stale_after=self.lock_stale_after,
            ):
                # 拿到锁后重查：别的进程可能已经写入了同一内容。
                if self.get_file(content_hash) is not None:
                    return True
                if not self._write_unlocked(content_hash, entry):
                    return False
        except CacheLockTimeout:
            return False
        self._writes += 1
        if self._writes % 100 == 0:
            self.prune()
        return True

    def _write_unlocked(
        self, content_hash: str, entry: dict[str, Any]
    ) -> bool:
        path = self._path(content_hash)
        payload = {
            "version": _VERSION,
            "hash": str(content_hash),
            "entry": entry,
        }
        temp: Path | None = None
        try:
            self.files_dir.mkdir(parents=True, exist_ok=True)
            try:
                os.chmod(self.directory, 0o700)
            except OSError:
                pass
            temp = path.with_suffix(
                f".{os.getpid()}.{os.urandom(4).hex()}.tmp"
            )
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, default=str),
                encoding="utf-8",
            )
            os.replace(temp, path)
        except OSError:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
            return False
        return True

    def prune(self) -> dict[str, int]:
        """按 TTL / 条目数 / 字节数淘汰；返回统计。"""
        try:
            with CacheLock(
                self.files_dir / ".prune.lock",
                timeout=min(0.5, self.lock_timeout),
                stale_after=self.lock_stale_after,
            ):
                return self._prune_unlocked()
        except CacheLockTimeout:
            return {"removed": 0, "kept": 0, "bytes": 0}

    def _prune_unlocked(self) -> dict[str, int]:
        now = time.time()
        records = _collect_records(self.files_dir)
        if records is None:
            return {"removed": 0, "kept": 0, "bytes": 0}

        removed, kept = _remove_expired(
            records,
            now=now,
            ttl_seconds=self.ttl_days * 86_400,
        )
        trimmed_entries, kept = _trim_entries(kept, self.max_entries)
        removed += trimmed_entries
        trimmed_bytes, total = _trim_bytes(kept, self.max_bytes)
        removed += trimmed_bytes
        return {
            "removed": removed,
            "kept": len(kept),
            "bytes": max(0, total),
        }


def build_index_incremental(
    sources: dict[str, str], cache: IndexCache
) -> tuple[PythonIndex, dict[str, Any]]:
    """只解析缓存未命中的内容；返回 (索引, 统计)。"""
    entries: list[dict[str, object]] = []
    changed = 0
    reused = 0
    for path, source in sources.items():
        normalized = str(path).replace("\\", "/").lstrip("./")
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        cached = cache.get_file(digest)
        if isinstance(cached, dict):
            entry = dict(cached)
            entry["path"] = normalized
            reused += 1
        else:
            entry = dict(parse_python_source(normalized, source))
            entry["hash"] = digest
            cache.put_file(
                digest,
                {key: value for key, value in entry.items() if key != "path"},
            )
            changed += 1
        entries.append(entry)
    index = build_index_from_entries(entries)
    return index, {
        "total": len(sources),
        "changed": changed,
        "reused": reused,
        "cache_hit": reused > 0,
        "cache_dir": str(cache.directory),
    }
