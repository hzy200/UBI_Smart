"""静态复杂度度量（零依赖，只用标准库）。

docs/COMPLEXITY.md 里的数字必须能从源码重算，否则就是第二份会腐烂的手抄本。
本脚本是那组数字的事实源：

    python scripts/measure_complexity.py            # 文本报告
    python scripts/measure_complexity.py --json     # 机器可读
    python scripts/measure_complexity.py --check docs/COMPLEXITY.md

`--check` 存在的原因：本文的表格过去是手抄的，于是出现过"文档写 179 文件 /
43,789 行，实际 249 / 67,322"这种**过期半年**的情况。现在关键数字由脚本
反查文档，对不上就红 —— 数字腐烂变成 CI 失败，而不是某天被偶然发现。

口径
----
- 规模：`src/coding_agent/**/*.py` 的总行数（含空行与注释）。
- 函数：`FunctionDef` / `AsyncFunctionDef`，嵌套函数各自计数。
- 圈复杂度：McCabe 近似 —— 基线 1，`if`/`for`/`while`/`except`/`assert`/
  `if` 表达式/推导式 `if` 各 +1，布尔表达式按操作数个数减一累加。
- 测试规模：`tests/**/*.py`；用例数用 pytest 实跑结果，不在此重复统计。

注意：这是**快照**，不是阈值门禁。数字变化不等于回退，判断要看热点与趋势。
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import statistics
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src" / "coding_agent"
TESTS = ROOT / "tests"


def _cc(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """单函数的圈复杂度近似值。嵌套函数由调用方单独统计。"""

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.value = 1

        def _skip_nested(self, node: ast.AST) -> None:
            if node is fn:
                self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
            self._skip_nested(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
            self._skip_nested(node)

        def visit_If(self, node: ast.If) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_For(self, node: ast.For) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_While(self, node: ast.While) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_Assert(self, node: ast.Assert) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_IfExp(self, node: ast.IfExp) -> None:
            self.value += 1
            self.generic_visit(node)

        def visit_BoolOp(self, node: ast.BoolOp) -> None:
            self.value += len(node.values) - 1
            self.generic_visit(node)

        def visit_comprehension(self, node: ast.comprehension) -> None:
            self.value += len(node.ifs)
            self.generic_visit(node)

    visitor = Visitor()
    visitor.visit(fn)
    return visitor.value


def _lines(path: Path) -> int:
    return len(path.read_text(encoding="utf-8", errors="ignore").splitlines())


def collect() -> dict:
    files = sorted(p for p in SRC.rglob("*.py") if "__pycache__" not in p.parts)
    functions: list[dict] = []
    total_lines = 0
    for path in files:
        total_lines += _lines(path)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.append(
                    {
                        "file": path.relative_to(ROOT).as_posix(),
                        "name": node.name,
                        "line": node.lineno,
                        "length": (node.end_lineno or node.lineno) - node.lineno + 1,
                        "cc": _cc(node),
                    }
                )

    test_files = sorted(p for p in TESTS.rglob("*.py") if "__pycache__" not in p.parts)
    test_lines = sum(_lines(p) for p in test_files)

    lengths = [f["length"] for f in functions]
    ccs = [f["cc"] for f in functions]
    file_lines = Counter({p.relative_to(ROOT).as_posix(): _lines(p) for p in files})

    return {
        "measured_at": "repo snapshot",
        "source": {
            "files": len(files),
            "lines": total_lines,
            "functions": len(functions),
        },
        "function_length": {
            "median": statistics.median(lengths),
            "mean": round(statistics.mean(lengths), 1),
            "over_40_pct": round(100 * sum(x > 40 for x in lengths) / len(lengths), 1),
        },
        "cyclomatic": {
            "median": statistics.median(ccs),
            "mean": round(statistics.mean(ccs), 1),
            "over_10_pct": round(100 * sum(x > 10 for x in ccs) / len(ccs), 1),
            "max": max(ccs),
        },
        "tests": {
            "files": len(test_files),
            "lines": test_lines,
            "ratio": round(test_lines / total_lines, 2),
        },
        "top_cc": sorted(functions, key=lambda f: (-f["cc"], -f["length"]))[:12],
        "top_files": [
            {"file": p, "lines": n} for p, n in file_lines.most_common(10)
        ],
    }


def render(data: dict) -> str:
    s, fl, cy, t = (
        data["source"],
        data["function_length"],
        data["cyclomatic"],
        data["tests"],
    )
    out = [
        f"source files {s['files']} | source lines {s['lines']:,} | functions {s['functions']}",
        f"function length: median {fl['median']:.0f} / mean {fl['mean']}"
        f" / >40 lines {fl['over_40_pct']}%",
        f"cyclomatic: median {cy['median']:.0f} / mean {cy['mean']} / >10 {cy['over_10_pct']}%"
        f" / max {cy['max']}",
        f"tests: {t['files']} files / {t['lines']:,} lines / test:source {t['ratio']}:1",
        "",
        "top cyclomatic complexity:",
    ]
    for f in data["top_cc"]:
        out.append(f"  CC {f['cc']:>3} | {f['length']:>4} lines  {f['file']}::{f['name']}")
    out.append("")
    out.append("largest files:")
    for f in data["top_files"]:
        out.append(f"  {f['lines']:>5} lines  {f['file']}")
    return "\n".join(out)


def _expected_in_doc(data: dict) -> list[str]:
    """`docs/COMPLEXITY.md` 里必须出现的当前数字。"""
    s, fl, cy, t = (
        data["source"],
        data["function_length"],
        data["cyclomatic"],
        data["tests"],
    )
    return [
        f"**{s['files']}**",          # 源文件
        f"**{s['lines']:,}**",        # 源码行数
        f"**{s['functions']:,}**",    # 函数/方法
        f"**{t['files']} / {t['lines']:,}**",  # 测试文件 / 行数
        f"**{t['ratio']} : 1**",      # 测试:源码
        f"**{fl['median']:.0f} 行**",
        f"{fl['over_40_pct']}%",
        f"**{cy['over_10_pct']}%**",
        f"**{cy['max']}**",
    ]


def _check(path: Path, data: dict) -> list[str]:
    if not path.is_file():
        return [f"文档不存在：{path}"]
    text = path.read_text(encoding="utf-8")
    errors = [
        f"文档里缺少当前数字 {token!r}（重跑本脚本后更新 {path.name}）"
        for token in _expected_in_doc(data)
        if token not in text
    ]
    errors.extend(_detail_errors(text, data))
    return errors


# 文档里的路径去掉了 `src/coding_agent/` 前缀（`runtime.py`、`web/app.py`）。
def _short(path: str) -> str:
    return path.split("src/coding_agent/", 1)[-1]


def _section(text: str, start: str, end: str) -> str:
    head = text.find(start)
    if head < 0:
        return ""
    tail = text.find(end, head + len(start))
    return text[head : tail if tail >= 0 else len(text)]


def _first_table(section: str) -> list[str]:
    """只取章节里第一张表：§3 后面还有一张"拆分后"的补充表，列数不同。"""
    rows: list[str] = []
    started = False
    for line in section.splitlines():
        stripped = line.strip()
        if not started:
            if stripped.startswith("| 函数"):
                started = True
            continue
        if not stripped.startswith("|"):
            break
        rows.append(stripped)
    return rows


# 表格里数字加不加粗（`**66**`）都合法，解析时统一剥掉。
_CC_ROW = re.compile(
    r"^\|\s*`(?P<name>[^`]+)`\s*\|\s*(?P<length>\*{0,2}[\d,]+\*{0,2})\s*\|"
    r"\s*\*{0,2}(?P<cc>\d+)\*{0,2}\s*\|"
)
_FILE_ROW = re.compile(
    r"^\|\s*`(?P<file>[^`]+)`\s*\|\s*\*{0,2}(?P<lines>[\d,]+)\*{0,2}\s*\|"
)


def _detail_errors(text: str, data: dict) -> list[str]:
    """§3（热点函数）与 §4（最大文件）的明细也要对得上。

    聚合数字对得上、明细表却过期是最难发现的一种腐烂：谁也不会去逐行核。
    """
    hot: list[tuple[str, int, int]] = []
    for line in _first_table(_section(text, "## 3.", "## 4.")):
        match = _CC_ROW.match(line.strip())
        if match:
            hot.append(
                (
                    match.group("name"),
                    int(match.group("length").replace(",", "").strip("*")),
                    int(match.group("cc")),
                )
            )
    measured_hot = [
        (f"{_short(item['file'])}::{item['name']}", item["length"], item["cc"])
        for item in data["top_cc"]
    ]

    big: list[tuple[str, int]] = []
    for line in _section(text, "## 4.", "## 5.").splitlines():
        match = _FILE_ROW.match(line.strip())
        if match:
            big.append(
                (
                    match.group("file"),
                    int(match.group("lines").replace(",", "").strip("*")),
                )
            )
    measured_big = [
        (_short(item["file"]), item["lines"]) for item in data["top_files"]
    ]

    return _compare("§3 复杂度热点", hot, measured_hot) + _compare(
        "§4 最大文件", big, measured_big
    )


def _compare(label: str, documented: list, measured: list) -> list[str]:
    if documented == measured:
        return []
    missing = [row for row in measured if row not in documented]
    stale = [row for row in documented if row not in measured]
    parts: list[str] = []
    if missing:
        parts.append("缺少 " + "；".join(str(row) for row in missing[:3]))
    if stale:
        parts.append("过期 " + "；".join(str(row) for row in stale[:3]))
    return [f"{label}与实测不一致：{'，'.join(parts)}（重跑本脚本后更新文档）"]


def main() -> int:
    parser = argparse.ArgumentParser(description="静态复杂度度量（零依赖）")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    parser.add_argument(
        "--check",
        metavar="DOC",
        help="校验文档里的关键数字是否与当前源码一致",
    )
    args = parser.parse_args()
    data = collect()
    if args.check:
        errors = _check(Path(args.check), data)
        for error in errors:
            print(error)
        if errors:
            return 1
        print(f"{args.check} 的数字与源码一致。")
        return 0
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else render(data))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
