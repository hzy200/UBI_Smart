"""按 git 改动选择受影响测试（增量测试）。

思路：解析 `src/coding_agent` 内的 import 图，求反向依赖闭包——
改了 `sandbox/policy.py`，所有（直接或间接）import 它的模块都算受影响，
再选出 import 了这些模块的测试文件。

保守优先：依赖图无法解析、改动碰到配置/测试基础设施/脚本、或没有命中任何
测试时，回退到快反馈全量（`-m "not wsl and not llm"`）。增量只用于缩短反馈，
不能替代提交前的 `make check` 与 CI 全量。

用法：
    python scripts/select_tests.py --worktree
    python scripts/select_tests.py --base origin/main --print
    python scripts/select_tests.py --base HEAD --exec
"""

from __future__ import annotations

import argparse
import ast
import json
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = Path("src")
TEST_ROOT = Path("tests")
QUICK_MARKERS = "not wsl and not llm"

# 这些路径变化会改变收集/运行语义，无法用导入图映射，直接回退全量。
FALLBACK_EXACT = {
    "pyproject.toml",
    "Makefile",
    "tests/conftest.py",
    "tests/__init__.py",
}
FALLBACK_PREFIXES = (
    "tests/contract/",
    "tests/eval/",
    ".github/",
    "scripts/",
)
# 纯文档/指令变化不影响可执行行为。
DOC_SUFFIXES = (".md", ".txt", ".rst", ".yaml", ".yml", ".json")
# 总会被选中的契约/架构测试：分层或契约被改动时它们最该先跑。
ALWAYS_TESTS = (
    "tests/unit/test_architecture.py",
    "tests/unit/test_layer_contracts.py",
    "tests/unit/test_contract_compat.py",
    "tests/smoke/test_system_smoke.py",
)


@dataclass
class Plan:
    changed: list[str]
    tests: list[str]
    fallback: bool
    reason: str


def _module_for_path(relpath: str, src_root: Path = SRC) -> str | None:
    """`src/coding_agent/a/b.py` → `coding_agent.a.b`；非 .py 返回 None。"""
    path = Path(relpath)
    if path.suffix != ".py":
        return None
    try:
        relative = path.relative_to(src_root)
    except ValueError:
        return None
    parts = list(relative.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    if not parts:
        return None
    return ".".join(parts)


def source_modules(root: Path) -> dict[str, Path]:
    """已知模块 → 源文件。包也以其包名登记。"""
    modules: dict[str, Path] = {}
    src_root = root / SRC
    for path in sorted(src_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        module = _module_for_path(rel)
        if module:
            modules[module] = path
    return modules


def parse_imports(
    path: Path,
    module: str,
    known: set[str],
) -> set[str]:
    """解析一个模块直接依赖的已知项目模块（尽量取最细粒度）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    is_package = path.name == "__init__.py"
    package = module if is_package else module.rsplit(".", 1)[0]
    deps: set[str] = set()

    def add_candidate(base: str, name: str | None = None) -> None:
        if name:
            candidate = f"{base}.{name}" if base else name
            if candidate in known:
                deps.add(candidate)
                return
        if base in known:
            deps.add(base)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in known:
                    deps.add(alias.name)
                else:
                    # `import coding_agent.web` 已在 known；其余顶层前缀忽略。
                    for candidate in known:
                        if alias.name.startswith(candidate + "."):
                            deps.add(candidate)
                            break
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package
                for _ in range(node.level - 1):
                    base = base.rsplit(".", 1)[0] if "." in base else ""
                if node.module:
                    base = f"{base}.{node.module}" if base else node.module
            else:
                base = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    add_candidate(base)
                else:
                    add_candidate(base, alias.name)
    return deps


def dependency_graph(root: Path) -> dict[str, set[str]]:
    known = set(source_modules(root))
    graph: dict[str, set[str]] = {}
    for module, path in source_modules(root).items():
        graph[module] = parse_imports(path, module, known)
    return graph


def reverse_graph(graph: dict[str, set[str]]) -> dict[str, set[str]]:
    reverse: dict[str, set[str]] = {module: set() for module in graph}
    for module, deps in graph.items():
        for dep in deps:
            reverse.setdefault(dep, set()).add(module)
    return reverse


def affected_modules(
    changed: set[str], reverse: dict[str, set[str]]
) -> set[str]:
    affected = set(changed)
    queue = list(changed)
    while queue:
        current = queue.pop()
        for importer in reverse.get(current, set()):
            if importer not in affected:
                affected.add(importer)
                queue.append(importer)
    return affected


def test_dependencies(root: Path, known: set[str]) -> dict[str, set[str]]:
    deps: dict[str, set[str]] = {}
    for path in sorted((root / TEST_ROOT).rglob("test_*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root).as_posix()
        module = rel.replace("/", ".").removesuffix(".py")
        deps[rel] = parse_imports(path, module, known)
    return deps


def _is_doc(relpath: str) -> bool:
    return relpath.endswith(DOC_SUFFIXES) or "/AGENTS.md" in relpath


def select(root: Path, changed: list[str]) -> Plan:
    changed = sorted(set(changed))
    if not changed:
        return Plan(changed, [], False, "没有改动")

    # 必须先判 CI/脚本/测试基础设施：它们常是 .yml/.json，但会改变运行语义，
    # 不能被下面的「纯文档」分支误判成“无需测试”。
    force = [
        item
        for item in changed
        if item in FALLBACK_EXACT
        or item.startswith(FALLBACK_PREFIXES)
        or item == "tests/conftest.py"
        or item.startswith("tests/contract/")
        or item.startswith("tests/eval/")
    ]
    if force:
        return Plan(
            changed,
            [],
            True,
            f"改动涉及测试基础设施/脚本/配置：{', '.join(force[:3])}",
        )

    if all(_is_doc(item) for item in changed):
        return Plan(changed, [], False, "只有文档/指令变化，不跑测试")

    modules = source_modules(root)
    known = set(modules)
    changed_tests = [
        item for item in changed
        if item.startswith("tests/") and item.endswith(".py")
    ]
    if any(not (root / item).is_file() for item in changed_tests):
        return Plan(changed, [], True, "存在删除的测试文件，回退全量")
    changed_modules: set[str] = set()
    unknown_sources: list[str] = []
    deleted_sources: list[str] = []
    for item in changed:
        if item.startswith("src/"):
            module = _module_for_path(item)
            if module is None or not (root / item).is_file():
                deleted_sources.append(item)
            else:
                changed_modules.add(module)
        elif item.endswith(".py") and not item.startswith("tests/"):
            unknown_sources.append(item)

    if deleted_sources or unknown_sources:
        return Plan(
            changed,
            [],
            True,
            "存在删除/未识别的源文件，依赖图不完整",
        )

    graph = dependency_graph(root)
    # 包 `__init__.py` 变化会执行该包下的所有子模块导入路径，
    # 因此把子模块一并作为受影响种子（仅靠反向 import 图抓不到这条边）。
    seeds = set(changed_modules)
    for module in changed_modules:
        seeds.update(
            name for name in known if name.startswith(module + ".")
        )
    affected = affected_modules(seeds, reverse_graph(graph))
    tests = test_dependencies(root, known)

    selected = set(changed_tests)
    for relpath, deps in tests.items():
        if deps & affected:
            selected.add(relpath)
    if not selected:
        return Plan(changed, [], True, "没有测试命中改动模块，回退全量")

    for always in ALWAYS_TESTS:
        if (root / always).is_file():
            selected.add(always)
    return Plan(
        changed,
        sorted(selected),
        False,
        f"命中 {len(selected)} 个测试文件",
    )


def changed_paths(root: Path, args: argparse.Namespace) -> list[str]:
    commands: list[list[str]] = []
    if args.base:
        commands.append(["git", "diff", "--name-status", args.base])
    elif args.staged:
        commands.append(["git", "diff", "--name-status", "--cached"])
    else:
        commands.append(["git", "diff", "--name-status"])
        commands.append(
            ["git", "ls-files", "--others", "--exclude-standard"]
        )

    paths: list[str] = []
    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
        except OSError as exc:
            raise RuntimeError(f"无法执行 git：{exc}") from exc
        if result.returncode != 0:
            if command[1] == "ls-files":
                continue
            raise RuntimeError(result.stderr.strip() or "git diff 失败")
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if command[1] == "ls-files":
                paths.append(line)
                continue
            fields = line.split("\t")
            if len(fields) >= 2:
                paths.append(fields[-1])
    return sorted(set(paths))


def build_command(plan: Plan, root: Path) -> list[str]:
    if plan.fallback:
        return [sys.executable, "-m", "pytest", "-m", QUICK_MARKERS]
    if not plan.tests:
        return []
    return [
        sys.executable,
        "-m",
        "pytest",
        *plan.tests,
        "-m",
        QUICK_MARKERS,
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="按 git 改动选择受影响测试（增量测试）"
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--base", help="相对某个 git 版本（如 origin/main）")
    group.add_argument(
        "--staged", action="store_true", help="只看已暂存改动"
    )
    group.add_argument(
        "--worktree",
        action="store_true",
        help="工作区未提交改动 + 未跟踪文件（默认）",
    )
    parser.add_argument("--print", action="store_true", help="只打印测试路径")
    parser.add_argument(
        "--exec", action="store_true", help="直接执行选中的 pytest 命令"
    )
    parser.add_argument(
        "--json", action="store_true", help="输出结构化结果"
    )
    parser.add_argument(
        "--root", default=str(ROOT), help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()

    try:
        changed = changed_paths(root, args)
    except RuntimeError as exc:
        print(f"无法读取 git 改动：{exc}", file=sys.stderr)
        return 2

    plan = select(root, changed)
    command = build_command(plan, root)
    should_run = bool(plan.tests) or plan.fallback

    if args.json:
        print(
            json.dumps(
                {
                    "changed": plan.changed,
                    "tests": plan.tests,
                    "fallback": plan.fallback,
                    "reason": plan.reason,
                    "should_run": should_run,
                    "command": command,
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    elif args.print:
        print("\n".join(plan.tests))
    else:
        print(f"改动 {len(plan.changed)} 个文件：{plan.reason}")
        if plan.fallback:
            print(f"回退全量：{shlex.join(command)}")
        elif plan.tests:
            print(f"选中 {len(plan.tests)} 个测试文件：")
            for path in plan.tests:
                print(f"  {path}")
            print(f"运行：{shlex.join(command)}")
        else:
            print("无需运行测试")

    if args.exec and should_run:
        return subprocess.run(command, cwd=root, check=False).returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
