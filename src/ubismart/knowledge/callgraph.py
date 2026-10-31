"""轻量跨语言调用图。

不是语言服务器：Python 用 AST，其余语言用正则提取定义/调用，再按
**唯一符号名**做保守解析。同名多定义、动态调用一律跳过（宁缺勿错）。
产物仅用于影响分析，不参与执行。
"""

from __future__ import annotations

import ast
import re
from typing import Any

from ubismart.knowledge.polyglot import (
    extract_symbol_defs,
    language_of,
)

_KEYWORDS = frozenset(
    {
        "if",
        "for",
        "while",
        "switch",
        "catch",
        "return",
        "function",
        "func",
        "fn",
        "def",
        "class",
        "new",
        "sizeof",
        "typeof",
        "await",
        "async",
        "super",
        "this",
        "self",
        "print",
        "len",
        "str",
        "int",
        "float",
        "list",
        "dict",
        "set",
        "tuple",
        "range",
        "open",
        "isinstance",
        "getattr",
        "setattr",
        "hasattr",
        "Exception",
        "ValueError",
        "RuntimeError",
    }
)
_CALL_RE = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
# 计算属性调用：obj["method"]()
_BRACKET_CALL_RE = re.compile(
    r"\[\s*[\"']([A-Za-z_$][\w$]*)[\"']\s*\]\s*\("
)
# 宏调用：trace!(...)
_MACRO_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)!\s*[\(\[]")
_DYNAMIC_PATTERNS = (
    re.compile(r"\beval\s*\("),
    re.compile(r"\bFunction\s*\("),
    re.compile(r"\bimport\s*\("),
    re.compile(r"\breflect\s*\."),
    re.compile(r"::\s*[A-Za-z_]\w*\s*\("),
)
_DYNAMIC_PY_CALLS = frozenset(
    {
        "getattr",
        "setattr",
        "eval",
        "exec",
        "__import__",
        "import_module",
        "partial",
    }
)
_RUST_MACRO_DEF = re.compile(r"macro_rules!\s+([A-Za-z_]\w*)")
_C_MACRO_DEF = re.compile(
    r"^\s*#\s*define\s+([A-Za-z_]\w*)", re.MULTILINE
)
MAX_EDGES = 5_000
MAX_CALLS_PER_FILE = 500
MAX_DYNAMIC_PER_FILE = 200


def _macro_defs_for(path: str, text: str) -> list[dict[str, Any]]:
    lowered = str(path).lower()
    if lowered.endswith(".rs"):
        pattern = _RUST_MACRO_DEF
    elif lowered.endswith((".c", ".h", ".cc", ".cpp", ".hpp")):
        pattern = _C_MACRO_DEF
    else:
        return []
    return [
        {
            "file": path,
            "name": match.group(1),
            "line": text.count("\n", 0, match.start()) + 1,
            "kind": "macro",
        }
        for match in pattern.finditer(text)
    ]


def extract_macro_defs(
    sources: dict[str, str],
) -> list[dict[str, Any]]:
    """提取 Rust macro_rules! 与 C 预处理器宏定义（不做真正展开）。"""
    defs: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int]] = set()
    for path, text in sources.items():
        for item in _macro_defs_for(path, text):
            key = (str(item["file"]), str(item["name"]), int(item["line"]))
            if key in seen:
                continue
            seen.add(key)
            defs.append(item)
    return defs


def _call_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _python_defs_calls(
    path: str, source: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [], []

    defs: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    def record_calls(
        body: ast.AST, caller: str, *, skip_nested: bool = True
    ) -> None:
        for node in ast.walk(body):
            if skip_nested and node is not body and isinstance(
                node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
            ):
                continue
            if not isinstance(node, ast.Call):
                continue
            line = int(getattr(node, "lineno", 0) or 0)
            name = _call_name(node.func)
            inner = node.func
            if isinstance(inner, ast.Call) and _call_name(inner.func) in {
                "getattr",
                "setattr",
            }:
                args = list(inner.args)
                if len(args) >= 2 and isinstance(args[1], ast.Constant):
                    target = str(args[1].value or "")
                    if target:
                        calls.append(
                            {
                                "file": path,
                                "caller": caller,
                                "callee": target,
                                "line": line,
                                "dynamic": True,
                            }
                        )
                        continue
            if isinstance(inner, ast.Subscript) and isinstance(
                inner.slice, ast.Constant
            ):
                target = str(inner.slice.value or "")
                if target:
                    calls.append(
                        {
                            "file": path,
                            "caller": caller,
                            "callee": target,
                            "line": line,
                            "dynamic": True,
                        }
                    )
                    continue
            if not name or name in _KEYWORDS:
                continue
            calls.append(
                {
                    "file": path,
                    "caller": caller,
                    "callee": name,
                    "line": line,
                }
            )

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            defs.append(
                {"file": path, "name": node.name, "line": node.lineno, "kind": "class"}
            )
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defs.append(
                        {
                            "file": path,
                            "name": item.name,
                            "line": item.lineno,
                            "kind": "def",
                        }
                    )
                    record_calls(item, item.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append(
                {"file": path, "name": node.name, "line": node.lineno, "kind": "def"}
            )
            record_calls(node, node.name)
    return defs, calls[:MAX_CALLS_PER_FILE]


def _regex_defs_calls(
    path: str, text: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    defs = [
        {
            "file": path,
            "name": str(item["name"]),
            "line": int(item["line"]),
            "kind": str(item["kind"]),
        }
        for item in extract_symbol_defs(path, text)
    ]
    defs.extend(
        {
            "file": item["file"],
            "name": item["name"],
            "line": item["line"],
            "kind": item["kind"],
        }
        for item in _macro_defs_for(path, text)
    )
    ordered = sorted(defs, key=lambda item: int(item["line"]))

    def caller_at(line: int) -> str:
        caller = ""
        for item in ordered:
            if int(item["line"]) <= line:
                caller = str(item["name"])
            else:
                break
        return caller

    calls: list[dict[str, Any]] = []
    for match in _CALL_RE.finditer(text):
        name = match.group(1)
        if name in _KEYWORDS:
            continue
        line = text.count("\n", 0, match.start()) + 1
        calls.append(
            {
                "file": path,
                "caller": caller_at(line),
                "callee": name,
                "line": line,
            }
        )
        if len(calls) >= MAX_CALLS_PER_FILE:
            break
    for pattern, dynamic in (
        (_BRACKET_CALL_RE, True),
        (_MACRO_CALL_RE, True),
    ):
        for match in pattern.finditer(text):
            name = match.group(1)
            if not name or name in _KEYWORDS:
                continue
            line = text.count("\n", 0, match.start()) + 1
            calls.append(
                {
                    "file": path,
                    "caller": caller_at(line),
                    "callee": name,
                    "line": line,
                    "dynamic": dynamic,
                }
            )
            if len(calls) >= MAX_CALLS_PER_FILE:
                break
    return defs, calls


def detect_dynamic_calls(
    sources: dict[str, str],
) -> list[dict[str, Any]]:
    """记录无法静态确定目标的调用/反射/宏，供影响分析判断是否保守。"""
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for path, text in sources.items():
        entries: list[dict[str, Any]] = []
        if str(path).lower().endswith((".py", ".pyi")):
            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree is not None:
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    name = _call_name(node.func)
                    kind = ""
                    if name in _DYNAMIC_PY_CALLS:
                        kind = name
                    elif isinstance(node.func, ast.Subscript) and _call_name(
                        node.func.value
                    ) in {"globals", "locals"}:
                        kind = _call_name(node.func.value)
                    if kind:
                        entries.append(
                            {
                                "file": path,
                                "line": int(
                                    getattr(node, "lineno", 0) or 0
                                ),
                                "kind": kind,
                                "language": "python",
                            }
                        )
        else:
            for index, pattern in enumerate(_DYNAMIC_PATTERNS):
                for match in pattern.finditer(text):
                    entries.append(
                        {
                            "file": path,
                            "line": text.count("\n", 0, match.start()) + 1,
                            "kind": f"pattern-{index}",
                            "language": language_of(path),
                        }
                    )
        for item in entries[:MAX_DYNAMIC_PER_FILE]:
            key = (str(item["file"]), int(item["line"]), str(item["kind"]))
            if key in seen:
                continue
            seen.add(key)
            found.append(item)
    return found


def build_call_graph(
    sources: dict[str, str], *, max_edges: int = MAX_EDGES
) -> list[dict[str, Any]]:
    defs: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    for path, text in sources.items():
        if str(path).lower().endswith((".py", ".pyi")):
            file_defs, file_calls = _python_defs_calls(path, text)
        else:
            file_defs, file_calls = _regex_defs_calls(path, text)
        defs.extend(file_defs)
        calls.extend(file_calls)

    by_name: dict[str, list[dict[str, Any]]] = {}
    for item in defs:
        by_name.setdefault(str(item["name"]), []).append(item)

    edges: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for call in calls:
        callee = str(call.get("callee", ""))
        candidates = by_name.get(callee) or []
        if not candidates:
            continue
        source_file = str(call.get("file", ""))
        caller = str(call.get("caller", ""))
        if caller == callee:
            continue
        same_file = [
            item
            for item in candidates
            if str(item["file"]) == source_file
        ]
        if same_file:
            target = same_file[0]
            confidence = "high"
        elif len(candidates) == 1:
            target = candidates[0]
            confidence = (
                "medium"
                if language_of(source_file)
                == language_of(str(target["file"]))
                else "low"
            )
        else:
            # 同名多定义：无法可靠解析，跳过而不是赌一个。
            continue
        target_file = str(target["file"])
        if target_file == source_file and caller == str(target["name"]):
            continue
        key = (source_file, caller, target_file, str(target["name"]))
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            {
                "src_file": source_file,
                "src_symbol": caller,
                "dst_file": target_file,
                "dst_symbol": str(target["name"]),
                "line": int(call.get("line", 0) or 0),
                "kind": (
                    "macro"
                    if str(target.get("kind", "")) == "macro"
                    else "call"
                ),
                "confidence": confidence,
                "dynamic": bool(call.get("dynamic")),
            }
        )
        if len(edges) >= max_edges:
            break
    return edges
