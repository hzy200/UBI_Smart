"""多语言/大规模仓库的架构源码抽样。

- 先按语言组统计文件数（一次 `find`），再按比例分配 `arch_max_files` 配额；
- 只对实际存在的语言组调用 `collect_sources`，避免每组都全仓遍历；
- 非 Python 文件用轻量正则提符号；import/调用边仍只对 Python 建图。

这是**抽样**，不是全量索引：`stats.truncated=True` 时必须让上层知道
影响分析可能不完整。
"""

from __future__ import annotations

import re
from typing import Any

from ubismart.sandbox.fs import SandboxFs, SandboxFsError
from ubismart.sandbox.pathguard import SandboxPathError
from ubismart.sandbox.wsl_exec import WslSandbox

LANGUAGE_GROUPS: dict[str, tuple[str, ...]] = {
    "python": (".py", ".pyi"),
    "web": (".ts", ".tsx", ".js", ".jsx"),
    "go": (".go",),
    "rust": (".rs",),
    "jvm": (".java", ".kt"),
}
COUNT_PATTERNS: dict[str, tuple[str, ...]] = {
    "python": ("*.py", "*.pyi"),
    "web": ("*.ts", "*.tsx", "*.js", "*.jsx"),
    "go": ("*.go",),
    "rust": ("*.rs",),
    "jvm": ("*.java", "*.kt"),
}
EXCLUDES = (
    ".git",
    ".agent",
    "__pycache__",
    ".venv",
    "venv",
    "node_modules",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    "target",
)

_JS_SYMBOL = re.compile(
    r"(?:export\s+)?(?:async\s+)?(?:function|class|interface|type)\s+"
    r"([A-Za-z_$][\w$]*)"
    r"|(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="
)
_GO_SYMBOL = re.compile(
    r"func\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"
    r"|type\s+([A-Za-z_]\w*)"
)
_RUST_SYMBOL = re.compile(
    r"(?:pub\s+)?fn\s+([A-Za-z_]\w*)"
    r"|(?:pub\s+)?(?:struct|enum|trait)\s+([A-Za-z_]\w*)"
)
_JVM_SYMBOL = re.compile(
    r"(?:class|interface|object|enum)\s+([A-Za-z_]\w*)"
    r"|fun\s+([A-Za-z_]\w*)"
)


def language_of(path: str) -> str:
    lowered = str(path).lower()
    for name, suffixes in LANGUAGE_GROUPS.items():
        if lowered.endswith(suffixes):
            return name
    return "other"


def _quota(total_budget: int, counts: dict[str, int]) -> dict[str, int]:
    total = sum(counts.values())
    if total <= 0:
        return {}
    quotas: dict[str, int] = {}
    remaining = max(1, total_budget)
    names = sorted(counts)
    for index, name in enumerate(names):
        if index == len(names) - 1:
            quotas[name] = max(1, remaining)
        else:
            value = max(1, round(total_budget * counts[name] / total))
            value = min(value, max(1, remaining - (len(names) - index - 1)))
            quotas[name] = value
            remaining -= value
    return quotas


def collect_arch_sources(
    sandbox: WslSandbox, root: str, settings: Any
) -> tuple[dict[str, str], dict[str, Any]]:
    fs = SandboxFs(sandbox, root)
    max_files = max(
        1, int(getattr(settings, "arch_max_files", 2_000) or 2_000)
    )
    max_file_bytes = max(
        1_000,
        int(getattr(settings, "arch_max_file_bytes", 1_000_000) or 1_000_000),
    )
    max_total_bytes = max(
        10_000,
        int(
            getattr(settings, "arch_max_total_bytes", 32_000_000)
            or 32_000_000
        ),
    )
    try:
        counts = {
            name: int(value or 0)
            for name, value in fs.count_files(
                COUNT_PATTERNS, exclude_dirs=EXCLUDES
            ).items()
            if int(value or 0) > 0
        }
    except (SandboxFsError, SandboxPathError):
        counts = {}
    if not counts:
        try:
            sources = fs.collect_sources(
                tuple(
                    suffix
                    for suffixes in LANGUAGE_GROUPS.values()
                    for suffix in suffixes
                ),
                exclude_dirs=EXCLUDES,
                max_files=max_files,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
            )
        except (SandboxFsError, SandboxPathError):
            sources = {}
        return sources, {
            "languages": {},
            "sampled": len(sources),
            "total_files": len(sources),
            "truncated": False,
        }

    quotas = _quota(max_files, counts)
    sources: dict[str, str] = {}
    for name, quota in quotas.items():
        if len(sources) >= max_files:
            break
        suffixes = LANGUAGE_GROUPS.get(name, ())
        if not suffixes:
            continue
        byte_budget = max(
            10_000, int(max_total_bytes * quota / max_files)
        )
        try:
            part = fs.collect_sources(
                suffixes,
                exclude_dirs=EXCLUDES,
                max_files=quota,
                max_file_bytes=max_file_bytes,
                max_total_bytes=byte_budget,
            )
        except (SandboxFsError, SandboxPathError):
            continue
        for path, text in part.items():
            if len(sources) >= max_files:
                break
            sources[path] = text
    total_files = sum(counts.values())
    return sources, {
        "languages": counts,
        "sampled": len(sources),
        "total_files": total_files,
        "truncated": total_files > len(sources),
    }


def _symbol_pattern(path: str) -> re.Pattern[str] | None:
    lowered = str(path).lower()
    if lowered.endswith((".ts", ".tsx", ".js", ".jsx")):
        return _JS_SYMBOL
    if lowered.endswith(".go"):
        return _GO_SYMBOL
    if lowered.endswith(".rs"):
        return _RUST_SYMBOL
    if lowered.endswith((".java", ".kt")):
        return _JVM_SYMBOL
    return None


def extract_symbol_defs(
    path: str, text: str
) -> list[dict[str, Any]]:
    """非 Python 文件的轻量符号定义（带行号，供调用图使用）。"""
    if str(path).lower().endswith((".py", ".pyi")):
        return []
    pattern = _symbol_pattern(path)
    if pattern is None:
        return []
    defs: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for match in pattern.finditer(text):
        groups = [item for item in match.groups() if item]
        if not groups:
            continue
        name = groups[0]
        line = text.count("\n", 0, match.start()) + 1
        if (name, line) in seen:
            continue
        seen.add((name, line))
        matched = match.group(0)
        type_keyword = any(
            re.search(rf"\b{keyword}\b", matched)
            for keyword in (
                "class",
                "interface",
                "type",
                "struct",
                "enum",
                "trait",
                "object",
            )
        )
        defs.append(
            {
                "name": name,
                "line": line,
                "kind": "class" if type_keyword else "def",
            }
        )
    return defs


def extract_symbols(path: str, text: str) -> list[str]:
    """非 Python 文件的轻量符号提取（只用于定位）。"""
    symbols: list[str] = []
    for item in extract_symbol_defs(path, text):
        rendered = f"{item['kind']} {item['name']}"
        if rendered not in symbols:
            symbols.append(rendered)
    return symbols
