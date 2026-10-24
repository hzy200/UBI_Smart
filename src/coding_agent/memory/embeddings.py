"""可选记忆向量化 provider。

默认关闭；开启后仍以 BM25 为回退。当前内置三类：

- `hash`：确定性哈希向量，仅用于测试/无模型环境，不具备语义能力；
- `local`：可选依赖 `fastembed`（ONNX，CPU），模型缓存在宿主侧；
- `remote`：OpenAI-compatible `/embeddings`。记忆内容会离开本机，
  必须显式设置 `AGENT_MEMORY_ALLOW_REMOTE_EMBEDDINGS=true`，且
  不可信/生产 profile 一律拒绝。
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
from typing import Any, Protocol

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{2,}|[\u3400-\u9fff]")


class EmbeddingError(RuntimeError):
    """embedding provider 不可用或调用失败。"""


class EmbeddingProvider(Protocol):
    name: str
    model: str
    dimension: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...


class HashEmbedder:
    """确定性哈希向量；只用于测试与降级验证，不是语义模型。"""

    name = "hash"

    def __init__(self, dimension: int = 256) -> None:
        self.dimension = max(16, int(dimension))
        self.model = f"hash-{self.dimension}"

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for text in texts:
            vector = [0.0] * self.dimension
            for token in _TOKEN_RE.findall(str(text).lower()):
                digest = hashlib.blake2b(
                    token.encode("utf-8"), digest_size=8
                ).digest()
                value = int.from_bytes(digest, "big")
                index = value % self.dimension
                sign = 1.0 if (value >> 63) & 1 == 0 else -1.0
                vector[index] += sign
            norm = sum(item * item for item in vector) ** 0.5
            if norm:
                vector = [item / norm for item in vector]
            vectors.append(vector)
        return vectors


class LocalFastEmbedProvider:
    """本地 ONNX 模型；首次 embed 时才加载/下载模型。"""

    name = "local"

    def __init__(
        self,
        model: str,
        *,
        dimension: int = 512,
        cache_dir: str = "",
    ) -> None:
        if importlib.util.find_spec("fastembed") is None:
            raise EmbeddingError(
                "未安装 fastembed；请运行 pip install -e \".[embeddings]\""
            )
        self.model = str(model or "BAAI/bge-small-zh-v1.5")
        self.dimension = max(1, int(dimension))
        self._cache_dir = str(cache_dir or "").strip()
        self._model: Any = None

    def _ensure_model(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - 构造时已检查
            raise EmbeddingError(f"fastembed 不可用：{exc}") from exc
        kwargs = {}
        if self._cache_dir:
            kwargs["cache_dir"] = self._cache_dir
        try:
            self._model = TextEmbedding(model_name=self.model, **kwargs)
        except Exception as exc:  # noqa: BLE001 - 模型下载/加载失败
            raise EmbeddingError(
                f"加载本地 embedding 模型失败（{self.model}）：{exc}"
            ) from exc
        return self._model

    def embed(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        try:
            vectors = [
                [float(value) for value in vector]
                for vector in model.embed(list(texts))
            ]
        except Exception as exc:  # noqa: BLE001 - provider 调用失败
            raise EmbeddingError(f"本地 embedding 调用失败：{exc}") from exc
        for vector in vectors:
            if len(vector) != self.dimension:
                raise EmbeddingError(
                    f"模型输出维度 {len(vector)} 与配置 "
                    f"AGENT_MEMORY_EMBEDDING_DIM={self.dimension} 不一致"
                )
        return vectors


def _post_json(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    timeout: float,
) -> Any:
    """隔离 HTTP 依赖，测试可替换本函数。"""
    import httpx

    return httpx.post(
        url, json=payload, headers=headers, timeout=timeout
    )


class RemoteOpenAIEmbeddingProvider:
    """OpenAI-compatible `/embeddings` 端点。

    记忆内容会离开本机，因此必须同时满足：
    - `AGENT_MEMORY_ALLOW_REMOTE_EMBEDDINGS=true`；
    - 安全 profile 是 `local`；
    - 配置了 base_url 与 api_key。
    """

    name = "remote"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        dimension: int = 512,
        timeout_s: float = 10.0,
        batch_size: int = 64,
    ) -> None:
        base_url = str(base_url or "").strip().rstrip("/")
        if not base_url:
            raise EmbeddingError("远程 embedding 缺少 base_url")
        if not str(api_key or "").strip():
            raise EmbeddingError("远程 embedding 缺少 api_key")
        self.base_url = base_url
        self._api_key = str(api_key)
        self.model = str(model or "text-embedding-3-small")
        self.dimension = max(1, int(dimension))
        self.timeout_s = max(0.1, float(timeout_s))
        self.batch_size = max(1, int(batch_size))

    @property
    def url(self) -> str:
        return f"{self.base_url}/embeddings"

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        response = _post_json(
            self.url,
            {"model": self.model, "input": texts},
            {
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
            },
            self.timeout_s,
        )
        status = int(getattr(response, "status_code", 0) or 0)
        if status < 200 or status >= 300:
            body = str(getattr(response, "text", "") or "")[:200]
            raise EmbeddingError(
                f"远程 embedding HTTP {status}：{body or '无响应体'}"
            )
        try:
            payload = response.json()
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingError(
                f"远程 embedding 响应不是 JSON：{exc}"
            ) from exc
        items = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise EmbeddingError("远程 embedding 响应缺少 data")
        ordered = sorted(
            items,
            key=lambda item: int(
                item.get("index", 0) if isinstance(item, dict) else 0
            ),
        )
        vectors: list[list[float]] = []
        for item in ordered:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, list) or not vector:
                raise EmbeddingError("远程 embedding 返回空向量")
            vectors.append([float(value) for value in vector])
        if len(vectors) != len(texts):
            raise EmbeddingError(
                f"远程 embedding 返回 {len(vectors)} 条，期望 {len(texts)} 条"
            )
        for vector in vectors:
            if len(vector) != self.dimension:
                raise EmbeddingError(
                    f"远程模型输出维度 {len(vector)} 与配置 "
                    f"AGENT_MEMORY_EMBEDDING_DIM={self.dimension} 不一致"
                )
        return vectors

    def embed(self, texts: list[str]) -> list[list[float]]:
        texts = list(texts)
        vectors: list[list[float]] = []
        try:
            for start in range(0, len(texts), self.batch_size):
                vectors.extend(
                    self._embed_batch(
                        texts[start : start + self.batch_size]
                    )
                )
        except EmbeddingError:
            raise
        except Exception as exc:  # noqa: BLE001 - 网络/超时/解析失败
            raise EmbeddingError(f"远程 embedding 调用失败：{exc}") from exc
        return vectors


_REMOTE_PROFILES = {"untrusted", "production", "ops"}


def _remote_gate(settings: Any) -> str:
    """返回空串表示允许；否则返回拒绝原因。"""
    profile = str(
        getattr(settings, "security_profile", "local") or "local"
    ).strip().lower()
    if profile in _REMOTE_PROFILES:
        return f"安全 profile {profile} 禁止远程 embedding"
    if not bool(
        getattr(settings, "memory_allow_remote_embeddings", False)
    ):
        return (
            "远程 embedding 未显式开启；请设置 "
            "AGENT_MEMORY_ALLOW_REMOTE_EMBEDDINGS=true"
        )
    if not str(getattr(settings, "memory_embedding_base_url", "") or "").strip():
        return "缺少 AGENT_MEMORY_EMBEDDING_BASE_URL"
    if not str(getattr(settings, "memory_embedding_api_key", "") or "").strip():
        return "缺少 AGENT_MEMORY_EMBEDDING_API_KEY"
    return ""


def build_embedder(
    settings: Any,
) -> tuple[EmbeddingProvider | None, str]:
    """按配置构造 provider；返回 `(provider, error)`，不抛异常。"""
    if not bool(getattr(settings, "memory_vector_enabled", False)):
        return None, ""
    name = str(
        getattr(settings, "memory_embedding_provider", "local") or "local"
    ).strip().lower()
    if name in {"", "none", "off", "disabled"}:
        return None, ""
    dimension = max(1, int(getattr(settings, "memory_embedding_dim", 512)))
    if name == "hash":
        return HashEmbedder(dimension), ""
    if name == "local":
        try:
            provider = LocalFastEmbedProvider(
                str(
                    getattr(
                        settings,
                        "memory_embedding_model",
                        "BAAI/bge-small-zh-v1.5",
                    )
                    or "BAAI/bge-small-zh-v1.5"
                ),
                dimension=dimension,
                cache_dir=str(
                    getattr(settings, "memory_embedding_cache_dir", "") or ""
                ),
            )
        except EmbeddingError as exc:
            return None, str(exc)
        return provider, ""
    if name in {"remote", "openai"}:
        gate = _remote_gate(settings)
        if gate:
            return None, gate
        try:
            provider = RemoteOpenAIEmbeddingProvider(
                base_url=str(
                    getattr(settings, "memory_embedding_base_url", "") or ""
                ),
                api_key=str(
                    getattr(settings, "memory_embedding_api_key", "") or ""
                ),
                model=str(
                    getattr(settings, "memory_embedding_model", "")
                    or "text-embedding-3-small"
                ),
                dimension=dimension,
                timeout_s=float(
                    getattr(settings, "memory_embedding_timeout_s", 10.0)
                    or 10.0
                ),
                batch_size=int(
                    getattr(settings, "memory_embedding_batch_size", 64)
                    or 64
                ),
            )
        except EmbeddingError as exc:
            return None, str(exc)
        return provider, ""
    return None, (
        f"未知的 embedding provider：{name}"
        "（支持 local / remote / hash / none）"
    )


def embedding_status(settings: Any) -> tuple[bool, str]:
    """给 doctor 用的轻量检查；不加载模型。"""
    if not bool(getattr(settings, "memory_vector_enabled", False)):
        return True, "未启用"
    name = str(
        getattr(settings, "memory_embedding_provider", "local") or "local"
    ).strip().lower()
    if name == "hash":
        return True, "hash（确定性测试向量）"
    if name == "local":
        if importlib.util.find_spec("fastembed") is None:
            return False, "未安装 fastembed（pip install -e \".[embeddings]\"）"
        return True, f"local · {getattr(settings, 'memory_embedding_model', '')}"
    if name in {"remote", "openai"}:
        gate = _remote_gate(settings)
        if gate:
            return False, gate
        if importlib.util.find_spec("httpx") is None:
            return False, "未安装 httpx"
        return True, (
            "remote · "
            f"{getattr(settings, 'memory_embedding_model', '')} · "
            f"{getattr(settings, 'memory_embedding_base_url', '')}"
        )
    if name in {"", "none", "off", "disabled"}:
        return True, "未启用"
    return False, f"未知 provider：{name}"
