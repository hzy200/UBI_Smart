"""记忆向量 sidecar：内容哈希索引、余弦相似度与 RRF 融合。

不引入向量数据库：记忆上限是每个作用域 50 条，内存内线性扫描足够。
向量文件与 memory.md 同目录，使用同样的沙箱路径校验与原子写入。
"""

from __future__ import annotations

import hashlib
import json
import math
import posixpath
from collections.abc import Iterable
from typing import Any

from ubismart.sandbox.fs import SandboxFs, SandboxFsError
from ubismart.sandbox.pathguard import SandboxPathError
from ubismart.sandbox.snapshots import AGENT_STATE_DIRNAME

VECTOR_RELPATH = f"{AGENT_STATE_DIRNAME}/memory.vectors.json"
VECTOR_VERSION = 1
RRF_K = 60


def content_hash(fact: str) -> str:
    normalized = " ".join(str(fact).split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def rrf_fuse(
    lexical: Iterable[str],
    vector: Iterable[str],
    *,
    limit: int,
    k: int = RRF_K,
) -> list[str]:
    """Reciprocal Rank Fusion；同分保持首次出现顺序。"""
    scores: dict[str, float] = {}
    order: dict[str, int] = {}
    for ranking in (list(lexical), list(vector)):
        for rank, fact in enumerate(ranking, start=1):
            scores[fact] = scores.get(fact, 0.0) + 1.0 / (k + rank)
            order.setdefault(fact, len(order))
    ranked = sorted(scores, key=lambda fact: (-scores[fact], order[fact]))
    return ranked[: max(0, int(limit))]


class VectorStore:
    def __init__(
        self,
        fs: SandboxFs,
        state_root: str,
        *,
        provider: str,
        model: str,
        dimension: int,
    ) -> None:
        self._fs = fs
        self.state_root = state_root
        self.provider = provider
        self.model = model
        self.dimension = max(1, int(dimension))

    @property
    def path(self) -> str:
        return posixpath.join(self.state_root, VECTOR_RELPATH)

    def _header_matches(self, raw: dict[str, Any]) -> bool:
        return (
            int(raw.get("version", 0) or 0) == VECTOR_VERSION
            and str(raw.get("provider", "")) == self.provider
            and str(raw.get("model", "")) == self.model
            and int(raw.get("dimension", 0) or 0) == self.dimension
        )

    def _load(self) -> tuple[dict[str, list[float]], bool]:
        try:
            raw = self._fs.read_text(self.path, max_bytes=100_000_000)
        except (SandboxFsError, SandboxPathError):
            return {}, True
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return {}, False
        if not isinstance(parsed, dict) or not self._header_matches(parsed):
            return {}, False
        facts = parsed.get("facts")
        if not isinstance(facts, dict):
            return {}, False
        vectors: dict[str, list[float]] = {}
        for key, value in facts.items():
            vector = value.get("vector") if isinstance(value, dict) else None
            if (
                isinstance(vector, list)
                and len(vector) == self.dimension
                and all(isinstance(item, (int, float)) for item in vector)
            ):
                vectors[str(key)] = [float(item) for item in vector]
        return vectors, True

    def _save(self, vectors: dict[str, list[float]]) -> None:
        payload = {
            "version": VECTOR_VERSION,
            "provider": self.provider,
            "model": self.model,
            "dimension": self.dimension,
            "facts": {
                key: {"vector": vector}
                for key, vector in vectors.items()
            },
        }
        self._fs.write_text(
            self.path,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        )

    def all(self, facts: Iterable[str]) -> dict[str, list[float]]:
        """按**事实原文**返回向量，方便检索层直接按 fact 取值。"""
        vectors, _ = self._load()
        result: dict[str, list[float]] = {}
        for fact in facts:
            vector = vectors.get(content_hash(fact))
            if vector is not None:
                result[fact] = vector
        return result

    def get(self, fact: str) -> list[float] | None:
        vectors, _ = self._load()
        return vectors.get(content_hash(fact))

    def put(self, fact: str, vector: list[float]) -> None:
        vectors, _ = self._load()
        vectors[content_hash(fact)] = [float(item) for item in vector]
        self._save(vectors)

    def put_many(self, items: dict[str, list[float]]) -> None:
        vectors, _ = self._load()
        for fact, vector in items.items():
            vectors[content_hash(fact)] = [float(item) for item in vector]
        self._save(vectors)

    def prune(self, facts: Iterable[str]) -> int:
        vectors, ok = self._load()
        if not ok:
            return 0
        wanted = {content_hash(fact) for fact in facts}
        stale = [key for key in vectors if key not in wanted]
        if not stale:
            return 0
        for key in stale:
            vectors.pop(key, None)
        self._save(vectors)
        return len(stale)

    def missing(self, facts: Iterable[str]) -> list[str]:
        vectors, _ = self._load()
        return [
            fact
            for fact in facts
            if content_hash(fact) not in vectors
        ]

    def status(self, facts: Iterable[str]) -> dict[str, Any]:
        facts = list(facts)
        vectors, header_ok = self._load()
        wanted = {content_hash(fact) for fact in facts}
        known = sum(1 for key in wanted if key in vectors)
        return {
            "path": self.path,
            "provider": self.provider,
            "model": self.model,
            "dimension": self.dimension,
            "header_ok": header_ok,
            "facts": len(facts),
            "vectors": known,
            "missing": max(0, len(facts) - known),
        }
