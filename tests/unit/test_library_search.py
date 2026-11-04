"""词法检索、引用渲染与跨工作区根目录的验收锚点。"""

from __future__ import annotations

from ubismart.library.root import resolve_library_root
from ubismart.library.search import render_hits, score, tokens


class _Settings:
    def __init__(self, root: str) -> None:
        self.library_root = root


class _Sandbox:
    def home(self) -> str:
        return "/home/tester"


def test_tokens_cover_latin_cjk_and_bigrams() -> None:
    result = tokens("HTTPS 接口 API_v2")
    assert "https" in result
    assert "api_v2" in result
    assert "接" in result and "口" in result
    assert "接口" in result


def test_score_prefers_exact_over_partial_and_ignores_empty_query() -> None:
    query = tokens("接口 HTTPS")
    exact = score(query, "接口 HTTPS")
    partial = score(query, "接口")
    assert exact > partial > 0
    assert score(set(), "anything") == 0.0
    assert score(query, "无关内容") == 0.0


def test_render_hits_keeps_citations_mode_and_truncates() -> None:
    hits = [
        {
            "item_id": "lib_a",
            "anchor": "page=3",
            "name": "规范.pdf",
            "heading": "接口",
            "score": 0.75,
            "mode": "hybrid",
            "text": "x" * 2_000,
        }
    ]
    text = render_hits(hits, max_chars=500)
    assert "[lib_a#page=3]" in text
    assert "规范.pdf" in text
    assert "mode=hybrid" in text
    assert "结果已截断" in text


def test_library_root_is_stable_across_workspaces() -> None:
    settings = _Settings("/srv/library/alice")
    sandbox = _Sandbox()
    assert (
        resolve_library_root(settings, sandbox, "/srv/ws/w1")
        == "/srv/library/alice"
    )
    assert (
        resolve_library_root(settings, sandbox, "/srv/ws/w2")
        == "/srv/library/alice"
    )


def test_library_root_default_is_outside_workspace() -> None:
    settings = _Settings("")
    sandbox = _Sandbox()
    root = resolve_library_root(settings, sandbox, "/home/tester/agent-ws")
    assert root == "/home/tester/.agent-library"
    assert not root.startswith("/home/tester/agent-ws")
