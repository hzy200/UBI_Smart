"""多语言抽样与轻量符号提取。"""

from __future__ import annotations

from typing import Any

from ubismart.config import Settings
from ubismart.knowledge import polyglot
from ubismart.knowledge.architecture import build_arch_index
from ubismart.knowledge.polyglot import extract_symbols


class _FakeFs:
    def __init__(self, sandbox: Any, root: str) -> None:
        self.calls: list[dict[str, Any]] = []
        self.counts = {"python": 8, "web": 4, "go": 2}

    def count_files(self, groups, *, exclude_dirs=()):  # noqa: ANN001
        return dict(self.counts)

    def collect_sources(
        self,
        suffixes,
        *,
        exclude_dirs=(),
        max_files=2000,
        max_file_bytes=1_000_000,
        max_total_bytes=32_000_000,
    ):  # noqa: ANN001
        self.calls.append(
            {"suffixes": tuple(suffixes), "max_files": max_files}
        )
        suffix = tuple(suffixes)[0]
        return {f"file{len(self.calls)}{suffix}": "content"}


def test_collect_arch_sources_allocates_per_language(monkeypatch) -> None:
    fake = _FakeFs(object(), "/ws")
    monkeypatch.setattr(polyglot, "SandboxFs", lambda s, r: fake)
    settings = Settings(
        _env_file=None,
        arch_max_files=14,
        arch_max_file_bytes=1000,
        arch_max_total_bytes=1_000_000,
    )

    sources, stats = polyglot.collect_arch_sources(
        object(), "/ws", settings  # type: ignore[arg-type]
    )

    assert len(fake.calls) == 3
    assert sum(call["max_files"] for call in fake.calls) <= 14
    assert stats["languages"] == {"python": 8, "web": 4, "go": 2}
    assert stats["sampled"] == len(sources)


def test_extract_symbols_per_language() -> None:
    assert "def hello" in extract_symbols(
        "a.ts", "export async function hello() {}"
    )
    assert "class Widget" in extract_symbols(
        "a.ts", "export class Widget {}"
    )
    assert "def Run" in extract_symbols("a.go", "func (s *S) Run() {}")
    assert "class Shape" in extract_symbols(
        "a.rs", "pub struct Shape { x: i32 }"
    )
    assert "class App" in extract_symbols(
        "Main.java", "public class App {}"
    )


def test_build_arch_index_includes_non_python_symbols_and_languages() -> None:
    arch = build_arch_index(
        {
            "pkg/core.py": "def run():\n    return 1\n",
            "web/app.ts": "export class View {}",
            "cmd/main.go": "func main() {}",
        },
        sample={"languages": {"python": 1, "web": 1, "go": 1}},
    )

    assert arch.symbols["web/app.ts"] == ["class View"]
    assert arch.symbols["cmd/main.go"] == ["def main"]
    assert arch.languages == {"python": 1, "web": 1, "go": 1}
    assert arch.sample["languages"]["web"] == 1
    assert "pkg.core" in arch.module_files
