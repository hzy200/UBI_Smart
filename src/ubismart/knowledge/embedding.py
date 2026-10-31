"""知识产物的向量检索适配。

复用 `memory/embeddings.py` 的 provider（hash/local/remote），不新建一套
embedding 客户端。默认 `lexical`，只有显式配置 provider 且
`AGENT_KNOWLEDGE_SEARCH_MODE` 为 vector/hybrid 时才加载模型或发起远程请求。
远程 provider 继续受 `AGENT_MEMORY_ALLOW_REMOTE_EMBEDDINGS` 与 base_url/key
双重门禁。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from ubismart.memory.embeddings import build_embedder

_CACHE: dict[tuple[str, str], list[float]] = {}
_MAX_CACHE = 4_096


def _key(provider: Any, text: str) -> tuple[str, str]:
    name = str(getattr(provider, "model", type(provider).__name__))
    return name, hashlib.sha1(text.encode("utf-8")).hexdigest()


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(a * a for a in left))
    norm_right = math.sqrt(sum(b * b for b in right))
    if norm_left <= 0 or norm_right <= 0:
        return 0.0
    return dot / (norm_left * norm_right)


def make_knowledge_embedder(settings: Any) -> tuple[Any | None, str]:
    """按知识检索配置构造 provider；失败返回 `(None, error)`。"""
    mode = str(
        getattr(settings, "knowledge_search_mode", "lexical") or "lexical"
    ).strip().lower()
    if mode not in {"vector", "hybrid"}:
        return None, ""
    provider = str(
        getattr(settings, "knowledge_embedding_provider", "") or ""
    ).strip().lower()
    if not provider:
        provider = str(
            getattr(settings, "memory_embedding_provider", "local")
            or "local"
        ).strip().lower()
    if provider in {"", "none", "off", "disabled"}:
        return None, ""
    overrides: dict[str, Any] = {
        "memory_vector_enabled": True,
        "memory_embedding_provider": provider,
    }
    dimension = int(getattr(settings, "knowledge_embedding_dim", 0) or 0)
    if dimension > 0:
        overrides["memory_embedding_dim"] = dimension
    try:
        cloned = settings.model_copy(update=overrides)
    except AttributeError:
        return None, "settings 不支持 model_copy"
    return build_embedder(cloned)


def embed_texts(
    provider: Any, texts: list[str]
) -> list[list[float]] | None:
    """批量 embedding，带进程内缓存；失败返回 None（调用方回退词法）。"""
    if provider is None:
        return None
    missing: list[str] = []
    seen: set[str] = set()
    for text in texts:
        if _key(provider, text) in _CACHE or text in seen:
            continue
        seen.add(text)
        missing.append(text)
    if missing:
        try:
            vectors = provider.embed(missing)
        except Exception:  # noqa: BLE001 - embedding 是增强，不是前提
            return None
        if len(vectors) != len(missing):
            return None
        for text, vector in zip(missing, vectors, strict=True):
            if len(_CACHE) >= _MAX_CACHE:
                _CACHE.clear()
            _CACHE[_key(provider, text)] = list(vector)
    return [_CACHE[_key(provider, text)] for text in texts]
