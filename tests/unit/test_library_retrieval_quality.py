"""检索质量：BM25、RRF、MMR、标签/分页/详情与小型评测集。"""

from __future__ import annotations

import json
from pathlib import Path

from tests.unit.test_library_store import MemoryFs

from ubismart.library.rerank import rerank_hits
from ubismart.library.search import (
    bm25_scores,
    mmr_select,
    rrf_scores,
    token_counts,
    tokens,
)
from ubismart.library.store import LibraryStore


def test_bm25_prefers_matching_terms_and_term_frequency() -> None:
    query = tokens("alpha")
    docs = [
        token_counts("alpha alpha alpha"),
        token_counts("alpha beta gamma delta epsilon"),
        token_counts("unrelated words"),
    ]
    scores = bm25_scores(query, docs)
    assert scores[0] > scores[1] > scores[2]
    assert scores[2] == 0.0


def test_rrf_fuses_rankings_without_score_normalization() -> None:
    fused = rrf_scores([[0, 1], [1, 0]], 2)
    assert fused[0] == fused[1]
    only_first = rrf_scores([[0], [0]], 2)
    assert only_first[0] > only_first[1] == 0.0


def test_mmr_diversifies_near_duplicate_hits() -> None:
    hits = [
        {"text": "alpha beta gamma", "_relevance": 1.0},
        {"text": "alpha beta gamma delta", "_relevance": 0.95},
        {"text": "unrelated database", "_relevance": 0.6},
    ]
    selected = mmr_select(hits, top_k=2, lambda_=0.5)
    assert selected[0] is hits[0]
    assert selected[1] is hits[2]


def test_rerank_prefers_exact_phrase_in_name() -> None:
    hits = [
        {"text": "general text", "name": "other.md", "_relevance": 1.0},
        {"text": "general text", "name": "HTTPS 规范.md", "_relevance": 1.0},
    ]
    rerank_hits(hits, "https", boost=0.3)
    hits.sort(key=lambda hit: -hit["_relevance"])
    assert hits[0]["name"] == "HTTPS 规范.md"


def test_tags_pagination_and_detail() -> None:
    store = LibraryStore(MemoryFs("/lib"), "/lib")
    first = store.ingest("a.md", b"# A\n\nalpha")
    second = store.ingest("b.md", b"# B\n\nbeta")
    store.set_tags(first["item_id"], ["规范", "网络"])

    tagged = store.list_page(tag="规范")
    assert tagged["total"] == 1
    assert tagged["items"][0]["item_id"] == first["item_id"]

    page = store.list_page(limit=1, offset=0)
    assert page["total"] == 2
    assert len(page["items"]) == 1
    page2 = store.list_page(limit=1, offset=1)
    assert page2["items"][0]["item_id"] != page["items"][0]["item_id"]

    detail = store.get_detail(second["item_id"], chunk_limit=1)
    assert detail is not None
    assert detail["chunk_total"] >= 1
    assert len(detail["chunks"]) == 1
    assert detail["outline"]


def test_library_query_eval_set_recall() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "tests"
        / "eval"
        / "library_queries.json"
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    store = LibraryStore(MemoryFs("/lib"), "/lib")
    for doc in data["documents"]:
        store.ingest(doc["name"], doc["text"].encode("utf-8"))

    hits = 0
    for case in data["queries"]:
        results = store.search(case["query"], top_k=3, mode="lexical")
        names = {str(item.get("name", "")) for item in results}
        if case["expected"] in names:
            hits += 1
    recall = hits / max(1, len(data["queries"]))
    assert recall >= 0.75
