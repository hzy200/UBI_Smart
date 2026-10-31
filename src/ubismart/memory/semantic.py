"""可选语义检索层：BM25 + 向量 RRF，失败自动回退。

`SemanticMemory` 与 `LayeredMemory` 暴露同一组接口；关闭向量时完全透传。
向量只是增强信息，任何 embedding/索引失败都不影响记忆读写与 run。
"""

from __future__ import annotations

from typing import Any

from ubismart.memory.embeddings import (
    EmbeddingError,
    EmbeddingProvider,
    build_embedder,
)
from ubismart.memory.longterm import LayeredMemory, LongTermMemory
from ubismart.memory.vectors import (
    VectorStore,
    cosine,
    rrf_fuse,
)
from ubismart.sandbox.fs import SandboxFsError
from ubismart.sandbox.pathguard import SandboxPathError
from ubismart.security.secrets import secret_kinds


def _scope_memories(
    layered: LayeredMemory,
) -> list[tuple[str, LongTermMemory]]:
    result = [("project", layered.project)]
    if layered.global_memory is not None:
        result.append(("global", layered.global_memory))
    return result


class SemanticMemory:
    def __init__(
        self,
        layered: LayeredMemory,
        settings: Any,
        *,
        embedder: EmbeddingProvider | None = None,
        embedding_error: str = "",
    ) -> None:
        self.layered = layered
        self.settings = settings
        if embedder is None and not embedding_error:
            embedder, embedding_error = build_embedder(settings)
        self.embedder = embedder
        self.embedding_error = embedding_error
        self.last_error = ""
        self._stores: dict[str, VectorStore] = {}
        self._top_n = max(
            1, int(getattr(settings, "memory_vector_top_n", 20) or 20)
        )

    # ---------------- 透传属性 ----------------

    @property
    def path(self) -> str:
        return self.layered.path

    @property
    def meta_path(self) -> str:
        return self.layered.meta_path

    @property
    def last_replaced(self) -> list[str]:
        return self.layered.last_replaced

    @property
    def last_related(self) -> list[str]:
        return self.layered.last_related

    @property
    def last_removed(self) -> str:
        return self.layered.last_removed

    @property
    def last_backup_id(self) -> str:
        return self.layered.last_backup_id

    @property
    def last_unsafe(self) -> list[str]:
        return list(self.layered.last_unsafe)

    def path_for(self, scope: str) -> str:
        return self.layered.path_for(scope)

    # ---------------- 向量索引 ----------------

    @property
    def enabled(self) -> bool:
        return self.embedder is not None

    def _store(self, memory: LongTermMemory) -> VectorStore | None:
        if self.embedder is None:
            return None
        key = memory.state_root
        store = self._stores.get(key)
        if store is None:
            store = VectorStore(
                memory.fs,
                memory.state_root,
                provider=self.embedder.name,
                model=self.embedder.model,
                dimension=self.embedder.dimension,
            )
            self._stores[key] = store
        return store

    def _embed(self, texts: list[str]) -> list[list[float]] | None:
        if self.embedder is None or not texts:
            return None
        try:
            vectors = self.embedder.embed(texts)
        except EmbeddingError as exc:
            self.last_error = str(exc)
            return None
        except Exception as exc:  # noqa: BLE001 - provider 失败必须回退
            self.last_error = f"{type(exc).__name__}: {exc}"
            return None
        if len(vectors) != len(texts):
            self.last_error = "embedding 返回数量与输入不一致"
            return None
        return vectors

    def _sync_scope(self, scope: str, *, force: bool = False) -> dict[str, int]:
        memory = self.layered._target(scope)  # noqa: SLF001 - 同包协作
        store = self._store(memory)
        facts = memory.load()
        unsafe = set(memory.last_unsafe)
        if unsafe:
            # 疑似密钥事实不送 embedding、不写向量。
            facts = [fact for fact in facts if fact not in unsafe]
        if store is None:
            return {"facts": len(facts), "indexed": 0, "pruned": 0}
        pruned = store.prune(facts)
        missing = facts if force else store.missing(facts)
        if not missing:
            return {"facts": len(facts), "indexed": 0, "pruned": pruned}
        vectors = self._embed(missing)
        if vectors is None:
            return {"facts": len(facts), "indexed": 0, "pruned": pruned}
        try:
            store.put_many(dict(zip(missing, vectors, strict=True)))
        except (SandboxFsError, SandboxPathError) as exc:
            self.last_error = f"向量索引写入失败：{exc}"
            return {"facts": len(facts), "indexed": 0, "pruned": pruned}
        return {
            "facts": len(facts),
            "indexed": len(missing),
            "pruned": pruned,
        }

    # ---------------- 读写 ----------------

    def load(self, *, scope: str | None = None) -> list[str]:
        return self.layered.load(scope=scope)

    def add(
        self,
        text: str,
        *,
        source: str = "user",
        scope: str = "project",
        ttl_days: int | None = None,
        reason: str = "",
        thread_id: str = "",
        run_id: str = "",
    ) -> list[str]:
        facts = self.layered.add(
            text,
            source=source,
            scope=scope,
            ttl_days=ttl_days,
            reason=reason,
            thread_id=thread_id,
            run_id=run_id,
        )
        self._sync_scope(scope)
        return facts

    def remove(self, index: int, *, scope: str = "project") -> list[str]:
        facts = self.layered.remove(index, scope=scope)
        self._sync_scope(scope)
        return facts

    def clear(self, *, scope: str = "project") -> list[str]:
        facts = self.layered.clear(scope=scope)
        self._sync_scope(scope, force=True)
        return facts

    def restore(
        self, backup_id: str, *, scope: str = "project"
    ) -> list[str]:
        facts = self.layered.restore(backup_id, scope=scope)
        self._sync_scope(scope)
        return facts

    def set_pinned(
        self, index: int, pinned: bool, *, scope: str = "project"
    ) -> list[str]:
        return self.layered.set_pinned(index, pinned, scope=scope)

    def set_priority(
        self, index: int, priority: int, *, scope: str = "project"
    ) -> list[str]:
        return self.layered.set_priority(index, priority, scope=scope)

    def history(
        self, limit: int = 20, *, scope: str = "project"
    ) -> list[dict[str, str]]:
        return self.layered.history(limit, scope=scope)

    def conflicts(
        self, *, scope: str | None = None
    ) -> list[dict[str, object]]:
        return self.layered.conflicts(scope=scope)

    # ---------------- 检索 ----------------

    def _vector_rank(
        self,
        memory: LongTermMemory,
        query_vector: list[float],
        store: VectorStore,
    ) -> list[str]:
        facts = memory.search("", 0)  # 全部安全事实，带 pinned/priority 顺序
        vectors = store.all(facts)
        weights = memory.meta_weights(facts)
        order = {fact: index for index, fact in enumerate(facts)}
        scored: list[tuple[float, int, str]] = []
        for fact in facts:
            vector = vectors.get(fact)
            if vector is None:
                continue
            score = cosine(query_vector, vector) * weights.get(fact, 1.0)
            if score <= 0:
                continue
            scored.append((-score, order[fact], fact))
        scored.sort()
        return [fact for _, _, fact in scored]

    def search(self, query: str = "", limit: int = 8) -> list[str]:
        if self.embedder is None or not str(query or "").strip():
            return self.layered.search(query, limit)
        if (
            getattr(self.embedder, "name", "") == "remote"
            and secret_kinds(str(query))
        ):
            # 查询本身可能含密钥；不要发给远程 provider。
            self.last_error = "查询疑似包含密钥，已跳过远程 embedding"
            return self.layered.search(query, limit)
        query_vectors = self._embed([query])
        if not query_vectors:
            return self.layered.search(query, limit)
        query_vector = query_vectors[0]

        merged: list[str] = []
        for _scope, memory in _scope_memories(self.layered):
            store = self._store(memory)
            lexical = memory.search(
                query, max(limit, self._top_n), fallback=False
            )
            vector: list[str] = []
            if store is not None:
                vector = self._vector_rank(
                    memory, query_vector, store
                )
            fused = rrf_fuse(lexical, vector, limit=max(limit, self._top_n))
            for fact in fused:
                if fact not in merged:
                    merged.append(fact)
            if limit > 0 and len(merged) >= limit:
                break
        if not merged:
            return self.layered.search(query, limit)
        return merged if limit <= 0 else merged[:limit]

    # ---------------- 管理 ----------------

    def reindex(
        self, *, scope: str | None = None, force: bool = False
    ) -> dict[str, dict[str, int]]:
        scopes = (
            [scope]
            if scope is not None
            else [name for name, _ in _scope_memories(self.layered)]
        )
        return {
            name: self._sync_scope(name, force=force) for name in scopes
        }

    def status(self, *, scope: str | None = None) -> dict[str, Any]:
        scopes = (
            [scope]
            if scope is not None
            else [name for name, _ in _scope_memories(self.layered)]
        )
        details: dict[str, Any] = {}
        for name in scopes:
            memory = self.layered._target(name)  # noqa: SLF001
            store = self._store(memory)
            facts = memory.load()
            details[name] = (
                store.status(facts)
                if store is not None
                else {
                    "path": "",
                    "provider": "",
                    "model": "",
                    "dimension": 0,
                    "header_ok": False,
                    "facts": len(facts),
                    "vectors": 0,
                    "missing": len(facts),
                }
            )
        return {
            "enabled": self.enabled,
            "provider": getattr(self.embedder, "name", ""),
            "model": getattr(self.embedder, "model", ""),
            "dimension": getattr(self.embedder, "dimension", 0),
            "error": self.embedding_error or self.last_error,
            "scopes": details,
        }


def wrap_memory(layered: LayeredMemory, settings: Any) -> SemanticMemory:
    return SemanticMemory(layered, settings)
