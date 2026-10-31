"""knowledge_read / knowledge_search 工具：上下文、路径与返回值。"""

from __future__ import annotations

from typing import Any

import pytest

from ubismart.config import Settings
from ubismart.knowledge.store import knowledge_context
from ubismart.tools import knowledge as knowledge_module
from ubismart.tools.artifacts import unpack
from ubismart.tools.knowledge import (
    KNOWLEDGE_READ,
    KNOWLEDGE_SEARCH,
    build_knowledge_tools,
)


class _FakeStore:
    def __init__(self) -> None:
        self.searches: list[dict[str, Any]] = []
        self.reads: list[dict[str, Any]] = []

    def search(
        self,
        run_id: str,
        query: str,
        *,
        top_k: int = 8,
        kind: str = "all",
        **_kwargs: Any,
    ) -> list[dict[str, Any]]:
        self.searches.append(
            {"run_id": run_id, "query": query, "top_k": top_k, "kind": kind}
        )
        return [
            {
                "kind": "doc",
                "artifact_id": "doc-1",
                "path": "a.pdf",
                "anchor": "page=1",
                "text": "折扣逻辑",
                "score": 1.0,
            }
        ]

    def render_search(self, run_id: str, results: list[dict[str, Any]]) -> str:
        return f"[知识检索] run={run_id} hits={len(results)}"

    def read(
        self,
        run_id: str,
        kind: str,
        artifact_id: str = "",
        *,
        selector: str = "",
        limit: int = 20,
    ) -> dict[str, Any]:
        self.reads.append(
            {
                "run_id": run_id,
                "kind": kind,
                "artifact_id": artifact_id,
                "selector": selector,
            }
        )
        return {"kind": kind, "artifact_id": artifact_id, "items": [{"text": "x"}]}


def _tools(monkeypatch: pytest.MonkeyPatch) -> tuple[list[Any], _FakeStore]:
    fake = _FakeStore()

    def _factory(sandbox: Any, root: str) -> _FakeStore:
        return fake

    monkeypatch.setattr(knowledge_module, "KnowledgeStore", _factory)
    monkeypatch.setattr(
        knowledge_module, "resolve_workspace", lambda settings, sandbox: "/ws"
    )
    settings = Settings(_env_file=None, wsl_workspace="/ws")
    return build_knowledge_tools(settings, object()), fake  # type: ignore[arg-type]


def _invoke(tool: Any, args: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    return unpack(tool.invoke(args))


def test_search_requires_a_run_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, fake = _tools(monkeypatch)
    search = next(tool for tool in tools if tool.name == KNOWLEDGE_SEARCH)

    text, artifact = _invoke(search, {"query": "折扣"})

    assert "没有知识产物上下文" in text
    assert artifact is not None and artifact.get("skipped") is True
    assert fake.searches == []


def test_search_reads_the_current_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, fake = _tools(monkeypatch)
    search = next(tool for tool in tools if tool.name == KNOWLEDGE_SEARCH)

    with knowledge_context("run-1"):
        text, artifact = _invoke(search, {"query": "折扣", "kind": "doc"})

    assert "hits=1" in text
    assert artifact is not None and artifact.get("ok") is True
    assert fake.searches == [
        {"run_id": "run-1", "query": "折扣", "top_k": 8, "kind": "doc"}
    ]


def test_read_passes_selector_and_artifact_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tools, fake = _tools(monkeypatch)
    read = next(tool for tool in tools if tool.name == KNOWLEDGE_READ)

    with knowledge_context("run-1"):
        text, artifact = _invoke(
            read,
            {"kind": "doc", "artifact_id": "doc-1", "selector": "折扣"},
        )

    assert "doc-1" in text
    assert artifact is not None and artifact.get("ok") is True
    assert fake.reads[0]["selector"] == "折扣"
