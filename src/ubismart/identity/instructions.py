"""分层项目指令：`AGENTS.md` / `CLAUDE.md`。

长期记忆（`.agent/memory.md`）记的是**事实与约定**；项目指令文件记的是
**执行规则**（怎么构建、哪些目录不要碰、提交格式）。两者来源不同、生命周期
不同，混在一个扁平文件里会越来越难维护。

发现策略：经 `SandboxFs.find_files` 一次扫描工作区（限深度、排除重目录），
按路径深度排序后合并 —— 越靠近文件的指令越具体，放在后面，让模型最后读到。
每个文件有字符上限，总量也有上限，避免把上下文挤爆。
"""

from __future__ import annotations

from ubismart.sandbox.fs import SandboxFs, SandboxFsError
from ubismart.sandbox.pathguard import SandboxPathError
from ubismart.sandbox.wsl_exec import WslSandbox

INSTRUCTION_FILENAMES = ("AGENTS.md", "CLAUDE.md")
MAX_FILE_CHARS = 8_000
MAX_TOTAL_CHARS = 24_000
MAX_FILES = 12
MAX_DEPTH = 4
EXCLUDED_DIRS = (".git", ".agent", ".venv", "venv", "node_modules", "__pycache__", "dist", "build")


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit] + "\n…（指令文件过长已截断，请直接读取原文件）"


def _root_instruction_paths(fs: SandboxFs) -> list[str]:
    """扫描失败时的兜底：至少检查工作区根目录，不全盘丢失。"""
    found: list[str] = []
    for name in INSTRUCTION_FILENAMES:
        try:
            if fs.stat(name).exists:
                found.append(name)
        except (SandboxFsError, SandboxPathError):
            continue
    return found


def discover_instruction_paths(
    sandbox: WslSandbox, root: str
) -> list[str]:
    """返回工作区内的指令文件相对路径（深度浅的在前）。

    走 `SandboxFs.find_files` 而不是自己拼 `find`：指令文件的内容会直接进
    模型上下文，这条路径不该同时握着"任意命令"的能力（TD-034）。
    """
    fs = SandboxFs(sandbox, root)
    try:
        paths = fs.find_files(
            INSTRUCTION_FILENAMES,
            max_depth=MAX_DEPTH,
            exclude_dirs=EXCLUDED_DIRS,
            limit=MAX_FILES * 20,
        )
    except (SandboxFsError, SandboxPathError):
        return _root_instruction_paths(fs)
    paths = [rel for rel in paths if rel and not rel.startswith("./")]
    # 浅层指令先注入，深层/具体指令后注入（后者覆盖前者）
    paths.sort(key=lambda p: (p.count("/"), p))
    return paths[:MAX_FILES]


def load_project_instructions(
    sandbox: WslSandbox, root: str, *, trusted: bool = True
) -> str:
    """读取并合并分层指令；没有文件时返回空串。

    `trusted=False` 用于不可信仓库：AGENTS.md/CLAUDE.md 是**仓库内容**，
    不能当成宿主指令注入，否则提示注入可以直接改写 agent 行为。
    """
    if not trusted:
        return ""
    fs = SandboxFs(sandbox, root)
    sections: list[str] = []
    total = 0
    for rel in discover_instruction_paths(sandbox, root):
        try:
            text = fs.read_text(rel, max_bytes=MAX_FILE_CHARS * 4)
        except (SandboxFsError, SandboxPathError):
            continue
        text = _truncate(text, MAX_FILE_CHARS)
        if not text:
            continue
        block = f"### {rel}\n{text}"
        if total + len(block) > MAX_TOTAL_CHARS:
            break
        sections.append(block)
        total += len(block)
    if not sections:
        return ""
    return (
        "项目指令（仓库内的 AGENTS.md / CLAUDE.md；越靠后的越具体，必须遵守）：\n"
        + "\n\n".join(sections)
    )
