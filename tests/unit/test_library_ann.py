"""向量索引后端：linear 与不可用 hnswlib 的降级。"""

from __future__ import annotations

from tests.unit.test_library_store import MemoryFs

from ubismart.library.ann import LinearVectorIndex, build_vector_index
from ubismart.library.store import LibraryStore
from ubismart.memory.embeddings import HashEmbedder


def test_linear_vector_index_returns_best_match() -> None:
    entries = [
        {"vector": [1.0, 0.0]},
        {"vector": [0.0, 1.0]},
    ]
    index = build_vector_index(entries, dimension=2, backend="linear")
    assert isinstance(index, LinearVectorIndex)
    assert index.search([1.0, 0.0], 1)[0][0] == 0


def test_unknown_ann_backend_falls_back_to_linear() -> None:
    entries = [{"vector": [1.0, 0.0]}]
    index = build_vector_index(entries, dimension=2, backend="missing")
    assert isinstance(index, LinearVectorIndex)
    assert index.size == 1


def test_store_vector_search_with_ann_backend() -> None:
    store = LibraryStore(
        MemoryFs("/lib"),
        "/lib",
        search_mode="vector",
        embedder=HashEmbedder(32),
        vector_backend="missing",
    )
    item = store.ingest("a.md", b"# alpha\n\nalpha alpha")
    hits = store.search("alpha", mode="vector")
    assert hits
    assert hits[0]["item_id"] == item["item_id"]
