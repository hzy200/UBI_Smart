"""向量 provider 与配置构造。"""

from __future__ import annotations

import pytest

from ubismart.config import Settings
from ubismart.memory import embeddings as embedding_module
from ubismart.memory.embeddings import (
    EmbeddingError,
    HashEmbedder,
    RemoteOpenAIEmbeddingProvider,
    build_embedder,
    embedding_status,
)


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_hash_embedder_is_deterministic_and_normalized() -> None:
    embedder = HashEmbedder(64)
    first = embedder.embed(["这个仓库用 pytest", "依赖用 uv"])
    second = embedder.embed(["这个仓库用 pytest", "依赖用 uv"])

    assert first == second
    assert len(first) == 2
    assert all(len(vector) == 64 for vector in first)
    assert all(
        abs(sum(value * value for value in vector) - 1.0) < 1e-6
        for vector in first
    )


def test_vector_disabled_by_default() -> None:
    provider, error = build_embedder(_settings())

    assert provider is None
    assert error == ""
    assert embedding_status(_settings()) == (True, "未启用")


def test_hash_provider_can_be_enabled_explicitly() -> None:
    settings = _settings(
        memory_vector_enabled=True,
        memory_embedding_provider="hash",
        memory_embedding_dim=32,
    )

    provider, error = build_embedder(settings)

    assert error == ""
    assert provider is not None
    assert provider.name == "hash"
    assert provider.dimension == 32


def test_unknown_provider_reports_an_error() -> None:
    settings = _settings(
        memory_vector_enabled=True,
        memory_embedding_provider="does-not-exist",
    )

    provider, error = build_embedder(settings)

    assert provider is None
    assert "未知" in error


class _Response:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _remote_settings(**overrides) -> Settings:
    values = {
        "memory_vector_enabled": True,
        "memory_embedding_provider": "remote",
        "memory_embedding_model": "remote-model",
        "memory_embedding_dim": 3,
        "memory_embedding_base_url": "https://example.test/v1",
        "memory_embedding_api_key": "secret-token",
        "memory_allow_remote_embeddings": True,
        "memory_embedding_batch_size": 2,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_remote_provider_posts_and_orders_by_index(monkeypatch) -> None:
    calls = []

    def fake_post(url, payload, headers, timeout):
        calls.append((url, payload, headers, timeout))
        return _Response(
            payload={
                "data": [
                    {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                    {"index": 0, "embedding": [1.0, 0.0, 0.0]},
                ]
            }
        )

    monkeypatch.setattr(embedding_module, "_post_json", fake_post)
    provider, error = build_embedder(_remote_settings())

    assert error == ""
    assert isinstance(provider, RemoteOpenAIEmbeddingProvider)
    vectors = provider.embed(["A", "B"])

    assert vectors == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert calls[0][0] == "https://example.test/v1/embeddings"
    assert calls[0][1] == {"model": "remote-model", "input": ["A", "B"]}
    assert calls[0][2]["Authorization"] == "Bearer secret-token"


def test_remote_provider_batches_inputs(monkeypatch) -> None:
    batches = []

    def fake_post(url, payload, headers, timeout):
        batches.append(payload["input"])
        return _Response(
            payload={
                "data": [
                    {"index": index, "embedding": [1.0, 0.0, 0.0]}
                    for index in range(len(payload["input"]))
                ]
            }
        )

    monkeypatch.setattr(embedding_module, "_post_json", fake_post)
    provider, _ = build_embedder(_remote_settings())

    provider.embed(["A", "B", "C"])

    assert batches == [["A", "B"], ["C"]]


def test_remote_provider_rejects_http_and_dimension_errors(monkeypatch) -> None:
    provider, _ = build_embedder(_remote_settings())

    monkeypatch.setattr(
        embedding_module,
        "_post_json",
        lambda *args, **kwargs: _Response(status_code=429, text="rate limited"),
    )
    with pytest.raises(EmbeddingError, match="429"):
        provider.embed(["A"])

    monkeypatch.setattr(
        embedding_module,
        "_post_json",
        lambda *args, **kwargs: _Response(
            payload={"data": [{"index": 0, "embedding": [1.0, 0.0]}]}
        ),
    )
    with pytest.raises(EmbeddingError, match="维度"):
        provider.embed(["A"])


def test_remote_requires_explicit_opt_in_and_safe_profile() -> None:
    provider, error = build_embedder(
        _remote_settings(memory_allow_remote_embeddings=False)
    )
    assert provider is None
    assert "ALLOW_REMOTE" in error

    provider, error = build_embedder(
        _remote_settings(security_profile="untrusted")
    )
    assert provider is None
    assert "untrusted" in error

    provider, error = build_embedder(
        _remote_settings(memory_embedding_api_key="")
    )
    assert provider is None
    assert "API_KEY" in error


def test_remote_status_does_not_require_model_load() -> None:
    ok, detail = embedding_status(_remote_settings())

    assert ok is True
    assert "remote" in detail
    assert "secret-token" not in detail
