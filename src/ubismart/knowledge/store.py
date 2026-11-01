"""知识产物持久化与确定性检索（RFC-0009 / ADR-0029）。

产物落在工作区 `.agent/artifacts/knowledge/<run_id>/<kind>/<artifact_id>.json`：

- `doc`：文档 chunk（带 anchor）；
- `claims`：claim + quote + anchor 证据；
- `arch`：`ArchIndex`；
- `brief`：架构简报。

检索是**确定性词法匹配**（拉丁词 + CJK 单字/双字），不依赖 embedding，
因此默认不需要网络或额外模型；后续可在同一接口下替换为向量检索。
所有路径由宿主拼装，模型只能传 artifact_id / selector，不能传路径。
"""

from __future__ import annotations

import hashlib
import json
import posixpath
import re
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from ubismart.knowledge.embedding import cosine, embed_texts
from ubismart.sandbox.fs import SandboxFs, SandboxFsError
from ubismart.sandbox.pathguard import SandboxPathError
from ubismart.sandbox.wsl_exec import WslSandbox

KNOWLEDGE_DIRNAME = ".agent/artifacts/knowledge"
KINDS = ("doc", "claims", "arch", "brief", "contract")
MAX_READ_BYTES = 4_000_000
MAX_CHUNK_CHARS = 4_000
_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_LATIN_RE = re.compile(r"[a-z0-9_]{2,}")

_RUN_ID: ContextVar[str] = ContextVar("ubismart_knowledge_run", default="")


@contextmanager
def knowledge_context(run_id: str) -> Iterator[None]:
    token = _RUN_ID.set(str(run_id or ""))
    try:
        yield
    finally:
        _RUN_ID.reset(token)


def current_knowledge_run_id() -> str:
    return _RUN_ID.get()


_SAFE_ARTIFACT_RE = re.compile(r"[A-Za-z0-9_.-]{1,80}")


def _safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or ""))[:48]
    text = text.replace("..", "_").strip("._")
    digest = hashlib.sha1(str(value or "").encode("utf-8")).hexdigest()[:8]
    return f"{text}-{digest}" if text else digest


def _artifact_name(value: str) -> str:
    """已经是安全 id 时保持原样，避免 list→load 二次哈希。"""
    text = str(value or "")
    if _SAFE_ARTIFACT_RE.fullmatch(text) and ".." not in text:
        return text
    return _safe_name(text)


def _tokens(text: str) -> set[str]:
    lowered = str(text or "").lower()
    latin = set(_LATIN_RE.findall(lowered))
    cjk = _CJK_RE.findall(lowered)
    grams = {cjk[index] + cjk[index + 1] for index in range(len(cjk) - 1)}
    return latin | set(cjk) | grams


def _score(query: set[str], text: str) -> float:
    if not query:
        return 0.0
    matched = query & _tokens(text)
    if not matched:
        return 0.0
    return len(matched) / (len(query) ** 0.5)


def _doc_items(
    artifact_id: str, payload: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    for chunk in payload.get("chunks") or []:
        if not isinstance(chunk, dict):
            continue
        yield {
            "kind": "doc",
            "artifact_id": artifact_id,
            "path": str(chunk.get("path", "")),
            "anchor": str(chunk.get("anchor", "")),
            "text": str(chunk.get("text", "")),
        }


def _claims_items(
    artifact_id: str, payload: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    for claim in payload.get("claims") or []:
        if not isinstance(claim, dict):
            continue
        yield {
            "kind": "claims",
            "artifact_id": artifact_id,
            "path": str(claim.get("evidence_path", "")),
            "anchor": str(claim.get("anchor", "")),
            "text": " ".join(
                str(claim.get(key, ""))
                for key in ("claim", "quote")
            ).strip(),
        }


def _arch_items(
    artifact_id: str, payload: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    for path, symbols in (payload.get("symbols") or {}).items():
        yield {
            "kind": "arch",
            "artifact_id": artifact_id,
            "path": str(path),
            "anchor": "",
            "text": " ".join(
                [str(path), *[str(item) for item in symbols]]
            ),
        }


def _brief_items(
    artifact_id: str, payload: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    yield {
        "kind": "brief",
        "artifact_id": artifact_id,
        "path": "",
        "anchor": "",
        "text": str(payload.get("text", "")),
    }


def _contract_items(
    artifact_id: str, payload: dict[str, Any]
) -> Iterator[dict[str, Any]]:
    names = [
        str(item.get("name", ""))
        for item in payload.get("interfaces") or []
        if isinstance(item, dict)
    ]
    files = [
        str(path)
        for wave in payload.get("waves") or []
        if isinstance(wave, dict)
        for path in wave.get("files") or []
    ]
    yield {
        "kind": "contract",
        "artifact_id": artifact_id,
        "path": files[0] if files else "",
        "anchor": "",
        "text": " ".join(
            [
                str(payload.get("request", "")),
                *names,
                *files,
                *[
                    str(item)
                    for item in payload.get("affected_tests") or []
                ],
            ]
        ),
    }


_ITEM_BUILDERS = {
    "doc": _doc_items,
    "claims": _claims_items,
    "arch": _arch_items,
    "brief": _brief_items,
    "contract": _contract_items,
}


def _matches(text: str, needle: str) -> bool:
    return not needle or needle in text.lower()


def _read_doc_items(
    payload: dict[str, Any], needle: str
) -> list[Any]:
    items: list[Any] = []
    for chunk in payload.get("chunks") or []:
        text = str(chunk.get("text", ""))
        if _matches(text, needle) or _matches(str(chunk.get("anchor", "")), needle):
            items.append(chunk)
    return items


def _read_claims_items(
    payload: dict[str, Any], needle: str
) -> list[Any]:
    items: list[Any] = []
    for claim in payload.get("claims") or []:
        text = " ".join(
            str(claim.get(key, ""))
            for key in ("claim", "quote")
        )
        if _matches(text, needle):
            items.append(claim)
    return items


def _read_arch_items(
    payload: dict[str, Any], needle: str
) -> list[Any]:
    items: list[Any] = []
    for path, symbols in (payload.get("symbols") or {}).items():
        text = " ".join([str(path), *map(str, symbols)])
        if _matches(text, needle):
            items.append(
                {
                    "path": path,
                    "symbols": symbols,
                    "imports": payload.get("imports", {}).get(path, []),
                }
            )
    return items


def _read_contract_items(
    payload: dict[str, Any], needle: str
) -> list[Any]:
    if not needle or needle in json.dumps(
        payload, ensure_ascii=False
    ).lower():
        return [payload]
    return []


_READERS = {
    "doc": _read_doc_items,
    "claims": _read_claims_items,
    "arch": _read_arch_items,
    "contract": _read_contract_items,
}


class KnowledgeStore:
    def __init__(self, sandbox: WslSandbox, root: str) -> None:
        self._fs = SandboxFs(sandbox, root)
        self._root = root

    def _run_dir(self, run_id: str) -> str:
        return posixpath.join(KNOWLEDGE_DIRNAME, _safe_name(run_id))

    def _rel(self, run_id: str, kind: str, artifact_id: str) -> str:
        if kind not in KINDS:
            raise ValueError(f"不支持的知识产物类型：{kind}")
        return posixpath.join(
            self._run_dir(run_id),
            kind,
            f"{_artifact_name(artifact_id)}.json",
        )

    def save(
        self,
        run_id: str,
        kind: str,
        artifact_id: str,
        payload: dict[str, Any],
    ) -> str:
        rel = self._rel(run_id, kind, artifact_id)
        body = json.dumps(payload, ensure_ascii=False, default=str)
        try:
            self._fs.write_text(
                posixpath.join(self._root, rel), body
            )
        except (SandboxFsError, SandboxPathError):
            return ""
        return rel

    def list_artifacts(self, run_id: str, kind: str) -> list[str]:
        if kind not in KINDS:
            return []
        rel = posixpath.join(self._run_dir(run_id), kind)
        try:
            entries = self._fs.list_dir(
                posixpath.join(self._root, rel), limit=200
            )
        except (SandboxFsError, SandboxPathError):
            return []
        return sorted(
            name[: -len(".json")]
            for entry_type, name in entries
            if entry_type == "f" and name.endswith(".json")
        )

    def load(
        self, run_id: str, kind: str, artifact_id: str
    ) -> dict[str, Any] | None:
        if kind not in KINDS:
            return None
        rel = posixpath.join(
            self._run_dir(run_id), kind, f"{_artifact_name(artifact_id)}.json"
        )
        try:
            text = self._fs.read_text(
                posixpath.join(self._root, rel), max_bytes=MAX_READ_BYTES
            )
        except (SandboxFsError, SandboxPathError):
            return None
        try:
            payload = json.loads(text)
        except ValueError:
            return None
        return payload if isinstance(payload, dict) else None

    def save_doc_index(
        self,
        run_id: str,
        doc_id: str,
        *,
        documents: list[dict[str, Any]],
        chunks: list[dict[str, Any]],
        summary: str,
    ) -> str:
        payload = {
            "kind": "doc",
            "doc_id": doc_id,
            "summary": summary[:2_000],
            "documents": documents,
            "chunks": [
                {
                    **chunk,
                    "text": str(chunk.get("text", ""))[:MAX_CHUNK_CHARS],
                }
                for chunk in chunks
            ],
        }
        return self.save(run_id, "doc", doc_id, payload)

    def save_claims(
        self, run_id: str, artifact_id: str, claims: list[dict[str, Any]]
    ) -> str:
        return self.save(
            run_id,
            "claims",
            artifact_id,
            {"kind": "claims", "claims": claims[:200]},
        )

    def _iter_items(
        self, run_id: str, kinds: list[str]
    ) -> Iterator[dict[str, Any]]:
        for kind in kinds:
            builder = _ITEM_BUILDERS.get(kind)
            if builder is None:
                continue
            for artifact_id in self.list_artifacts(run_id, kind):
                payload = self.load(run_id, kind, artifact_id)
                if payload is None:
                    continue
                yield from builder(artifact_id, payload)

    def search(
        self,
        run_id: str,
        query: str,
        *,
        top_k: int = 8,
        kind: str = "all",
        embedder: Any | None = None,
        mode: str = "lexical",
    ) -> list[dict[str, Any]]:
        kinds = list(KINDS) if kind == "all" else [kind]
        items = list(self._iter_items(run_id, kinds))
        tokens = _tokens(query)
        lexical = [_score(tokens, item["text"]) for item in items]
        mode = str(mode or "lexical").strip().lower()
        vector_scores = [0.0 for _ in items]
        if (
            embedder is not None
            and mode in {"vector", "hybrid"}
            and items
        ):
            vectors = embed_texts(
                embedder, [str(item["text"]) for item in items] + [query]
            )
            if vectors:
                query_vector = vectors[-1]
                vector_scores = [
                    cosine(query_vector, vector) for vector in vectors[:-1]
                ]
        max_lexical = max(lexical, default=0.0)
        scored: list[tuple[float, dict[str, Any]]] = []
        for item, lex, vector in zip(
            items, lexical, vector_scores, strict=True
        ):
            if mode == "vector":
                score = max(0.0, vector)
            elif mode == "hybrid":
                score = (
                    (lex / max_lexical if max_lexical > 0 else 0.0) * 0.5
                    + max(0.0, vector) * 0.5
                )
            else:
                score = lex
            if score > 0:
                scored.append((score, {**item, "vector_score": round(vector, 4)}))
        scored.sort(
            key=lambda pair: (
                -pair[0],
                pair[1]["kind"],
                pair[1]["path"],
                pair[1]["anchor"],
            )
        )
        results: list[dict[str, Any]] = []
        for score, item in scored[: max(1, int(top_k))]:
            results.append(
                {
                    **item,
                    "text": item["text"][:MAX_CHUNK_CHARS],
                    "score": round(score, 3),
                }
            )
        return results

    def read(
        self,
        run_id: str,
        kind: str,
        artifact_id: str = "",
        *,
        selector: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        cap = max(1, min(int(limit), 100))
        if not artifact_id:
            if kind in {"arch", "brief"}:
                artifact_id = "arch"
            else:
                return {
                    "kind": kind,
                    "artifacts": self.list_artifacts(run_id, kind),
                    "items": [],
                }
        payload = self.load(run_id, kind, artifact_id)
        if payload is None:
            return {"kind": kind, "artifact_id": artifact_id, "items": []}
        needle = str(selector or "").strip().lower()
        reader = _READERS.get(kind)
        items = reader(payload, needle) if reader else [payload]
        return {
            "kind": kind,
            "artifact_id": artifact_id,
            "total": len(items),
            "truncated": len(items) > cap,
            "items": items[:cap],
        }

    def render_search(self, run_id: str, results: list[dict[str, Any]]) -> str:
        if not results:
            return "知识产物中没有匹配内容。"
        lines = [f"[知识检索] run={run_id} 共 {len(results)} 条"]
        for item in results:
            where = (
                f"{item.get('path', '')}#{item.get('anchor', '')}".rstrip("#")
            )
            lines.append(
                f"- [{item.get('kind')}] {where} "
                f"(score={item.get('score')})\n"
                f"  {str(item.get('text', ''))[:600]}"
            )
        return "\n".join(lines)
