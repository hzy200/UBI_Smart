"""向量 sidecar、余弦与 RRF 融合。"""

from __future__ import annotations

from coding_agent.memory.vectors import (
    VectorStore,
    content_hash,
    cosine,
    rrf_fuse,
)
from coding_agent.sandbox.fs import SandboxFsError


class _FakeFs:
    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def read_text(self, path: str, *, max_bytes: int) -> str:
        if path not in self.files:
            raise SandboxFsError("not found")
        return self.files[path]

    def write_text(self, path: str, content: str) -> None:
        self.files[path] = content


def _store(fs: _FakeFs, *, model: str = "m1", dimension: int = 3) -> VectorStore:
    return VectorStore(
        fs,  # type: ignore[arg-type]
        "/work",
        provider="fake",
        model=model,
        dimension=dimension,
    )


def test_rrf_fuses_two_rankings_without_score_scales() -> None:
    fused = rrf_fuse(["a", "b"], ["b", "c"], limit=3)

    assert fused == ["b", "a", "c"]


def test_cosine_handles_zero_and_dimension_mismatch() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert cosine([1.0], [1.0, 0.0]) == 0.0
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_vector_store_round_trip_and_prune() -> None:
    fs = _FakeFs()
    store = _store(fs)
    store.put("事实 A", [1.0, 0.0, 0.0])
    store.put("事实 B", [0.0, 1.0, 0.0])

    assert store.get("事实 A") == [1.0, 0.0, 0.0]
    assert store.missing(["事实 A", "事实 B", "事实 C"]) == ["事实 C"]
    assert store.prune(["事实 A"]) == 1
    assert store.get("事实 B") is None
    assert content_hash("事实 A") == content_hash("  事实   A  ")


def test_model_change_invalidates_vectors() -> None:
    fs = _FakeFs()
    _store(fs, model="m1").put("事实 A", [1.0, 0.0, 0.0])

    changed = _store(fs, model="m2")

    assert changed.get("事实 A") is None
    assert changed.status(["事实 A"])["header_ok"] is False
    assert changed.missing(["事实 A"]) == ["事实 A"]


def test_status_counts_vectors() -> None:
    fs = _FakeFs()
    store = _store(fs)
    store.put("A", [1.0, 0.0, 0.0])

    status = store.status(["A", "B"])

    assert status["facts"] == 2
    assert status["vectors"] == 1
    assert status["missing"] == 1
    assert status["header_ok"] is True
