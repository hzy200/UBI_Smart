"""知识产物存储与确定性检索。"""

from __future__ import annotations

from typing import Any

import pytest

from ubismart.knowledge import store as store_module
from ubismart.knowledge.store import KnowledgeStore
from ubismart.sandbox.fs import SandboxFsError


class _FakeFs:
    def __init__(self, sandbox: Any, root: str) -> None:
        self.root = root
        self.files: dict[str, str] = {}

    def write_text(self, path: str, content: str) -> int:
        self.files[path] = content
        return len(content)

    def read_text(self, path: str, *, max_bytes: int) -> str:
        if path not in self.files:
            raise SandboxFsError("missing")
        return self.files[path]

    def list_dir(
        self, path: str, *, max_depth: int = 1, limit: int = 200
    ) -> list[tuple[str, str]]:
        prefix = path.rstrip("/") + "/"
        names = [
            key[len(prefix) :]
            for key in self.files
            if key.startswith(prefix) and "/" not in key[len(prefix) :]
        ]
        return [("f", name) for name in sorted(names)]


def _store(monkeypatch: pytest.MonkeyPatch) -> KnowledgeStore:
    fake = _FakeFs(object(), "/ws")

    def _factory(sandbox: Any, root: str) -> _FakeFs:
        fake.root = root
        return fake

    monkeypatch.setattr(store_module, "SandboxFs", _factory)
    store = KnowledgeStore(object(), "/ws")  # type: ignore[arg-type]
    store._fake = fake  # type: ignore[attr-defined]
    return store


def _seed(store: KnowledgeStore) -> None:
    assert store.save_doc_index(
        "run-1",
        "doc-1",
        documents=[{"path": "a.pdf", "pages": 3, "status": "complete"}],
        chunks=[
            {
                "chunk_id": "c1",
                "path": "a.pdf",
                "anchor": "page=1",
                "text": "折扣逻辑位于 pricing.py",
            },
            {
                "chunk_id": "c2",
                "path": "a.pdf",
                "anchor": "page=2",
                "text": "报表导出使用 revenue_cents",
            },
        ],
        summary="折扣与报表",
    )
    assert store.save_claims(
        "run-1",
        "doc-1",
        [
            {
                "claim": "折扣在 pricing 模块",
                "evidence_path": "a.pdf",
                "anchor": "page=1",
                "quote": "折扣逻辑位于 pricing.py",
            }
        ],
    )


def test_save_search_and_read(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(monkeypatch)
    _seed(store)

    results = store.search("run-1", "折扣", top_k=4)
    assert results
    assert {item["kind"] for item in results} >= {"doc", "claims"}
    assert any("pricing.py" in item["text"] for item in results)

    payload = store.read("run-1", "doc", "doc-1", selector="折扣")
    assert payload["total"] == 1
    assert payload["items"][0]["anchor"] == "page=1"


def test_artifact_ids_cannot_escape_the_run_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(monkeypatch)
    _seed(store)

    assert store.load("run-1", "doc", "../../etc/passwd") is None
    rel = store.save("run-1", "doc", "../../etc/passwd", {"x": 1})
    assert ".." not in rel
    assert rel.startswith(".agent/artifacts/knowledge/")


class _FakeEmbedder:
    model = "fake-vector"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [
            [1.0, 0.0] if "折扣" in text else [0.0, 1.0]
            for text in texts
        ]


def test_vector_search_uses_the_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(monkeypatch)
    _seed(store)

    results = store.search(
        "run-1",
        "折扣",
        top_k=4,
        mode="vector",
        embedder=_FakeEmbedder(),
    )

    assert results
    assert "pricing.py" in results[0]["text"]
    assert results[0]["vector_score"] > 0


def test_lexical_mode_does_not_require_an_embedder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(monkeypatch)
    _seed(store)

    assert store.search("run-1", "折扣", mode="vector") == []


def test_contract_artifact_is_searchable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(monkeypatch)
    store.save(
        "run-1",
        "contract",
        "change",
        {
            "request": "修改折扣接口",
            "interfaces": [{"name": "apply_discount"}],
            "waves": [
                {"stage": 0, "files": ["pkg/pricing.py"]},
                {"stage": 1, "files": ["pkg/api.py"]},
            ],
            "affected_tests": ["tests/test_pricing.py"],
        },
    )

    results = store.search("run-1", "apply_discount", kind="contract")
    assert results
    assert results[0]["kind"] == "contract"

    payload = store.read("run-1", "contract", "change")
    assert payload["items"][0]["request"] == "修改折扣接口"


def test_missing_artifact_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _store(monkeypatch)

    assert store.search("run-x", "anything") == []
    payload = store.read("run-x", "doc", "missing")
    assert payload["items"] == []
