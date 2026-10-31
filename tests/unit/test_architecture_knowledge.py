"""架构索引、简报与确定性影响分析。"""

from __future__ import annotations

from ubismart.knowledge.architecture import (
    apply_change_contract,
    arch_brief,
    build_arch_index,
    build_change_contract,
    impact_analysis,
)
from ubismart.knowledge.lsp_calls import resolve_dynamic_calls_with_lsp


def _sources() -> dict[str, str]:
    return {
        "pkg/__init__.py": "",
        "pkg/core.py": "def run(value):\n    return value\n",
        "pkg/api.py": (
            "from pkg.core import run\n\ndef handle():\n"
            "    return run(1)\n"
        ),
        "pkg/service.py": "from pkg.api import handle\n",
        "tests/test_core.py": "from pkg.core import run\n",
    }


def test_arch_index_captures_imports_and_reverse_edges() -> None:
    arch = build_arch_index(_sources())

    assert "pkg/core.py" in arch.files
    assert "pkg.core" in arch.module_importers
    assert "pkg.api" in arch.module_importers["pkg.core"]
    assert "pkg/api.py" in arch.importers["pkg.core"]
    assert "tests/test_core.py" in arch.importers["pkg.core"]
    assert arch.tests == ["tests/test_core.py"]
    assert any(item["path"] == "pkg/core.py" for item in arch.hotspots)


def test_arch_brief_is_bounded_and_actionable() -> None:
    arch = build_arch_index(_sources())
    text = arch_brief(arch, max_chars=500)

    assert "[架构简报]" in text
    assert "高被依赖文件" in text
    assert len(text) <= 520


def test_impact_analysis_follows_callers_and_tests() -> None:
    arch = build_arch_index(_sources())

    impact = impact_analysis(arch, "修改 run 的边界条件", max_modules=10)

    assert "pkg/core.py" in impact["seed_files"]
    assert "pkg.core" in impact["seed_modules"]
    assert "pkg/api.py" in impact["affected_files"]
    assert "pkg/service.py" in impact["affected_files"]
    assert "tests/test_core.py" in impact["affected_tests"]
    assert impact["fallback"] is False


def test_call_graph_finds_cross_language_callers() -> None:
    arch = build_arch_index(
        {
            "web/util.ts": "export function formatTotal(v) { return v; }",
            "web/view.ts": (
                "export function render(v) { return formatTotal(v); }"
            ),
            "svc/main.go": "func run(v int) { formatTotal(v) }",
        }
    )

    edges = {
        (edge["src_file"], edge["dst_file"]) for edge in arch.calls
    }
    assert ("web/view.ts", "web/util.ts") in edges
    assert ("svc/main.go", "web/util.ts") in edges

    impact = impact_analysis(arch, "修改 formatTotal", max_modules=10)
    assert "web/util.ts" in impact["seed_files"]
    assert "web/view.ts" in impact["affected_files"]
    assert "svc/main.go" in impact["affected_files"]
    assert impact["affected_calls"]


def test_call_graph_resolves_getattr_and_macros() -> None:
    arch = build_arch_index(
        {
            "pkg/target.py": "def target():\n    return 1\n",
            "pkg/use.py": (
                "def use(obj):\n"
                "    return getattr(obj, 'target')()\n"
            ),
            "lib/helper.rs": "pub fn traced() {}\n",
            "lib/main.rs": "fn run() { traced!(); }\n",
        }
    )

    edges = {
        (edge["src_file"], edge["dst_file"], edge["dst_symbol"])
        for edge in arch.calls
    }
    assert ("pkg/use.py", "pkg/target.py", "target") in edges
    assert ("lib/main.rs", "lib/helper.rs", "traced") in edges
    assert any(edge["dynamic"] for edge in arch.calls)


def test_macro_definitions_create_macro_edges() -> None:
    arch = build_arch_index(
        {
            "src/lib.rs": "macro_rules! trace { () => {} }\n",
            "src/main.rs": "fn run() { trace!(); }\n",
        }
    )

    assert any(
        edge["kind"] == "macro" and edge["dst_symbol"] == "trace"
        for edge in arch.calls
    )
    assert any(
        "macro trace" in symbols for symbols in arch.symbols.values()
    )


def test_lsp_resolution_turns_dynamic_calls_into_edges() -> None:
    class _Client:
        def __init__(self) -> None:
            self.opened: list[str] = []

        def did_open(self, path: str, text: str) -> None:
            self.opened.append(path)

        def definition(self, path: str, line: int, character: int):
            return [("/ws/target.py", 2, 1)]

    client = _Client()
    edges = resolve_dynamic_calls_with_lsp(
        client,
        {"caller.py": "def run():\n    eval('x')\n"},
        [{"file": "caller.py", "line": 2, "kind": "eval"}],
        root="/ws",
    )

    assert edges
    assert client.opened == ["/ws/caller.py"]
    assert edges[0]["dst_file"] == "target.py"
    assert edges[0]["kind"] == "lsp"
    assert edges[0]["dynamic"] is True


def test_lsp_edges_extend_impact_files() -> None:
    arch = build_arch_index(
        {
            "target.py": "def target():\n    return 1\n",
            "caller.py": "def run():\n    return 1\n",
        }
    )
    arch.calls.append(
        {
            "src_file": "caller.py",
            "src_symbol": "",
            "dst_file": "target.py",
            "dst_symbol": "",
            "line": 2,
            "kind": "lsp",
            "confidence": "lsp",
            "dynamic": True,
        }
    )

    impact = impact_analysis(arch, "修改 target", max_modules=10)

    assert "caller.py" in impact["affected_files"]


def test_dynamic_calls_near_change_force_fallback() -> None:
    arch = build_arch_index(
        {
            "a.py": (
                "def run():\n"
                "    return eval('1 + 1')\n"
            )
        }
    )

    impact = impact_analysis(arch, "修改 run", max_modules=10)

    assert impact["fallback"] is True
    assert impact["dynamic_call_count"] >= 1
    assert "动态" in impact["reason"]


def test_call_graph_skips_ambiguous_symbols() -> None:
    arch = build_arch_index(
        {
            "a.py": "def helper():\n    return 1\n",
            "b.py": "def helper():\n    return 2\n",
            "c.py": "def use():\n    return helper()\n",
        }
    )

    assert not any(
        edge["dst_symbol"] == "helper" and edge["src_file"] == "c.py"
        for edge in arch.calls
    )


def test_change_contract_orders_interfaces_before_callers() -> None:
    arch = build_arch_index(_sources())

    contract = build_change_contract(
        arch, "修改 run 的边界条件", max_modules=10
    )

    assert contract["seed_files"] == ["pkg/core.py"]
    assert contract["waves"][0]["files"] == ["pkg/core.py"]
    assert any(
        item["name"] == "run" and item["file"] == "pkg/core.py"
        for item in contract["interfaces"]
    )
    later = {
        path
        for wave in contract["waves"][1:]
        for path in wave["files"]
    }
    assert "pkg/api.py" in later
    assert "tests/test_core.py" in contract["affected_tests"]


def test_apply_change_contract_stages_implement_tasks() -> None:
    arch = build_arch_index(_sources())
    contract = build_change_contract(
        arch, "修改 run 的边界条件", max_modules=10
    )
    tasks = [
        {
            "id": "core",
            "role": "implement",
            "write_scope": ["pkg/core.py"],
            "stage": 0,
        },
        {
            "id": "api",
            "role": "implement",
            "write_scope": ["pkg/api.py"],
            "stage": 0,
        },
    ]

    changed = apply_change_contract(tasks, contract, max_stages=4)

    assert changed == 1
    assert tasks[0]["stage"] == 0
    assert tasks[1]["stage"] >= 1


def test_apply_change_contract_is_a_noop_without_waves() -> None:
    tasks = [
        {
            "id": "core",
            "role": "implement",
            "write_scope": ["pkg/core.py"],
            "stage": 0,
        }
    ]

    assert apply_change_contract(tasks, {}, max_stages=4) == 0
    assert tasks[0]["stage"] == 0


def test_impact_analysis_marks_sampled_repo_as_fallback() -> None:
    arch = build_arch_index(
        _sources(),
        sample={
            "languages": {"python": 5},
            "sampled": 5,
            "total_files": 500,
            "truncated": True,
        },
    )

    impact = impact_analysis(arch, "修改 run", max_modules=10)

    assert impact["fallback"] is True
    assert "抽样" in impact["reason"]


def test_impact_analysis_marks_unparsed_as_fallback() -> None:
    sources = _sources()
    sources["pkg/broken.py"] = "def broken(:\n"
    arch = build_arch_index(sources)

    impact = impact_analysis(arch, "改 pkg/broken.py", max_modules=10)

    assert impact["fallback"] is True
    assert "无法静态" in impact["reason"]
