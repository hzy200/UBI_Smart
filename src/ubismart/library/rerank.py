"""轻量确定性 rerank：不调用模型，只做字段/短语加权。

BM25/RRF 已经给出基础分；这里补的是“查询短语精确出现在正文/标题/文件名”
这种强信号。默认系数很小，避免压过 BM25 与 MMR。
"""

from __future__ import annotations

from typing import Any

from ubismart.library.search import tokens


def rerank_hits(
    hits: list[dict[str, Any]],
    query: str,
    *,
    boost: float = 0.25,
) -> list[dict[str, Any]]:
    phrase = " ".join(str(query or "").split()).lower()
    if not phrase:
        return hits
    query_tokens = tokens(query)
    boost = max(0.0, min(float(boost), 2.0))
    for hit in hits:
        base = float(hit.get("_relevance", 0.0) or 0.0)
        text = str(hit.get("text", "")).lower()
        name = str(hit.get("name", "")).lower()
        heading = str(hit.get("heading", "")).lower()
        factor = 1.0
        if phrase in text:
            factor += boost
        if phrase in name:
            factor += boost
        if phrase in heading:
            factor += boost * 0.5
        if query_tokens:
            coverage = len(query_tokens & tokens(text)) / len(query_tokens)
            factor += boost * 0.5 * coverage
        hit["_relevance"] = base * factor
    return hits
