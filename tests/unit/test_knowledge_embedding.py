"""知识检索的 embedding 适配：provider 选择、缓存与相似度。"""

from __future__ import annotations

from ubismart.config import Settings
from ubismart.knowledge.embedding import (
    cosine,
    embed_texts,
    make_knowledge_embedder,
)


def test_lexical_mode_does_not_build_a_provider() -> None:
    settings = Settings(
        _env_file=None,
        knowledge_search_mode="lexical",
        knowledge_embedding_provider="hash",
    )
    provider, error = make_knowledge_embedder(settings)
    assert provider is None
    assert error == ""


def test_hash_provider_is_available_without_network() -> None:
    settings = Settings(
        _env_file=None,
        knowledge_search_mode="vector",
        knowledge_embedding_provider="hash",
        memory_embedding_dim=16,
    )
    provider, error = make_knowledge_embedder(settings)
    assert provider is not None
    assert error == ""


def test_remote_provider_stays_gated() -> None:
    settings = Settings(
        _env_file=None,
        knowledge_search_mode="vector",
        knowledge_embedding_provider="remote",
        memory_allow_remote_embeddings=False,
    )
    provider, error = make_knowledge_embedder(settings)
    assert provider is None
    assert "AGENT_MEMORY_ALLOW_REMOTE_EMBEDDINGS" in error


class _CountingEmbedder:
    model = "counting"

    def __init__(self) -> None:
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        return [[float(len(text)), 1.0] for text in texts]


def test_embed_texts_caches_by_provider_and_text() -> None:
    provider = _CountingEmbedder()

    first = embed_texts(provider, ["alpha", "beta"])
    second = embed_texts(provider, ["alpha", "beta"])

    assert first == second
    assert provider.calls == 1


def test_cosine_handles_zero_vectors() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0


def test_embed_texts_returns_none_on_provider_error() -> None:
    class _Boom:
        model = "boom"

        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("down")

    assert embed_texts(_Boom(), ["x"]) is None


def test_settings_clone_does_not_mutate_original() -> None:
    settings = Settings(
        _env_file=None,
        knowledge_search_mode="vector",
        knowledge_embedding_provider="hash",
    )
    make_knowledge_embedder(settings)
    assert settings.memory_vector_enabled is False
