"""Python AST 索引：import 图、测试影响分析与轻量 repo map。

索引在宿主侧构建，源码由**一次**沙箱调用收集（避免每个文件一次 wsl.exe）。
这里只做静态近似：包根、非字面量动态 import、条件 import 都可能让结果
偏保守；无法可靠建图的改动会回退到全量验证。真正的 lint/类型信息仍留给
LSP（尚未接入）。
"""

from __future__ import annotations

import ast
from collections.abc import Iterable
from dataclasses import dataclass, field

from ubismart.sandbox.fs import SandboxFs, SandboxFsError
from ubismart.sandbox.pathguard import SandboxPathError
from ubismart.sandbox.wsl_exec import WslSandbox

_IGNORED_DIRS = {
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
}
_PACKAGE_ROOTS = {"src", "lib"}


@dataclass(slots=True)
class PythonIndex:
    # path -> 该文件 import 的模块名（绝对/相对已归一化）
    imports: dict[str, frozenset[str]] = field(default_factory=dict)
    # module name -> 文件 path（一个模块可能有 src/pkg 与 pkg 两种写法）
    module_files: dict[str, set[str]] = field(default_factory=dict)
    # path -> 顶层符号摘要
    symbols: dict[str, list[str]] = field(default_factory=dict)
    tests: set[str] = field(default_factory=set)
    # 语法错误等无法静态解析的文件；影响分析遇到它们必须回退全量。
    unparsed: set[str] = field(default_factory=set)
    # 含非字面量动态导入的文件；同样无法可靠建图，回退全量。
    dynamic: set[str] = field(default_factory=set)

    def module_candidates(self, path: str) -> set[str]:
        return set(_module_candidates(path))

    def modules_for_paths(self, paths: Iterable[str]) -> set[str]:
        result: set[str] = set()
        for path in paths:
            result.update(_module_candidates(path))
        return result

    def importers_of(self, modules: set[str]) -> set[str]:
        found: set[str] = set()
        for path, imported in self.imports.items():
            if any(_import_matches(item, modules) for item in imported):
                found.add(path)
        return found


def _module_candidates(path: str) -> list[str]:
    text = str(path).replace("\\", "/").lstrip("./")
    if not text.endswith(".py"):
        return []
    parts = text[:-3].split("/")
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    candidates = [".".join(parts)]
    if parts[0] in _PACKAGE_ROOTS and len(parts) > 1:
        candidates.append(".".join(parts[1:]))
    # 去重但保持顺序
    seen: set[str] = set()
    result: list[str] = []
    for candidate in candidates:
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)
    return result


def _package_of(candidate: str, path: str) -> str:
    if path.replace("\\", "/").endswith("/__init__.py") or path == "__init__.py":
        return candidate
    return candidate.rsplit(".", 1)[0] if "." in candidate else ""


def _resolve_relative(candidate: str, path: str, level: int, module: str | None) -> str:
    package = _package_of(candidate, path)
    parts = [part for part in package.split(".") if part]
    # level=1 表示当前包，level=2 表示上一级
    up = max(level - 1, 0)
    if up:
        parts = parts[:-up] if up <= len(parts) else []
    if module:
        parts.extend(module.split("."))
    return ".".join(parts)


def _imports_of(tree: ast.AST, path: str) -> set[str]:
    candidates = _module_candidates(path)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                for candidate in candidates:
                    base = _resolve_relative(
                        candidate, path, node.level, node.module
                    )
                    if base:
                        found.add(base)
                        for alias in node.names:
                            if alias.name != "*":
                                found.add(f"{base}.{alias.name}")
            elif node.module:
                found.add(node.module)
                for alias in node.names:
                    if alias.name != "*":
                        found.add(f"{node.module}.{alias.name}")
    return found


def _dynamic_import_targets(tree: ast.AST) -> tuple[set[str], bool]:
    """收集可静态确定的动态导入；无法确定目标时标记为不可靠。

    `importlib.import_module("a.b")` / `__import__("a.b")` 的字面量参数
    能直接补进 import 图；变量、f-string、函数返回值等一律视为不可靠，
    由调用方回退到全量验证。
    """
    targets: set[str] = set()
    unknown = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        else:
            continue
        if name not in {"import_module", "__import__"}:
            continue
        argument = node.args[0] if node.args else None
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            targets.add(argument.value)
        else:
            unknown = True
    return targets, unknown


def _symbols_of(tree: ast.AST) -> list[str]:
    symbols: list[str] = []
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.ClassDef):
            symbols.append(f"class {node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else "def "
            symbols.append(f"{prefix}{node.name}")
    return symbols


def _is_test_file(path: str) -> bool:
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py")


def parse_python_source(path: str, source: str) -> dict[str, object]:
    """解析单个文件；结果可直接缓存，供增量索引复用。"""
    normalized = str(path).replace("\\", "/").lstrip("./")
    entry: dict[str, object] = {
        "path": normalized,
        "imports": [],
        "symbols": [],
        "dynamic": False,
        "unparsed": False,
        "test": _is_test_file(normalized),
    }
    try:
        tree = ast.parse(source)
    except SyntaxError:
        entry["unparsed"] = True
        return entry
    dynamic_targets, unknown_dynamic = _dynamic_import_targets(tree)
    entry["imports"] = sorted(
        _imports_of(tree, normalized) | dynamic_targets
    )
    entry["symbols"] = _symbols_of(tree)
    entry["dynamic"] = unknown_dynamic
    return entry


def build_index_from_entries(
    entries: Iterable[dict[str, object]],
) -> PythonIndex:
    index = PythonIndex()
    for raw in entries:
        normalized = str(raw.get("path", ""))
        if not normalized:
            continue
        index.imports[normalized] = frozenset(
            str(item) for item in (raw.get("imports") or [])
        )
        index.symbols[normalized] = [
            str(item) for item in (raw.get("symbols") or [])
        ]
        if bool(raw.get("unparsed")):
            index.unparsed.add(normalized)
        if bool(raw.get("dynamic")):
            index.dynamic.add(normalized)
        for candidate in _module_candidates(normalized):
            index.module_files.setdefault(candidate, set()).add(normalized)
        if bool(raw.get("test")) or _is_test_file(normalized):
            index.tests.add(normalized)
    return index


def build_index(sources: dict[str, str]) -> PythonIndex:
    return build_index_from_entries(
        parse_python_source(path, source)
        for path, source in sources.items()
    )


def _import_matches(imported: str, modules: set[str]) -> bool:
    for module in modules:
        if imported == module:
            return True
        # import app 会影响 app.foo；from app.foo import x 会被解析成 app.foo
        if imported.startswith(module + ".") or module.startswith(imported + "."):
            return True
    return False


def related_test_files(
    index: PythonIndex,
    changed: Iterable[str],
    *,
    limit: int = 20,
) -> list[str]:
    """反向依赖闭包 + 文件名启发式，返回相关测试文件。"""
    changed_paths = {
        str(path).replace("\\", "/").lstrip("./")
        for path in changed
        if str(path).endswith(".py")
    }
    if not changed_paths:
        return []

    # 只要有一个改动文件无法可靠建图，就放弃子集选择。宁可多跑全量，
    # 也不能让「影响分析漏判」表现成一条绿色的子集验证。
    if any(
        path in index.unparsed
        or path in index.dynamic
        or path not in index.imports
        for path in changed_paths
    ):
        return []

    selected: set[str] = set()
    # 1) 改的本身就是测试
    selected.update(path for path in changed_paths if path in index.tests)

    # 2) 文件名启发式（先给一个稳定的兜底）
    stems = {
        path.rsplit("/", 1)[-1][:-3]
        for path in changed_paths
        if path.rsplit("/", 1)[-1].endswith(".py")
    }
    for test in index.tests:
        name = test.rsplit("/", 1)[-1]
        if name in {f"test_{stem}.py" for stem in stems} or name in {
            f"{stem}_test.py" for stem in stems
        }:
            selected.add(test)

    # 3) 反向依赖闭包：谁 import 了改动模块，谁就是受影响文件
    impacted: set[str] = set(changed_paths)
    frontier = index.modules_for_paths(changed_paths)
    while frontier:
        importers = index.importers_of(frontier) - impacted
        if not importers:
            break
        impacted |= importers
        frontier = index.modules_for_paths(importers)

    selected.update(path for path in impacted if path in index.tests)
    return sorted(selected)[:limit]


def build_repo_map(
    index: PythonIndex, *, max_chars: int = 4_000
) -> str:
    """压缩成「文件: 顶层符号」的 repo map，供 planner/worker 参考。"""
    lines: list[str] = []
    for path in sorted(index.symbols):
        symbols = index.symbols[path]
        if not symbols:
            continue
        suffix = " …" if len(symbols) > 8 else ""
        lines.append(f"{path}: " + ", ".join(symbols[:8]) + suffix)
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n…（repo map 已截断）"
    return text


def collect_python_sources(
    sandbox: WslSandbox,
    root: str,
    *,
    max_files: int = 2_000,
    max_bytes: int = 1_000_000,
    max_total_bytes: int = 32_000_000,
) -> dict[str, str]:
    """一次沙箱调用收集 .py 源码；失败返回空 dict。

    遍历与读取都交给 A4 的 `SandboxFs.collect_sources`：索引是只读动作，
    没必要为了它在这个模块里握一条"跑任意脚本"的通道（TD-034）。
    上限语义不变 —— 达到上限返回**部分**索引，影响分析偏保守但不会拖垮进程。
    """
    try:
        return SandboxFs(sandbox, root).collect_sources(
            (".py",),
            exclude_dirs=_IGNORED_DIRS,
            max_files=max_files,
            max_file_bytes=max_bytes,
            max_total_bytes=max_total_bytes,
        )
    except (SandboxFsError, SandboxPathError):
        return {}
