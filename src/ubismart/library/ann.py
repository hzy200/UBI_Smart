"""可选的向量索引后端。

默认 `linear`（进程内余弦），不新增依赖；配置 `hnswlib` 时按需导入，
不可用或建索引失败则回退 linear。索引只在内存中缓存，目录仍以
`vectors.json` 为事实源。
"""

from __future__ import annotations

from typing import Any

from ubismart.library.embedding import cosine


class LinearVectorIndex:
    backend = "linear"

    def __init__(self, entries: list[tuple[int, list[float]]]) -> None:
        self.entries = entries
        self.size = len(entries)

    def search(
        self, query: list[float], k: int
    ) -> list[tuple[int, float]]:
        scores = [
            (index, cosine(query, vector))
            for index, vector in self.entries
        ]
        scores.sort(key=lambda pair: -pair[1])
        return scores[: max(1, int(k))]


class HnswVectorIndex:
    backend = "hnswlib"

    def __init__(
        self, index: Any, labels: list[int], size: int
    ) -> None:
        self._index = index
        self._labels = labels
        self.size = size

    def search(
        self, query: list[float], k: int
    ) -> list[tuple[int, float]]:
        labels, distances = self._index.knn_query(
            query, k=min(max(1, int(k)), max(1, self.size))
        )
        results: list[tuple[int, float]] = []
        for label, distance in zip(
            list(labels[0]), list(distances[0]), strict=True
        ):
            results.append((int(label), 1.0 - float(distance)))
        return results


def build_vector_index(
    entries: list[dict[str, Any]],
    *,
    dimension: int,
    backend: str = "linear",
) -> LinearVectorIndex | HnswVectorIndex:
    pairs = [
        (index, list(entry.get("vector") or []))
        for index, entry in enumerate(entries)
        if len(entry.get("vector") or []) == dimension
    ]
    if backend != "hnswlib" or not pairs:
        return LinearVectorIndex(pairs)
    try:
        import hnswlib  # type: ignore[import-not-found]
    except ImportError:
        return LinearVectorIndex(pairs)
    try:
        index = hnswlib.Index(space="cosine", dim=dimension)
        index.init_index(
            max_elements=max(1, len(pairs)),
            ef_construction=200,
            M=16,
        )
        index.add_items(
            [vector for _, vector in pairs],
            [label for label, _ in pairs],
        )
        index.set_ef(max(64, len(pairs) // 10))
    except Exception:  # noqa: BLE001 - ANN 是增强，失败回退线性
        return LinearVectorIndex(pairs)
    return HnswVectorIndex(
        index, [label for label, _ in pairs], len(pairs)
    )
