"""library_search 工具与审批等级。"""

from __future__ import annotations

from typing import Any

from tests.unit.test_library_store import MemoryFs

from ubismart.config import Settings
from ubismart.graph.nodes.approve import _READ_TOOLS, tool_level
from ubismart.library.store import LibraryStore
from ubismart.memory.embeddings import HashEmbedder
from ubismart.sandbox.policy import CommandLevel
from ubismart.tools.artifacts import unpack
from ubismart.tools.library import LIBRARY_SEARCH, build_library_tools


class _Sandbox:
    def home(self) -> str:
        return "/home/u"


def _settings(**updates: Any) -> Settings:
    values: dict[str, Any] = {
        "library_enabled": True,
        "library_root": "/lib",
        "wsl_workspace": "/ws",
    }
    values.update(updates)
    return Settings(_env_file=None, **values)


def test_library_search_is_read_only_tool() -> None:
    assert LIBRARY_SEARCH in _READ_TOOLS
    assert tool_level(LIBRARY_SEARCH, {}) == CommandLevel.READ


def test_library_tool_returns_citation_and_redacts(monkeypatch) -> None:
    fs = MemoryFs("/lib")
    store = LibraryStore(fs, "/lib")
    item = store.ingest(
        "spec.md",
        b"# title\n\nAPI key sk-abcdefghijklmnopqrstuvwxyz012345",
    )
    assert item["status"] == "ready"

    monkeypatch.setattr(
        "ubismart.tools.library.SandboxFs",
        lambda sandbox, root, cache=None: fs,
    )
    tools = build_library_tools(_settings(), _Sandbox())  # type: ignore[arg-type]
    assert [tool.name for tool in tools] == [LIBRARY_SEARCH]
    text, artifact = unpack(tools[0].invoke({"query": "API key", "top_k": 3}))
    assert artifact is not None and artifact["ok"] is True
    assert "只读引用" in text
    assert "sk-" not in text


def test_library_tool_uses_vector_mode(monkeypatch) -> None:
    fs = MemoryFs("/lib")
    embedder = HashEmbedder(32)
    store = LibraryStore(
        fs, "/lib", search_mode="vector", embedder=embedder
    )
    store.ingest("a.md", b"# alpha\n\nalpha alpha")
    monkeypatch.setattr(
        "ubismart.tools.library.SandboxFs",
        lambda sandbox, root, cache=None: fs,
    )
    tools = build_library_tools(
        _settings(
            library_search_mode="vector",
            memory_vector_enabled=True,
            memory_embedding_provider="hash",
            memory_embedding_dim=32,
        ),
        _Sandbox(),  # type: ignore[arg-type]
    )
    text, artifact = unpack(
        tools[0].invoke({"query": "alpha", "mode": "vector"})
    )
    assert artifact is not None and artifact["ok"] is True
    assert "lib_" in text
    assert "vector" in text


def test_library_tool_is_absent_when_disabled() -> None:
    tools = build_library_tools(
        _settings(library_enabled=False), _Sandbox()  # type: ignore[arg-type]
    )
    assert tools == []
