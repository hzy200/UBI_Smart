"""混合检索：BM25 + 向量 RRF，以及失败回退。"""

from __future__ import annotations

from coding_agent.config import Settings
from coding_agent.memory.embeddings import EmbeddingError
from coding_agent.memory.longterm import LayeredMemory, LongTermMemory
from coding_agent.memory.semantic import SemanticMemory
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


class _ControlledEmbedder:
    name = "fake"
    model = "fake-v1"
    dimension = 2

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def embed(self, texts: list[str]) -> list[list[float]]:
        if self.fail:
            raise EmbeddingError("provider down")
        vectors: list[list[float]] = []
        for text in texts:
            if str(text).strip() == "pytest":
                vectors.append([1.0, 0.0])
            elif "pytest" in text:
                vectors.append([0.0, 1.0])
            elif "向量" in text:
                vectors.append([1.0, 0.0])
            else:
                vectors.append([0.0, 1.0])
        return vectors


def _settings() -> Settings:
    return Settings(_env_file=None, memory_vector_top_n=10)


def _memory(embedder: _ControlledEmbedder, *, enabled: bool = True):
    fs = _FakeFs()
    layered = LayeredMemory(LongTermMemory(fs, "/work"))  # type: ignore[arg-type]
    memory = SemanticMemory(
        layered,
        _settings(),
        embedder=embedder if enabled else None,
    )
    return memory, fs


def test_hybrid_search_includes_vector_only_fact() -> None:
    memory, _ = _memory(_ControlledEmbedder())
    memory.add("pytest 测试")
    memory.add("向量命中")

    lexical_only = memory.layered.search("pytest", 2)
    assert lexical_only == ["pytest 测试"]

    hits = memory.search("pytest", 2)
    assert set(hits) == {"pytest 测试", "向量命中"}


def test_embedding_failure_falls_back_to_bm25() -> None:
    memory, _ = _memory(_ControlledEmbedder(fail=True))
    memory.add("pytest 测试")
    memory.add("向量命中")

    assert memory.search("pytest", 2) == ["pytest 测试"]
    assert memory.last_error


def test_disabled_vector_is_pure_bm25() -> None:
    memory, _ = _memory(_ControlledEmbedder(), enabled=False)
    memory.add("pytest 测试")
    memory.add("向量命中")

    assert memory.search("pytest", 2) == ["pytest 测试"]


def test_add_indexes_and_remove_prunes_vectors() -> None:
    memory, _ = _memory(_ControlledEmbedder())
    memory.add("pytest 测试")
    memory.add("向量命中")

    status = memory.status(scope="project")
    project = status["scopes"]["project"]
    assert project["facts"] == 2
    assert project["vectors"] == 2

    memory.remove(1, scope="project")
    status = memory.status(scope="project")
    assert status["scopes"]["project"]["facts"] == 1
    assert status["scopes"]["project"]["vectors"] == 1


def test_remote_query_with_secret_is_not_sent() -> None:
    class _RemoteRecorder(_ControlledEmbedder):
        name = "remote"

        def __init__(self) -> None:
            super().__init__()
            self.calls: list[list[str]] = []

        def embed(self, texts: list[str]) -> list[list[float]]:
            self.calls.append(list(texts))
            return super().embed(texts)

    memory, _ = _memory(_ControlledEmbedder())
    memory.add("pytest 测试")
    remote = _RemoteRecorder()
    memory.embedder = remote

    memory.search("api_key=sk-abcdefghijklmnopqrstuvwxyz", 2)

    assert remote.calls == []
    assert "密钥" in memory.last_error


def test_reindex_force_rebuilds_vectors() -> None:
    memory, _ = _memory(_ControlledEmbedder())
    memory.add("pytest 测试")

    result = memory.reindex(scope="project", force=True)

    assert result["project"]["indexed"] == 1
    assert result["project"]["facts"] == 1
