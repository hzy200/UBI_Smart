"""代码架构索引、简报与确定性影响分析。

数据来源是已有的 `PythonIndex`（AST import 图 + 顶层符号 + 测试文件），
不引入新的解析器。产物是**确定性文本/字典**，可直接进入规划提示；
不依赖模型，因此可以先验证“看到了什么”，再决定要不要让模型总结。
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from ubismart.codeintel.python_index import PythonIndex, build_index
from ubismart.knowledge.callgraph import (
    build_call_graph,
    detect_dynamic_calls,
    extract_macro_defs,
)

_ENTRY_NAMES = {"main.py", "app.py", "cli.py", "__main__.py"}
_FILE_RE = re.compile(
    r"[A-Za-z0-9_./-]+\.(?:py|pyi|ts|tsx|js|jsx|go|rs|java|kt|"
    r"c|h|cpp|hpp|md|toml|yaml|yml)\b",
    re.IGNORECASE,
)
_SYMBOL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")


@dataclass(slots=True)
class ArchIndex:
    files: list[str] = field(default_factory=list)
    symbols: dict[str, list[str]] = field(default_factory=dict)
    imports: dict[str, list[str]] = field(default_factory=dict)
    # module -> 直接 import 它的文件
    importers: dict[str, list[str]] = field(default_factory=dict)
    # module -> 直接 import 它的模块（影响分析用，避免反复扫文件）
    module_importers: dict[str, list[str]] = field(default_factory=dict)
    # module -> 定义它的文件
    module_files: dict[str, list[str]] = field(default_factory=dict)
    # file -> 该文件可归属的模块名
    file_modules: dict[str, list[str]] = field(default_factory=dict)
    tests: list[str] = field(default_factory=list)
    entrypoints: list[str] = field(default_factory=list)
    hotspots: list[dict[str, Any]] = field(default_factory=list)
    unparsed: list[str] = field(default_factory=list)
    dynamic: list[str] = field(default_factory=list)
    languages: dict[str, int] = field(default_factory=dict)
    sample: dict[str, Any] = field(default_factory=dict)
    # 跨语言调用边（轻量、按唯一符号名解析）。
    calls: list[dict[str, Any]] = field(default_factory=list)
    # 反射/动态 import/eval/宏等无法静态确定目标的调用。
    dynamic_calls: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "files": self.files,
            "symbols": self.symbols,
            "imports": self.imports,
            "importers": self.importers,
            "module_importers": self.module_importers,
            "module_files": self.module_files,
            "file_modules": self.file_modules,
            "tests": self.tests,
            "entrypoints": self.entrypoints,
            "hotspots": self.hotspots,
            "unparsed": self.unparsed,
            "dynamic": self.dynamic,
            "languages": self.languages,
            "sample": self.sample,
            "calls": self.calls,
            "dynamic_calls": self.dynamic_calls,
        }


def _entrypoints(sources: dict[str, str]) -> list[str]:
    found: list[str] = []
    for path, text in sources.items():
        name = path.rsplit("/", 1)[-1]
        if name in _ENTRY_NAMES or "__main__" in text:
            found.append(path)
    return sorted(found)


def _hotspots(index: PythonIndex, sources: dict[str, str]) -> list[dict[str, Any]]:
    counts: Counter[str] = Counter()
    for module, files in index.module_files.items():
        importers = index.importers_of({module})
        for path in files:
            counts[path] = max(counts[path], len(importers))
    ordered = sorted(
        (
            (path, count)
            for path, count in counts.items()
            if path in sources and count > 0
        ),
        key=lambda item: (-item[1], item[0]),
    )
    return [
        {"path": path, "importers": count} for path, count in ordered[:10]
    ]


def build_arch_index(
    sources: dict[str, str],
    *,
    index: PythonIndex | None = None,
    sample: dict[str, Any] | None = None,
) -> ArchIndex:
    """从源码构建架构索引；`index` 可复用已建好的 AST 索引。

    非 Python 文件用轻量正则提符号；`sample` 来自多语言抽样，原样带入产物。
    """
    from ubismart.knowledge.polyglot import (
        extract_symbols,
        language_of,
    )

    python_sources = {
        path: text
        for path, text in sources.items()
        if str(path).lower().endswith((".py", ".pyi"))
    }
    index = index or build_index(python_sources)
    importers: dict[str, list[str]] = {}
    for module in sorted(index.module_files):
        # 只对实际存在的模块做反查，避免把外部 import 也列进来。
        importers[module] = sorted(index.importers_of({module}))
    reverse: dict[str, set[str]] = {}
    for path, imported_modules in index.imports.items():
        source_modules = index.module_candidates(path)
        for target in imported_modules:
            for source in source_modules:
                reverse.setdefault(target, set()).add(source)
    symbols: dict[str, list[str]] = {
        path: list(names)
        for path, names in sorted(index.symbols.items())
    }
    for path, text in sources.items():
        extracted = extract_symbols(path, text)
        if extracted:
            symbols[path] = extracted
    for item in extract_macro_defs(sources):
        path = str(item["file"])
        rendered = f"macro {item['name']}"
        declared = symbols.setdefault(path, [])
        if rendered not in declared:
            declared.append(rendered)
    languages: dict[str, int] = {}
    for path in sources:
        language = language_of(path)
        languages[language] = languages.get(language, 0) + 1
    return ArchIndex(
        files=sorted(sources),
        symbols=dict(sorted(symbols.items())),
        imports={
            path: sorted(imported)
            for path, imported in sorted(index.imports.items())
        },
        importers=importers,
        module_importers={
            module: sorted(callers)
            for module, callers in sorted(reverse.items())
        },
        module_files={
            module: sorted(files)
            for module, files in sorted(index.module_files.items())
        },
        file_modules={
            path: index.module_candidates(path) for path in sorted(sources)
        },
        tests=sorted(index.tests),
        entrypoints=_entrypoints(sources),
        hotspots=_hotspots(index, sources),
        unparsed=sorted(index.unparsed),
        dynamic=sorted(index.dynamic),
        languages=languages,
        sample=dict(sample or {}),
        calls=build_call_graph(sources),
        dynamic_calls=detect_dynamic_calls(sources),
    )


def arch_brief(arch: ArchIndex, *, max_chars: int = 4_000) -> str:
    """把 ArchIndex 压成规划提示可读的确定性简报。"""
    dirs: Counter[str] = Counter()
    for path in arch.files:
        top = path.split("/", 1)[0] if "/" in path else "."
        dirs[top] += 1
    lines = [
        "[架构简报]",
        f"文件：{len(arch.files)}；测试：{len(arch.tests)}；"
        f"入口：{len(arch.entrypoints)}",
    ]
    if arch.entrypoints:
        lines.append("入口：" + "、".join(arch.entrypoints[:8]))
    if dirs:
        lines.append(
            "子系统："
            + "、".join(
                f"{name}({count})" for name, count in dirs.most_common(8)
            )
        )
    if arch.calls:
        lines.append(f"调用边：{len(arch.calls)}（轻量解析）")
    if arch.dynamic_calls:
        lines.append(
            f"动态/反射调用：{len(arch.dynamic_calls)}（影响分析需保守）"
        )
    if arch.hotspots:
        lines.append("高被依赖文件：")
        lines.extend(
            f"- {item['path']}（{item['importers']} 个 importer）"
            for item in arch.hotspots[:6]
        )
    risky = sorted(set(arch.unparsed) | set(arch.dynamic))
    if risky:
        lines.append("无法静态建图的文件（影响分析需保守）：")
        lines.append("- " + "、".join(risky[:8]))
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（架构简报已截断）"
    return text


def _request_files(request: str) -> list[str]:
    found = []
    for raw in _FILE_RE.findall(request):
        path = raw.replace("\\", "/").lstrip("./")
        if path and path not in found:
            found.append(path)
    return found


def _symbol_seeds(arch: ArchIndex, request: str) -> list[str]:
    tokens = set(_SYMBOL_RE.findall(request))
    if not tokens:
        return []
    found: list[str] = []
    for path, names in arch.symbols.items():
        declared: set[str] = set()
        for name in names:
            text = str(name).strip()
            for prefix in ("async def ", "def ", "class ", "macro "):
                if text.startswith(prefix):
                    text = text[len(prefix) :]
                    break
            declared.add(text.split("(")[0].split(":")[0].strip())
        if declared & tokens and path not in found:
            found.append(path)
    return found


def _symbol_name(raw: str) -> str:
    text = str(raw).strip()
    for prefix in ("async def ", "def ", "class ", "macro "):
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    return text.split("(")[0].split(":")[0].strip()


def _normalize_path(path: str) -> str:
    text = str(path or "").strip().replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text.rstrip("/")


def _contract_interfaces(
    arch: ArchIndex, request: str, seed_files: list[str]
) -> list[dict[str, Any]]:
    tokens = set(_SYMBOL_RE.findall(request))
    interfaces: list[dict[str, Any]] = []
    for path in seed_files:
        for raw in arch.symbols.get(path, []):
            name = _symbol_name(raw)
            if not name or name.startswith("_"):
                continue
            if tokens and name not in tokens:
                continue
            callers: list[str] = []
            for module in arch.file_modules.get(path, []):
                callers.extend(arch.module_importers.get(module, []))
            interfaces.append(
                {
                    "name": name,
                    "file": path,
                    "callers": sorted(set(callers))[:20],
                }
            )
    return interfaces[:40]


def _contract_waves(
    arch: ArchIndex,
    seed_modules: list[str],
    seed_files: list[str],
    gate_tests: list[str],
    max_waves: int,
) -> list[dict[str, Any]]:
    """seed 模块为第 0 波，随后按反向 import 逐层展开。"""
    cap = max(1, int(max_waves))
    waves: list[dict[str, Any]] = []
    seen: set[str] = set()
    frontier = set(seed_modules)
    while frontier and len(waves) < cap:
        layer = sorted(frontier - seen)
        seen.update(layer)
        files = sorted(
            {
                path
                for module in layer
                for path in arch.module_files.get(module, [])
            }
        )
        if not files and layers_should_add_seed(waves, seed_files):
            files = list(seed_files)
        if files or layer:
            waves.append(
                {
                    "stage": len(waves),
                    "modules": layer,
                    "files": files,
                    "gate_tests": list(gate_tests)[:40],
                }
            )
        next_frontier: set[str] = set()
        for module in layer:
            next_frontier.update(arch.module_importers.get(module, []))
        frontier = next_frontier - seen
    if not waves and seed_files:
        waves.append(
            {
                "stage": 0,
                "modules": [],
                "files": list(seed_files),
                "gate_tests": list(gate_tests)[:40],
            }
        )
    return waves


def _merge_uncovered_waves(
    waves: list[dict[str, Any]],
    affected_files: list[str],
    gate_tests: list[str],
    max_waves: int,
) -> None:
    """调用图发现的跨语言调用方若不在 import 波次里，补到最后一波（或新波）。"""
    covered = {
        str(path)
        for wave in waves
        for path in wave.get("files") or []
    }
    uncovered = [
        str(path)
        for path in affected_files
        if str(path) not in covered
    ]
    if not uncovered:
        return
    if len(waves) < max(1, int(max_waves)):
        waves.append(
            {
                "stage": len(waves),
                "modules": [],
                "files": uncovered[:200],
                "gate_tests": list(gate_tests)[:40],
            }
        )
    else:
        waves[-1]["files"] = sorted(
            set(waves[-1].get("files") or []) | set(uncovered)
        )[:200]


def build_change_contract(
    arch: ArchIndex,
    request: str,
    *,
    max_modules: int = 40,
    max_waves: int = 4,
) -> dict[str, Any]:
    """生成确定性的跨模块变更契约（接口 + 文件 + 波次 + 受影响测试）。"""
    impact = impact_analysis(
        arch, request, max_modules=max_modules
    )
    seed_modules = list(impact["seed_modules"])
    seed_files = list(impact["seed_files"])
    interfaces = _contract_interfaces(arch, request, seed_files)
    waves = _contract_waves(
        arch,
        seed_modules,
        seed_files,
        impact["affected_tests"],
        max_waves,
    )
    _merge_uncovered_waves(
        waves, impact["affected_files"], impact["affected_tests"], max_waves
    )

    return {
        "request": str(request or "")[:500],
        "seed_files": seed_files,
        "seed_modules": seed_modules,
        "affected_files": list(impact["affected_files"]),
        "affected_modules": list(impact["affected_modules"]),
        "affected_tests": list(impact["affected_tests"]),
        "interfaces": interfaces,
        "waves": waves,
        "fallback": bool(impact["fallback"]),
        "reason": str(impact["reason"] or ""),
    }


def layers_should_add_seed(
    waves: list[dict[str, Any]], seed_files: list[str]
) -> bool:
    return not waves and bool(seed_files)


def _build_stage_maps(
    waves: list[dict[str, Any]], cap: int
) -> tuple[dict[str, int], dict[str, int]]:
    file_stage: dict[str, int] = {}
    module_stage: dict[str, int] = {}
    for wave in waves:
        try:
            stage = min(cap - 1, max(0, int(wave.get("stage", 0) or 0)))
        except (TypeError, ValueError):
            stage = 0
        for path in wave.get("files") or []:
            file_stage.setdefault(_normalize_path(str(path)), stage)
        for module in wave.get("modules") or []:
            module_stage.setdefault(str(module), stage)
    return file_stage, module_stage


def _scope_stages(
    scope: Any,
    file_stage: dict[str, int],
    module_stage: dict[str, int],
) -> list[int]:
    normalized = _normalize_path(str(scope))
    if not normalized:
        return []
    matched: list[int] = []
    if normalized in file_stage:
        matched.append(file_stage[normalized])
    prefix = normalized + "/"
    for path, stage in file_stage.items():
        if path.startswith(prefix):
            matched.append(stage)
    for module, stage in module_stage.items():
        if normalized == module or normalized.startswith(module + "/"):
            matched.append(stage)
    return matched


def _task_stage(
    task: dict[str, Any],
    file_stage: dict[str, int],
    module_stage: dict[str, int],
    cap: int,
) -> int | None:
    matched: list[int] = []
    for scope in task.get("write_scope") or []:
        matched.extend(_scope_stages(scope, file_stage, module_stage))
    if not matched:
        return None
    return min(cap - 1, max(matched))


def apply_change_contract(
    tasks: list[dict[str, Any]],
    contract: dict[str, Any],
    *,
    max_stages: int = 4,
) -> int:
    """按契约波次给 implement 任务分 stage；返回被调整的任务数。

    只做确定性的“写到哪个波次”，不增删任务、不改依赖；同 stage 的
    写范围冲突仍交给 `validate_tasks` 拒绝。
    """
    waves = contract.get("waves") or []
    if not waves:
        return 0
    cap = max(1, int(max_stages))
    file_stage, module_stage = _build_stage_maps(waves, cap)

    changed = 0
    for task in tasks:
        if str(task.get("role", "")) != "implement":
            continue
        stage = _task_stage(task, file_stage, module_stage, cap)
        if stage is None:
            continue
        if int(task.get("stage", 0) or 0) != stage:
            task["stage"] = stage
            changed += 1
    return changed


def _expand_import_closure(
    arch: ArchIndex,
    seed_modules: set[str],
    affected_files: set[str],
    affected_modules: set[str],
    limit: int,
) -> None:
    frontier = set(seed_modules)
    while frontier and len(affected_modules) < limit:
        next_frontier: set[str] = set()
        for module in frontier:
            for caller in arch.module_importers.get(module, []):
                if caller in affected_modules:
                    continue
                affected_modules.add(caller)
                next_frontier.add(caller)
                affected_files.update(arch.module_files.get(caller, []))
                if len(affected_modules) >= limit:
                    break
            if len(affected_modules) >= limit:
                break
        frontier = next_frontier


def _seed_symbol_names(
    arch: ArchIndex,
    seeds: list[str],
    explicit: list[str],
    tokens: set[str],
) -> set[str]:
    """跨语言调用图的起点符号（显式文件或请求里提到的符号）。"""
    explicit_set = set(explicit)
    seed_symbol_names: set[str] = set()
    for path in seeds:
        for raw in arch.symbols.get(path, []):
            name = _symbol_name(raw)
            if not name or name.startswith("_"):
                continue
            if path in explicit_set or not tokens or name in tokens:
                seed_symbol_names.add(name)
    return seed_symbol_names


def _callers_by_symbol(
    calls: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    mapping: dict[str, list[dict[str, Any]]] = {}
    for edge in calls:
        mapping.setdefault(str(edge.get("dst_symbol", "")), []).append(edge)
    return mapping


def _expand_call_closure(
    arch: ArchIndex,
    seed_symbol_names: set[str],
    affected_files: set[str],
    affected_modules: set[str],
    affected_calls: list[dict[str, Any]],
    limit: int,
) -> None:
    """沿反向 call 边继续扩影响面（按唯一符号名解析）。"""
    callers_by_symbol = _callers_by_symbol(arch.calls)
    queue = list(seed_symbol_names)
    visited_symbols = set(queue)
    while queue and len(affected_files) < limit:
        name = queue.pop(0)
        for edge in callers_by_symbol.get(name, []):
            affected_calls.append(edge)
            source = str(edge.get("src_file", ""))
            if source:
                affected_files.add(source)
                for module in arch.file_modules.get(source, []):
                    affected_modules.add(module)
            caller_symbol = str(edge.get("src_symbol", ""))
            if caller_symbol and caller_symbol not in visited_symbols:
                visited_symbols.add(caller_symbol)
                queue.append(caller_symbol)
            if len(affected_files) >= limit:
                break


def _affected_tests(
    arch: ArchIndex,
    affected_files: set[str],
    affected_modules: set[str],
) -> list[str]:
    affected_tests: list[str] = []
    for test in arch.tests:
        if any(stem in test for stem in affected_files):
            affected_tests.append(test)
            continue
        imports = arch.imports.get(test, [])
        if any(
            module in affected_modules
            or any(module.startswith(item + ".") for item in affected_modules)
            for module in imports
        ):
            affected_tests.append(test)
    return affected_tests


def _lsp_callers_by_file(
    calls: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    mapping: dict[str, list[dict[str, Any]]] = {}
    for edge in calls:
        if edge.get("kind") == "lsp" and not edge.get("dst_symbol"):
            mapping.setdefault(str(edge.get("dst_file", "")), []).append(edge)
    return mapping


def _expand_lsp_closure(
    arch: ArchIndex,
    affected_files: set[str],
    affected_calls: list[dict[str, Any]],
    limit: int,
) -> None:
    """LSP 边是文件级 definition：沿反向文件边继续扩调用方。"""
    lsp_callers_by_file = _lsp_callers_by_file(arch.calls)
    changed = True
    while changed and len(affected_files) < limit:
        changed = False
        for path in list(affected_files):
            for edge in lsp_callers_by_file.get(path, []):
                source = str(edge.get("src_file", ""))
                if source and source not in affected_files:
                    affected_files.add(source)
                    affected_calls.append(edge)
                    changed = True
                if len(affected_files) >= limit:
                    break
            if len(affected_files) >= limit:
                break


def _nearby_dynamic_calls(
    arch: ArchIndex,
    seeds: list[str],
    affected_files: set[str],
) -> list[dict[str, Any]]:
    return [
        item
        for item in arch.dynamic_calls
        if not item.get("resolved")
        and (
            str(item.get("file", "")) in seeds
            or str(item.get("file", "")) in affected_files
        )
    ]


def _fallback_reason(
    arch: ArchIndex,
    sampled_incomplete: bool,
    nearby_dynamic: list[dict[str, Any]],
) -> tuple[bool, str]:
    fallback = bool(
        arch.unparsed
        or arch.dynamic
        or sampled_incomplete
        or nearby_dynamic
    )
    reasons = []
    if arch.unparsed or arch.dynamic:
        reasons.append("存在无法静态解析/动态 import 的文件")
    if sampled_incomplete:
        reasons.append("仓库过大，架构索引为抽样结果")
    if nearby_dynamic:
        reasons.append("变更附近存在动态/反射/宏调用")
    return fallback, "；".join(reasons)


def impact_analysis(
    arch: ArchIndex,
    request: str,
    *,
    max_modules: int = 40,
) -> dict[str, Any]:
    """确定性影响分析：显式文件/符号 → 反向 import 闭包 → 受影响测试。

    无法静态建图（unparsed/dynamic）时返回 `fallback=True`，调用方应保守
    扩大范围或回退全量验证，而不是假装影响面很小。
    """
    explicit = _request_files(request)
    tokens = set(_SYMBOL_RE.findall(request))
    seeds = list(dict.fromkeys([*explicit, *_symbol_seeds(arch, request)]))
    seed_modules: set[str] = set()
    for path in seeds:
        seed_modules.update(arch.file_modules.get(path, []))
    affected_files = {path for path in seeds if path in arch.files}
    affected_modules = set(seed_modules)
    limit = max(1, int(max_modules))

    _expand_import_closure(
        arch, seed_modules, affected_files, affected_modules, limit
    )
    seed_symbol_names = _seed_symbol_names(arch, seeds, explicit, tokens)
    affected_calls: list[dict[str, Any]] = []
    _expand_call_closure(
        arch,
        seed_symbol_names,
        affected_files,
        affected_modules,
        affected_calls,
        limit,
    )
    affected_tests = _affected_tests(arch, affected_files, affected_modules)
    _expand_lsp_closure(arch, affected_files, affected_calls, limit)

    nearby_dynamic = _nearby_dynamic_calls(arch, seeds, affected_files)
    sampled_incomplete = bool((arch.sample or {}).get("truncated"))
    fallback, reason = _fallback_reason(
        arch, sampled_incomplete, nearby_dynamic
    )
    return {
        "seed_files": sorted(seeds),
        "seed_modules": sorted(seed_modules),
        "affected_files": sorted(affected_files)[:limit],
        "affected_modules": sorted(affected_modules)[:limit],
        "affected_tests": sorted(affected_tests)[:limit],
        "affected_calls": affected_calls[:200],
        "dynamic_calls": nearby_dynamic[:20],
        "dynamic_call_count": len(nearby_dynamic),
        "fallback": fallback,
        "reason": reason,
    }
