"""资料库向量检索适配。

复用 `memory/embeddings.py` 的 provider（hash/local/remote），不新建密钥体系。
默认 lexical；只有显式配置 `AGENT_LIBRARY_SEARCH_MODE=vector|hybrid` 时才加载
模型或发起远程请求。embedding 失败一律回退词法，不阻断资料检索。
"""

from __future__ import annotations

import hashlib
import math
from typing import Any

from ubismart.memory.embeddings import build_embedder

_CACHE: dict[tuple[str, str], list[float]] = {}
_MAX_CACHE = 4_096


def normalize_mode(value: str) -> str:
    mode = str(value or "lexical").strip().lower()
    return mode if mode in {"lexical", "vector", "hybrid"} else "lexical"


def make_library_embedder(settings: Any) -> tuple[Any | None, str]:
    """按资料库配置构造 provider；失败返回 `(None, error)`。"""
    mode = normalize_mode(
        str(getattr(settings, "library_search_mode", "lexical") or "lexical")
    )
    if mode not in {"vector", "hybrid"}:
        return None, ""
    provider = str(
        getattr(settings, "library_embedding_provider", "") or ""
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
    dimension = int(getattr(settings, "library_embedding_dim", 0) or 0)
    if dimension > 0:
        overrides["memory_embedding_dim"] = dimension
    try:
        cloned = settings.model_copy(update=overrides)
    except AttributeError:
        return None, "settings 不支持 model_copy"
    return build_embedder(cloned)


def _key(provider: Any, text: str) -> tuple[str, str]:
    name = ":".join(
        str(part)
        for part in (
            getattr(provider, "name", type(provider).__name__),
            getattr(provider, "model", ""),
            getattr(provider, "dimension", 0),
        )
    )
    return name, hashlib.sha1(text.encode("utf-8")).hexdigest()


def embed_texts(
    provider: Any, texts: list[str]
) -> list[list[float]] | None:
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
            _CACHE[_key(provider, text)] = [float(value) for value in vector]
    return [_CACHE[_key(provider, text)] for text in texts]


def cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    norm_left = math.sqrt(sum(value * value for value in left))
    norm_right = math.sqrt(sum(value * value for value in right))
    if norm_left <= 0 or norm_right <= 0:
        return 0.0
    return dot / (norm_left * norm_right)


def provider_header(provider: Any) -> dict[str, Any]:
    return {
        "provider": str(getattr(provider, "name", type(provider).__name__)),
        "model": str(getattr(provider, "model", "")),
        "dimension": int(getattr(provider, "dimension", 0) or 0),
    }
