"""只读文件树与 Git 面板后端（Phase 4）。

所有路径都相对工作区，解析后必须仍在工作区内（符号链接也不行）；
Git 只跑只读子命令，且用列表参数、不经过 shell。
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import time
from pathlib import Path

from ubismart.security.read_policy import is_protected_read_path

MAX_ENTRIES = 400
MAX_READ_BYTES = 256 * 1024
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_DIFF_CHARS = 120_000
MAX_SEARCH_FILE_BYTES = 128 * 1024
MAX_SEARCH_SCAN_FILES = 5000
_SKIP_DIRS = {"__pycache__", ".git", "node_modules"}
_SEARCH_SKIP_DIRS = _SKIP_DIRS | {
    ".venv", "venv", "env", ".agent", "dist", "build",
    ".idea", ".vscode", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".aws", ".kube",
}
_BRANCH_RE = re.compile(r"^## ([^.\[ ]+)(?:\.\.\.\S+)?(?: \[(.+)\])?$")
_IMAGE_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}


def _image_mime(path: Path) -> str:
    return _IMAGE_MIME.get(path.suffix.lower(), "")


class BrowseError(ValueError):
    pass


def _root(workspace: str) -> Path:
    try:
        root = Path(workspace).resolve()
    except (OSError, RuntimeError) as exc:
        raise BrowseError(f"工作区不可用：{exc}") from exc
    if not root.is_dir():
        raise BrowseError("工作区不存在或不是目录")
    return root


def _resolve(root: Path, rel: str) -> Path:
    text = str(rel or "").strip().replace("\\", "/")
    if "\x00" in text or text.startswith("/"):
        raise BrowseError("路径必须是工作区内的相对路径")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        raise BrowseError("路径越界")
    try:
        target = (root / Path(*parts)).resolve()
    except (OSError, RuntimeError) as exc:
        raise BrowseError(f"路径不可用：{exc}") from exc
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise BrowseError("路径越界") from exc
    return target


def _relative(root: Path, target: Path) -> str:
    return "" if target == root else target.relative_to(root).as_posix()


def list_directory(workspace: str, rel: str = "") -> dict:
    root = _root(workspace)
    target = _resolve(root, rel)
    if not target.is_dir():
        raise BrowseError("不是目录")
    entries: list[dict] = []
    truncated = False
    try:
        children = sorted(
            target.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.lower()),
        )
    except OSError as exc:
        raise BrowseError(f"无法读取目录：{exc}") from exc
    for item in children:
        if item.name in _SKIP_DIRS:
            continue
        if len(entries) >= MAX_ENTRIES:
            truncated = True
            break
        try:
            is_dir = item.is_dir()
            size = 0 if is_dir else item.stat().st_size
        except OSError:
            continue
        entries.append(
            {
                "name": item.name,
                "path": _relative(root, item),
                "is_dir": is_dir,
                "size": size,
            }
        )
    return {
        "path": _relative(root, target),
        "entries": entries,
        "truncated": truncated,
    }


def read_text_file(workspace: str, rel: str) -> dict:
    root = _root(workspace)
    target = _resolve(root, rel)
    if not target.is_file():
        raise BrowseError("不是文件")
    mime = _image_mime(target)
    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            data = handle.read(MAX_IMAGE_BYTES if mime else MAX_READ_BYTES)
    except OSError as exc:
        raise BrowseError(f"无法读取文件：{exc}") from exc
    binary = b"\x00" in data[:8192]
    image = bool(mime) and size <= MAX_IMAGE_BYTES and size <= len(data)
    data_url = ""
    if image:
        encoded = base64.b64encode(data).decode("ascii")
        data_url = f"data:{mime};base64,{encoded}"
    return {
        "path": _relative(root, target),
        "size": size,
        "truncated": size > len(data),
        "binary": binary,
        "content": "" if binary else data.decode("utf-8", errors="replace"),
        "image": image,
        "mime": mime,
        "data_url": data_url,
        "image_too_large": bool(mime) and size > MAX_IMAGE_BYTES,
    }


def search_workspace(
    workspace: str,
    query: str,
    *,
    limit: int = 50,
) -> dict:
    """按路径与内容做只读检索；不跟随符号链接，不返回秘密文件。"""
    root = _root(workspace)
    text = str(query or "").strip()
    if not text:
        return {"query": "", "results": [], "truncated": False}
    if len(text) > 200:
        raise BrowseError("搜索词过长")
    needle = text.lower()
    cap = max(1, min(int(limit), 100))
    results: list[dict] = []
    truncated = False
    scanned = 0
    deadline = time.monotonic() + 3.0
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name for name in dirs
            if name not in _SEARCH_SKIP_DIRS and not name.startswith(".git")
        )
        for name in sorted(files):
            if time.monotonic() > deadline:
                truncated = True
                break
            target = Path(current) / name
            if target.is_symlink():
                continue
            rel = _relative(root, target)
            if is_protected_read_path(rel):
                continue
            scanned += 1
            if scanned > MAX_SEARCH_SCAN_FILES:
                truncated = True
                break
            if needle in rel.lower():
                results.append(
                    {"path": rel, "line": 0, "preview": "", "kind": "path"}
                )
                if len(results) >= cap:
                    truncated = True
                    break
            try:
                size = target.stat().st_size
            except OSError:
                continue
            if size > MAX_SEARCH_FILE_BYTES:
                continue
            try:
                data = target.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:4096]:
                continue
            content = data.decode("utf-8", errors="replace")
            for line_number, line in enumerate(content.splitlines(), 1):
                if needle not in line.lower():
                    continue
                results.append(
                    {
                        "path": rel,
                        "line": line_number,
                        "preview": line.strip()[:240],
                        "kind": "content",
                    }
                )
                if len(results) >= cap:
                    truncated = True
                    break
            if len(results) >= cap:
                break
        if len(results) >= cap or scanned > MAX_SEARCH_SCAN_FILES:
            break
        if time.monotonic() > deadline:
            truncated = True
            break
    return {"query": text, "results": results[:cap], "truncated": truncated}


def _git(root: Path, *args: str, timeout: float = 10.0):
    env = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "LC_ALL": "C",
    }
    return subprocess.run(  # noqa: S603 - 列表参数，无 shell
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        check=False,
        env=env,
    )


def _parse_branch(line: str) -> tuple[str, int, int]:
    match = _BRANCH_RE.match(line)
    if not match:
        return "", 0, 0
    branch = match.group(1).strip()
    ahead = behind = 0
    tracker = match.group(2) or ""
    for part in tracker.split(","):
        part = part.strip()
        if part.startswith("ahead "):
            ahead = int(part[6:] or 0)
        elif part.startswith("behind "):
            behind = int(part[7:] or 0)
    return branch, ahead, behind


def git_summary(
    workspace: str, *, diff: bool = False, log_limit: int = 20
) -> dict:
    root = _root(workspace)
    try:
        probe = _git(root, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"is_repo": False, "error": str(exc)}
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        return {"is_repo": False, "error": probe.stderr.strip()}

    status = _git(root, "status", "--porcelain=v1", "--branch")
    branch = ""
    ahead = behind = 0
    changes: list[dict[str, str]] = []
    lines = status.stdout.splitlines()
    for index, line in enumerate(lines):
        if index == 0 and line.startswith("## "):
            branch, ahead, behind = _parse_branch(line)
            continue
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        changes.append({"status": line[:2].strip(), "path": path})

    log = _git(
        root,
        "log",
        f"-n{max(1, min(log_limit, 100))}",
        "--pretty=%h\t%ad\t%s",
        "--date=short",
    )
    commits = [
        {"hash": row[0], "date": row[1], "subject": row[2]}
        for row in (
            line.split("\t", 2)
            for line in log.stdout.splitlines()
            if line.count("\t") >= 2
        )
    ]
    result = {
        "is_repo": True,
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "changes": changes,
        "commits": commits,
    }
    if diff:
        try:
            # /mnt 上的大仓库可能超过 10 秒；超时只降级 diff，不影响状态面板。
            unstaged = _git(root, "diff", "--no-color", timeout=20.0)
            staged = _git(
                root, "diff", "--cached", "--no-color", timeout=20.0
            )
            result["diff"] = unstaged.stdout[:MAX_DIFF_CHARS]
            result["staged_diff"] = staged.stdout[:MAX_DIFF_CHARS]
        except (OSError, subprocess.SubprocessError) as exc:
            result["diff"] = ""
            result["staged_diff"] = ""
            result["diff_error"] = f"{type(exc).__name__}: {exc}"
    return result
