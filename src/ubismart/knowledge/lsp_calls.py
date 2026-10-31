"""可选的 LSP 动态调用解析。

只在显式开启且存在 `AGENT_LSP_COMMAND` 时使用；对 `dynamic_calls` 里的
位置执行 `textDocument/definition`，把能定位到的目标补成 `kind=lsp` 边。
LSP 不可用/超时/返回空时保持原有 `dynamic_calls`，影响分析继续 fallback。
"""

from __future__ import annotations

import posixpath
from typing import Any

from ubismart.codeintel.lsp import (
    LspClient,
    command_from_settings,
)


def make_lsp_client(settings: Any, root: str) -> LspClient | None:
    command = command_from_settings(
        str(getattr(settings, "lsp_command", "") or "")
    )
    if not command:
        return None
    try:
        client = LspClient(
            command,
            root=root,
            timeout=float(getattr(settings, "lsp_timeout", 10.0) or 10.0),
        )
        client.initialize()
    except Exception:  # noqa: BLE001 - LSP 是增强，不是前提
        return None
    return client


def _relative(path: str, root: str) -> str:
    text = str(path or "").replace("\\", "/")
    if root:
        prefix = str(root).replace("\\", "/").rstrip("/") + "/"
        if text.startswith(prefix):
            return text[len(prefix) :]
    return text.lstrip("/")


def resolve_dynamic_calls_with_lsp(
    client: Any,
    sources: dict[str, str],
    dynamic_calls: list[dict[str, Any]],
    *,
    root: str = "",
    max_calls: int = 50,
) -> list[dict[str, Any]]:
    """把动态调用位置解析成文件级 definition 边；失败的位置原样跳过。"""
    edges: list[dict[str, Any]] = []
    opened: set[str] = set()
    by_file: dict[str, list[str]] = {}
    for path, text in sources.items():
        by_file[path] = text.splitlines()
    for item in dynamic_calls[: max(1, int(max_calls))]:
        path = str(item.get("file", ""))
        line = int(item.get("line", 0) or 0)
        kind = str(item.get("kind", ""))
        lines = by_file.get(path)
        if not lines or line <= 0 or line > len(lines):
            continue
        absolute = (
            posixpath.join(root.replace("\\", "/").rstrip("/"), path)
            if root and not path.startswith("/")
            else path
        )
        if absolute not in opened:
            try:
                client.did_open(absolute, sources[path])
                opened.add(absolute)
            except Exception:  # noqa: BLE001
                continue
        text_line = lines[line - 1]
        character = text_line.find(kind) + 1
        if character <= 0:
            continue
        try:
            locations = client.definition(absolute, line, character)
        except Exception:  # noqa: BLE001 - 单个位置失败不影响其它
            continue
        if not locations:
            continue
        target_path, target_line, _ = locations[0]
        edges.append(
            {
                "src_file": path,
                "src_symbol": "",
                "dst_file": _relative(target_path, root),
                "dst_symbol": "",
                "dst_line": int(target_line or 0),
                "line": line,
                "kind": "lsp",
                "confidence": "lsp",
                "dynamic": True,
            }
        )
    return edges
