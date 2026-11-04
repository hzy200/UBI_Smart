"""Phase 3C：并发写 worker（git worktree 隔离）。

每个 implement 任务在**独立 worktree** 中运行，工具只作用于该 worktree；
完成后把变更导出成 patch，由 dispatch 顺序合并回主工作区。这样：

- 并发写不会互相踩文件；
- 主工作区在合并前保持不变；
- 合并失败时补丁不会被应用（先 `git apply --check`）。

前置条件是工作区为干净的 git 仓库；不满足时 runner 直接失败，调用方会
退回只读编排。
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import posixpath
import re
import shlex
import time
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import BaseTool

from ubismart.codeintel.python_index import (
    build_index,
    collect_python_sources,
    related_test_files,
)
from ubismart.config import Settings
from ubismart.execution.context import tool_execution_context
from ubismart.execution.host_ops import HOST_ORIGIN, host_run
from ubismart.llm.prompts import (
    IMPLEMENT_WORKER_PROMPT,
    format_verification_feedback,
)
from ubismart.orchestrator.contracts import WorkerResult
from ubismart.orchestrator.retry import format_retry_context
from ubismart.platform.read_cache import ReadCache
from ubismart.protocol.messages import text_of
from ubismart.sandbox.fs import SandboxFs
from ubismart.sandbox.wsl_exec import WslSandbox
from ubismart.session.recovery import retry_async
from ubismart.tools.artifacts import unpack
from ubismart.tools.files import (
    COPY_TOOL_NAME,
    EDIT_MANY_TOOL_NAME,
    EDIT_TOOL_NAME,
    LIST_TOOL_NAME,
    MKDIR_TOOL_NAME,
    MOVE_TOOL_NAME,
    READ_TOOL_NAME,
    WRITE_TOOL_NAME,
)
from ubismart.tools.registry import build_tools
from ubismart.tools.search import FIND_FILES, SEARCH_CODE
from ubismart.tools.testrun import run_verification

# 注意：**不含 file_delete**。删除在主管道路径是 L2（逐条确认），而 worker
# 直接调用工具、不经过 approval_gate；把它留给主 agent，避免并发写 worker
# 成为无审批的删除通道。移动/覆盖仍有快照兜底。
IMPLEMENT_TOOL_NAMES = frozenset(
    {
        READ_TOOL_NAME,
        WRITE_TOOL_NAME,
        EDIT_TOOL_NAME,
        EDIT_MANY_TOOL_NAME,
        MOVE_TOOL_NAME,
        COPY_TOOL_NAME,
        MKDIR_TOOL_NAME,
        LIST_TOOL_NAME,
        SEARCH_CODE,
        FIND_FILES,
    }
)

ImplementRunner = Callable[..., Awaitable[WorkerResult]]


def _safe_id(task_id: str) -> str:
    """worktree / patch 文件名。

    截断后必须再拼一个短哈希：两个长 id 的前缀相同时，只截断会让它们共用
    同一个 worktree 与 patch 路径（并发时互相覆盖）。
    """
    text = re.sub(r"[^A-Za-z0-9_-]", "_", task_id)[:40]
    digest = hashlib.sha1(task_id.encode("utf-8")).hexdigest()[:8]
    return f"{text}-{digest}" if text else digest


def _worktree_id(task_id: str, attempt: int = 1) -> str:
    """worktree / patch 标识；重试必须换目录，不能复用旧 attempt。"""
    number = max(1, int(attempt or 1))
    if number <= 1:
        return _safe_id(task_id)
    return _safe_id(f"{task_id}#attempt-{number}")


def _usage(message: Any) -> tuple[int, int]:
    meta = getattr(message, "usage_metadata", None) or {}
    return (
        int(meta.get("input_tokens") or 0),
        int(meta.get("output_tokens") or 0),
    )


def _child_config(config: RunnableConfig | None, task_id: str) -> dict[str, Any]:
    base = dict(config or {})
    metadata = dict(base.get("metadata") or {})
    metadata.update({"agent_id": f"implement:{task_id}", "role": "implement"})
    return {
        **base,
        "run_name": f"implement:{task_id}",
        "tags": [*(base.get("tags") or []), "subagent", "implement"],
        "metadata": metadata,
    }


def _within_scope(path: str, scopes: list[str]) -> bool:
    normalized = posixpath.normpath(path.replace("\\", "/"))
    return any(
        normalized == scope or normalized.startswith(scope.rstrip("/") + "/")
        for scope in scopes
    )


# 工作区不一定有 .gitignore（评测种子就没有）。worker 自己的快照、worktree、
# patch 与 Python 字节码必须显式排除，否则会被当成用户改动 / 收进补丁。
STAGE_EXCLUDES = (
    ":(exclude).agent",
    ":(exclude).agent/**",
    ":(exclude,glob)**/__pycache__/**",
)


def stage_args() -> str:
    """`git status` / `git add` / `git diff` 共用的排除 pathspec。"""
    return " ".join(shlex.quote(item) for item in STAGE_EXCLUDES)


def prune_orphan_worktrees(sandbox: WslSandbox, root: str) -> None:
    """清掉上次运行残留的 worktree 与 git 元数据。

    正常路径由 `remove_worktree()` 在每个 worker 收尾时清理；但 SIGKILL /
    宿主断电时它没有机会执行，`.agent/worktrees/` 与 `.git/worktrees`
    里的注册项会一直留着，占空间也让 `git worktree list` 越来越脏。

    **只能在确认没有 implement 在飞时调用**（例如编排启动点）：这里的
    `rm -rf` 会把正在使用的 worktree 一起删掉。
    """
    parent = posixpath.join(root, ".agent", "worktrees")
    try:
        host_run(
            sandbox,
            "git worktree prune",
            origin=HOST_ORIGIN,
            kind="git_worktree",
            cwd=root,
        )
        host_run(
            sandbox,
            f"rm -rf {shlex.quote(parent)}",
            origin=HOST_ORIGIN,
            kind="worktree_cleanup",
            cwd=root,
        )
    except Exception:  # noqa: BLE001 - 清理是 best-effort，不该阻断运行
        return


def _workspace_changed_paths(
    sandbox: WslSandbox, root: str
) -> set[str] | None:
    """主工作区当前改动路径；读取失败返回 None（调用方 fail closed）。"""
    excludes = stage_args()
    tracked = host_run(
        sandbox,
        f"git diff --name-only -z HEAD -- . {excludes}",
        origin=HOST_ORIGIN,
        kind="git_seed_probe",
        cwd=root,
    )
    untracked = host_run(
        sandbox,
        f"git ls-files --others --exclude-standard -z -- . {excludes}",
        origin=HOST_ORIGIN,
        kind="git_seed_probe",
        cwd=root,
    )
    if not tracked.ok and not untracked.ok:
        return None
    paths: set[str] = set()
    for result in (tracked, untracked):
        if not result.ok:
            continue
        for item in result.stdout.split("\0"):
            text = item.strip()
            if text:
                paths.add(text)
    return paths


def _parse_status_flags(stdout: str) -> dict[str, str]:
    flags: dict[str, str] = {}
    for line in stdout.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            flags[key] = value
    return flags


def _dirty_workspace_error(
    sandbox: WslSandbox,
    root: str,
    allow_dirty_paths: set[str] | None,
) -> tuple[bool, str]:
    # `.agent/` 里是 worktree、patch、快照等 agent 自己的产物；并发 worker
    # 互相看到对方刚创建的 worktree 是正常现象，不能据此判定"用户工作区脏"。
    if allow_dirty_paths is None:
        return False, "工作区有未提交改动，写 worker 已禁用"
    actual = _workspace_changed_paths(sandbox, root)
    if actual is None:
        return False, "无法读取工作区改动，写 worker 已拒绝"
    extra = sorted(actual - set(allow_dirty_paths))
    if extra:
        preview = "、".join(extra[:5])
        more = f" 等 {len(extra)} 个" if len(extra) > 5 else ""
        return False, (
            "工作区存在不属于本次已合并结果的改动，"
            f"写 worker 已拒绝：{preview}{more}"
        )
    return True, ""


def _git_ready(
    sandbox: WslSandbox,
    root: str,
    *,
    allow_dirty_paths: set[str] | None = None,
) -> tuple[bool, str]:
    """一次 wsl.exe 完成 worktree 前置检查。

    原先拆成 5 条命令，4 个 implement worker 并发时就是 20 次进程启动；
    检查项本身不变，只是合并成一次脚本。

    顺序写进入第二波时主工作区必然是 dirty；此时只允许"本 run 已合并路径"
    造成的脏改动，其它路径一律拒绝（fail closed）。
    """
    script = f"""\
inside=$(git rev-parse --is-inside-work-tree 2>/dev/null)
if [ "$inside" != "true" ]; then echo "INSIDE=0"; exit 0; fi
echo "INSIDE=1"
top=$(git rev-parse --show-toplevel 2>/dev/null)
physical=$(pwd -P)
if [ "$top" = "$physical" ]; then echo "ROOT=1"; else echo "ROOT=0"; fi
if git rev-parse HEAD >/dev/null 2>&1; then echo "HEAD=1"; else echo "HEAD=0"; fi
dirty=$(git status --porcelain -- . {stage_args()} 2>/dev/null)
if [ -z "$dirty" ]; then echo "CLEAN=1"; else echo "CLEAN=0"; fi
"""
    result = host_run(
        sandbox,
        script,
        origin=HOST_ORIGIN,
        kind="git_probe",
        cwd=root,
    )
    if not result.ok:
        return False, "无法确认 git 仓库状态"
    flags = _parse_status_flags(result.stdout)
    if flags.get("INSIDE") != "1":
        return False, "工作区不是 git 仓库"
    # worktree 基于整个仓库的 HEAD，patch 路径也是仓库相对的。工作区若只是
    # 仓库的子目录，合并会落到错误的根上 —— 必须要求它就是仓库根。
    if flags.get("ROOT") != "1":
        return False, "工作区不是 git 仓库根目录（写编排需要以仓库根作为工作区）"
    if flags.get("HEAD") != "1":
        return False, "git 仓库还没有 HEAD（需要至少一次提交）"
    if flags.get("CLEAN") != "1":
        return _dirty_workspace_error(sandbox, root, allow_dirty_paths)
    return True, ""


def _changed_files(
    sandbox: WslSandbox, worktree: str, base_ref: str = "HEAD"
) -> list[str]:
    """本 worker 改动过的路径。

    用 `--name-status -M -z` 而不是 `--name-only`：重命名时后者只给新路径，
    合并前按这个列表留底会漏掉被删除的旧路径，回滚就不完整。
    """
    excludes = stage_args()
    add = host_run(
        sandbox,
        f"git add -A -- . {excludes}",
        origin=HOST_ORIGIN,
        kind="git_patch_prepare",
        cwd=worktree,
    )
    # 快照写在 worktree 内的 `.agent/`，它被 .gitignore 忽略。Git 会对这个
    # 被忽略目录返回 exit=1（但已经成功暂存真实改动）。这不是失败，继续看 diff。
    ignored_warning = "ignored by one of your .gitignore" in (
        add.stderr or ""
    ).lower()
    if not add.ok and not ignored_warning:
        return []
    names = host_run(
        sandbox,
        f"git diff --cached --name-status -M -z {shlex.quote(base_ref)} -- . {excludes}",
        origin=HOST_ORIGIN,
        kind="git_patch_prepare",
        cwd=worktree,
    )
    if not names.ok:
        return []

    paths: list[str] = []
    fields = names.stdout.split("\0")
    index = 0
    while index < len(fields):
        status = fields[index]
        if not status:
            index += 1
            continue
        if status.startswith(("R", "C")) and index + 2 < len(fields):
            # R100\0old\0new —— 两条路径都算改动
            paths.extend([fields[index + 1], fields[index + 2]])
            index += 3
        elif index + 1 < len(fields):
            paths.append(fields[index + 1])
            index += 2
        else:
            index += 1
    return sorted({path.strip() for path in paths if path.strip()})


_IMPORT_ERROR_RE = re.compile(
    r"cannot import name '[^']+' from '([^']+)'"
)
_MODULE_ERROR_RE = re.compile(r"No module named '([^']+)'")


def _peer_dependency_failure(
    verification: dict[str, Any], changed: list[str], scopes: list[str]
) -> bool:
    """测试失败是否来自同批其它 worker 尚未合并的模块。

    多个 implement worker 并发时，共享测试可能 import 其它 worker 的文件。
    这不是本 worker 能修的：它没有对应 write_scope，重试只会白烧轮次。
    合并后的主 agent 会跑完整验证，所以这里直接停在 partial。
    """
    text = "\n".join(
        [
            str(verification.get("output_tail") or ""),
            *[
                str(issue.get("message") or "")
                for issue in (verification.get("issues") or [])
                if isinstance(issue, dict)
            ],
        ]
    )
    modules = _IMPORT_ERROR_RE.findall(text) + _MODULE_ERROR_RE.findall(text)
    for module in modules:
        path = module.replace(".", "/") + ".py"
        if path not in changed and not _within_scope(path, scopes):
            return True
    return False


def _impacted_tests(
    sandbox: WslSandbox, worktree: str, changed: list[str]
) -> list[str]:
    """用 AST import 图选测试；索引不可用时返回空，触发全量验证。

    这里**不再**用文件名启发式兜底：索引失败、改动文件含非字面量动态
    import 或未被收集时，任何收窄都可能漏跑测试。`related_test_files`
    内部仍保留文件名启发式，但只在索引可靠时参与选择。
    """
    sources = collect_python_sources(sandbox, worktree)
    if not sources:
        return []
    return related_test_files(build_index(sources), changed)


def _collect_seed_patch(
    sandbox: WslSandbox, root: str, excludes: str
) -> tuple[str | None, list[str], set[str], list[str]]:
    tracked = host_run(
        sandbox,
        f"git diff --binary HEAD -- . {excludes}",
        origin=HOST_ORIGIN,
        kind="git_seed_probe",
        cwd=root,
    )
    if not tracked.ok:
        return (
            f"无法生成基线补丁：{tracked.render(300)}",
            [],
            set(),
            [],
        )
    chunks = [tracked.stdout]

    names = host_run(
        sandbox,
        f"git diff --name-only -z HEAD -- . {excludes}",
        origin=HOST_ORIGIN,
        kind="git_seed_probe",
        cwd=root,
    )
    seed_paths = {
        item.strip()
        for item in (names.stdout.split("\0") if names.ok else [])
        if item.strip()
    }

    listing = host_run(
        sandbox,
        f"git ls-files --others --exclude-standard -z -- . {excludes}",
        origin=HOST_ORIGIN,
        kind="git_seed_probe",
        cwd=root,
    )
    if not listing.ok:
        return (
            f"无法列出未跟踪文件：{listing.render(300)}",
            [],
            set(),
            [],
        )
    untracked = [item for item in listing.stdout.split("\0") if item.strip()]
    seed_paths.update(untracked)
    return None, chunks, seed_paths, untracked


def _seed_file_limit_error(
    seed_paths: set[str], settings: Settings
) -> str | None:
    max_files = max(
        int(getattr(settings, "orchestrator_seed_max_files", 200) or 0), 0
    )
    if len(seed_paths) > max_files:
        return f"seed 文件数 {len(seed_paths)} 超过上限 {max_files}"
    return None


def _append_untracked_patches(
    sandbox: WslSandbox,
    root: str,
    chunks: list[str],
    untracked: list[str],
) -> str | None:
    for path in untracked:
        result = host_run(
            sandbox,
            f"git diff --no-index --binary -- /dev/null {shlex.quote(path)}",
            origin=HOST_ORIGIN,
            kind="git_seed_probe",
            cwd=root,
        )
        # 有差异时 git 返回 exit=1，但 stdout 就是补丁。
        if result.stdout:
            chunks.append(result.stdout)
        elif not result.ok:
            return f"无法读取未跟踪文件 {path}：{result.render(200)}"
    return None


def _apply_seed_patch(
    sandbox: WslSandbox,
    root: str,
    worktree: str,
    task_id: str,
    excludes: str,
    data: bytes,
    attempt: int = 1,
) -> tuple[bool, str, str]:
    patch_rel = posixpath.join(
        ".agent", "seeds", f"{_worktree_id(task_id, attempt)}.patch"
    )
    patch_abs = posixpath.join(root, patch_rel)
    try:
        SandboxFs(sandbox, root).write_bytes(patch_rel, data)
    except Exception as exc:  # noqa: BLE001
        return False, f"无法写入基线补丁：{exc}", ""
    try:
        applied = host_run(
            sandbox,
            f"git apply --binary {shlex.quote(patch_abs)}",
            origin=HOST_ORIGIN,
            kind="git_apply",
            cwd=worktree,
        )
        if not applied.ok:
            return False, f"基线补丁应用失败：{applied.render(300)}", ""
        add = host_run(
            sandbox,
            f"git add -A -- . {excludes}",
            origin=HOST_ORIGIN,
            kind="git_seed_baseline",
            cwd=worktree,
        )
        ignored_warning = "ignored by one of your .gitignore" in (
            add.stderr or ""
        ).lower()
        if not add.ok and not ignored_warning:
            return False, f"无法记录基线：{add.render(300)}", ""
        tree = host_run(
            sandbox,
            "git write-tree",
            origin=HOST_ORIGIN,
            kind="git_seed_baseline",
            cwd=worktree,
        )
        base_ref = (
            tree.stdout.strip().splitlines()[-1].strip()
            if tree.stdout.strip()
            else ""
        )
        if not tree.ok or not base_ref:
            return False, "无法记录基线树", ""
        host_run(
            sandbox,
            "git reset -q",
            origin=HOST_ORIGIN,
            kind="git_reset",
            cwd=worktree,
        )
        return True, "", base_ref
    finally:
        host_run(
            sandbox,
            f"rm -f {shlex.quote(patch_abs)}",
            origin=HOST_ORIGIN,
            kind="merge_temp_cleanup",
            cwd=root,
        )


def _seed_worktree(
    sandbox: WslSandbox,
    root: str,
    worktree: str,
    task_id: str,
    settings: Settings,
    attempt: int = 1,
) -> tuple[bool, str, str, list[str], int]:
    """把主工作区已合并的未提交改动注入新 worktree，并返回基线树。

    不 commit、不移动 HEAD：先用 `git diff` 生成补丁，apply 到 worktree 后
    用 `git write-tree` 记录基线树，后续 worker 补丁以这棵树为 diff 基准。
    返回值：(ok, error, base_ref, seed_files, seed_bytes)。
    """
    excludes = stage_args()
    error, chunks, seed_paths, untracked = _collect_seed_patch(
        sandbox, root, excludes
    )
    if error:
        return False, error, "", [], 0
    error = _seed_file_limit_error(seed_paths, settings)
    if error:
        return False, error, "", [], 0
    error = _append_untracked_patches(sandbox, root, chunks, untracked)
    if error:
        return False, error, "", [], 0

    patch = "".join(chunks)
    if not patch.strip():
        return False, "未找到上一波已合并的改动，拒绝顺序写", "", [], 0
    data = patch.encode("utf-8", errors="surrogateescape")
    max_bytes = max(
        int(getattr(settings, "orchestrator_seed_max_bytes", 5_000_000) or 0),
        0,
    )
    if len(data) > max_bytes:
        return (
            False,
            f"基线补丁 {len(data)} 字节超过上限 {max_bytes}",
            "",
            [],
            0,
        )
    ok, error, base_ref = _apply_seed_patch(
        sandbox, root, worktree, task_id, excludes, data, attempt
    )
    if not ok:
        return False, error, "", [], 0
    return True, "", base_ref, sorted(seed_paths), len(data)


def _make_patch(
    sandbox: WslSandbox,
    worktree: str,
    root: str,
    task_id: str,
    base_ref: str = "HEAD",
    attempt: int = 1,
) -> str:
    excludes = stage_args()
    diff = host_run(
        sandbox,
        f"git diff --cached --binary {shlex.quote(base_ref)} -- . {excludes}",
        origin=HOST_ORIGIN,
        kind="git_patch_prepare",
        cwd=worktree,
    )
    if not diff.ok or not diff.stdout.strip():
        return ""
    patch_rel = posixpath.join(
        ".agent", "patches", f"{_worktree_id(task_id, attempt)}.patch"
    )
    try:
        SandboxFs(sandbox, root).write_text(patch_rel, diff.stdout)
    except Exception:  # noqa: BLE001
        return ""
    return posixpath.join(root, patch_rel)


def _build_worker_messages(
    *,
    task: dict[str, Any],
    worktree: str,
    scopes: list[str],
    max_rounds: int,
    memories: list[str] | None,
    instructions: str,
    parent_request: str,
    session_brief: str,
    repo_map: str,
    retry_note: str,
) -> list[Any]:
    """组装 implement worker 的系统提示与首条用户消息。"""
    system_text = IMPLEMENT_WORKER_PROMPT.format(
        cwd=worktree,
        write_scope="\n".join(f"- {scope}" for scope in scopes),
        max_rounds=max_rounds,
    )
    facts = [str(fact).strip() for fact in (memories or []) if str(fact).strip()]
    if facts:
        system_text += (
            "\n\n关于这个项目的长期记忆（可信，必须遵守）：\n"
            + "\n".join(f"- {fact}" for fact in facts)
        )
    if instructions:
        system_text += f"\n\n{instructions}"
    message = (
        (
            "之前轮次的会话约束（继续遵守）：\n"
            f"{session_brief.strip()}\n\n"
            if session_brief.strip()
            else ""
        )
        + f"当前用户请求：{parent_request or '（未提供）'}\n\n"
        f"实现任务：{str(task.get('goal', '')).strip()}\n\n"
        f"write_scope：{', '.join(scopes)}"
        f"{retry_note}"
        + (
            "\n\n本任务的 worktree 已包含上一波已合并的改动；"
            "不要重复实现这些内容，只在它们之上继续。"
            if task.get("seed_from_workspace")
            else ""
        )
        + (
            f"\n\n仓库结构摘要（AST 生成，供定位）：\n{repo_map}"
            if repo_map
            else ""
        )
    )
    return [
        SystemMessage(content=system_text),
        HumanMessage(content=message),
    ]


def _normalize_implement_scopes(task: dict[str, Any]) -> list[str]:
    return [
        str(scope).strip().replace("\\", "/").rstrip("/")
        for scope in (task.get("write_scope") or [])
        if str(scope).strip()
    ]


def _baseline_paths(task: dict[str, Any]) -> set[str]:
    return {
        str(path).strip()
        for path in (task.get("baseline_paths") or [])
        if str(path).strip()
    }


def _coerce_positive(raw: Any, default: int) -> int:
    return max(int(raw if raw is not None else default), 1)


def _resolve_max_tokens(raw: Any, settings: Settings) -> int:
    value = raw if raw is not None else settings.orchestrator_task_tokens or 0
    return max(int(value), 0)


class _ImplementRun:
    """单次 implement worker 执行的状态与步骤。"""

    def __init__(
        self,
        *,
        llm: BaseChatModel,
        sandbox: WslSandbox,
        root: str,
        settings: Settings,
        default_rounds: int,
        max_repairs: int,
        default_timeout: int,
        task: dict[str, Any],
        parent_request: str,
        cwd: str,
        config: RunnableConfig | None,
        cancel: Callable[[], bool] | None,
        session_brief: str,
        memories: list[str] | None,
        instructions: str,
    ) -> None:
        self.llm = llm
        self.sandbox = sandbox
        self.root = root
        self.settings = settings
        self.max_repairs = max_repairs
        self.task = task
        self.parent_request = parent_request
        self.cwd = cwd
        self.config = config
        self.cancel = cancel
        self.session_brief = session_brief
        self.memories = memories
        self.instructions = instructions
        self.started = time.monotonic()
        self.task_id = str(task.get("id", ""))
        self.attempt = max(1, int(task.get("attempt") or 1))
        # 把 attempt 编进传给 helpers 的标识，既保持它们原有的五参数签名，
        # 又让 worktree/patch/seed 路径在重试时天然隔离。
        self.safe = _worktree_id(self.task_id, self.attempt)
        # 首次 attempt 保持原有 task_id 传给 helper（测试与旧补丁路径不变）；
        # 重试时传入带 attempt 的稳定标识，确保 patch/seed 也不复用。
        self.export_id = (
            self.task_id if self.attempt <= 1 else self.safe
        )
        # 取消只停协程，不停 to_thread 里的 wsl.exe；保留 executor future，
        # 清理 worktree 前先确认这些进程已经退出。
        self._thread_futures: set[asyncio.Future[Any]] = set()
        self.goal = str(task.get("goal", "")).strip()
        self.retry_note = format_retry_context(task)
        self.scopes = _normalize_implement_scopes(task)
        self.worker_id = f"implement:{self.task_id}"
        self.child_config = _child_config(config, self.task_id)
        # 任务级预算优先于全局默认；None 继承全局。轮次/超时至少为 1，
        # token 预算 0 表示不限制。
        self.max_rounds = _coerce_positive(
            task.get("max_rounds"), default_rounds
        )
        self.timeout = _coerce_positive(
            task.get("timeout"), default_timeout
        )
        self.max_tokens = _resolve_max_tokens(
            task.get("max_tokens"), settings
        )
        # 修复轮的任务可能是 stage=0，但仍需要基线注入；调度器用
        # seed_from_workspace 显式标记，而不是靠 stage 推断。
        self.sequential = bool(task.get("seed_from_workspace"))
        self.baseline_paths = _baseline_paths(task)
        self.worktree = posixpath.join(
            root, ".agent", "worktrees", self.safe
        )
        self.worktree_parent = posixpath.join(
            root, ".agent", "worktrees"
        )
        self.base_ref = "HEAD"
        self.seed_files: list[str] = []
        self.seed_bytes = 0
        self.seed_error = ""
        self.created: Any = None
        self.tools: list[Any] = []
        self.tool_map: dict[str, BaseTool] = {}
        self.child_llm: Any = None
        self.messages: list[Any] = []
        self.records: list[dict[str, Any]] = []
        self.input_tokens = 0
        self.output_tokens = 0
        self.llm_ms = 0
        self.tool_ms = 0
        self.scope_violations: list[str] = []
        self.verification: dict[str, Any] = {}
        self.error = ""
        self.cancelled = False
        self.timed_out = False
        self.budget_exhausted = False

    async def run(self) -> WorkerResult:
        # 取消可能落在 _prepare 的 worktree 创建/基线注入上；finally 必须
        # 从准备阶段就开始生效，否则 worktree 会留在磁盘上。
        try:
            prepared = await self._prepare()
            if prepared is not None:
                return prepared
            await self._run_with_timeout()
            return await self._finish()
        finally:
            await self._cleanup_worktree()

    async def _prepare(self) -> WorkerResult | None:
        if not self.scopes:
            return self._failed("implement 任务没有 write_scope，拒绝执行")
        ready, reason = await self._check_git()
        if not ready:
            return self._failed(reason)
        if not await self._create_worktree():
            return self._failed(
                f"创建 worktree 失败：{self.created.render(400)}",
                error=self.created.stderr,
            )
        if self.sequential and not await self._seed():
            await self._cleanup_worktree()
            return self._failed(self.seed_error)
        self._build_agent()
        return None

    async def _call_thread(
        self, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Any:
        """`to_thread` + 追踪：取消时线程可能仍在跑，清理要等它退出。"""
        loop = asyncio.get_running_loop()
        # run_in_executor 不像 to_thread 那样自动复制 contextvars；工具执行
        # 上下文（审计/工作区）必须显式带进线程。
        ctx = contextvars.copy_context()
        future = loop.run_in_executor(
            None, ctx.run, functools.partial(fn, *args, **kwargs)
        )
        self._thread_futures.add(future)
        future.add_done_callback(self._thread_futures.discard)
        # shield：外层取消不应把 executor future 一起取消（线程本来也停不了）。
        return await asyncio.shield(future)

    async def _check_git(self) -> tuple[bool, str]:
        if self.sequential:
            return await self._call_thread(
                _git_ready,
                self.sandbox,
                self.root,
                allow_dirty_paths=self.baseline_paths,
            )
        return await self._call_thread(_git_ready, self.sandbox, self.root)

    async def _shell(
        self, command: str, *, kind: str, cwd: str | None = None
    ) -> Any:
        """把同步沙箱调用挪出事件循环，避免阻塞同批其它 worker。"""
        return await self._call_thread(
            host_run,
            self.sandbox,
            command,
            origin=HOST_ORIGIN,
            kind=kind,
            cwd=cwd or self.root,
        )

    def _failed(self, summary: str, *, error: str = "") -> WorkerResult:
        return WorkerResult(
            task_id=self.task_id,
            role="implement",
            worker_id=self.worker_id,
            goal=self.goal,
            status="failed",
            summary=summary,
            error=error,
            duration_ms=int((time.monotonic() - self.started) * 1000),
        )

    def _remove_worktree(self) -> None:
        try:
            host_run(
                self.sandbox,
                f"git worktree remove --force "
                f"{shlex.quote(self.worktree)}",
                origin=HOST_ORIGIN,
                kind="git_worktree",
                cwd=self.root,
            )
            host_run(
                self.sandbox,
                "git worktree prune",
                origin=HOST_ORIGIN,
                kind="git_worktree",
                cwd=self.root,
            )
        except Exception:  # noqa: BLE001 - 清理失败不覆盖原始结果
            pass

    async def _create_worktree(self) -> bool:
        # 清理守卫从**创建之前**就生效：取消可能正好落在下面某个 await
        # 上，此时 worktree 目录可能已经部分创建。
        # 取消时交给 run() 的 finally：那里会先等在飞线程退出再清理。
        await self._shell(
            f"mkdir -p {shlex.quote(self.worktree_parent)}",
            kind="worktree_prepare",
        )
        await self._shell(
            f"git worktree remove --force {shlex.quote(self.worktree)}",
            kind="git_worktree",
        )
        await self._shell(
            f"rm -rf {shlex.quote(self.worktree)}",
            kind="worktree_cleanup",
        )
        created = await self._shell(
            f"git worktree add --detach "
            f"{shlex.quote(self.worktree)} HEAD",
            kind="git_worktree",
        )
        self.created = created
        if created.ok:
            return True
        self._remove_worktree()
        return False

    async def _cleanup_worktree(self) -> None:
        """移除本次 worker 的 worktree；取消路径也必须执行。

        取消只停协程，不停 `to_thread` 里的 wsl.exe。若还有在飞的线程，
        不能立刻 `rm -rf`：那会和仍在写文件的进程竞态。改为等线程退出后
        再清理；等不到就留给下次启动的 orphan prune。
        """
        pending = {f for f in self._thread_futures if not f.done()}
        if pending:
            # 在 finally 里等到线程退出再清理；`_cleanup_after_threads`
            # 自带 30s 上限，等不到则留给 orphan prune。不能只 create_task
            # 后立刻返回，否则调用方会看到 worktree 已“取消但未清理”的
            # 中间态（集成测试与服务重启恢复都会踩到）。
            await self._cleanup_after_threads(pending)
            return
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            self._remove_worktree()
            return
        # 清理本身也被取消时，目录留在原地，交给 orphan prune。
        await asyncio.shield(self._call_thread(self._remove_worktree))

    async def _cleanup_after_threads(
        self, futures: set[asyncio.Future[Any]]
    ) -> None:
        loop = asyncio.get_running_loop()
        waits = [asyncio.wrap_future(f, loop=loop) for f in futures]
        try:
            await asyncio.wait_for(
                asyncio.gather(*waits, return_exceptions=True),
                timeout=30.0,
            )
        except (TimeoutError, asyncio.CancelledError):
            return
        try:
            await asyncio.to_thread(self._remove_worktree)
        except Exception:  # noqa: BLE001 - 清理失败留给 orphan prune
            return

    async def _seed(self) -> bool:
        (
            seeded,
            self.seed_error,
            self.base_ref,
            self.seed_files,
            self.seed_bytes,
        ) = await self._call_thread(
            _seed_worktree,
            self.sandbox,
            self.root,
            self.worktree,
            self.export_id,
            self.settings,
        )
        return seeded

    def _build_agent(self) -> None:
        scoped = self.settings.model_copy(
            update={"wsl_workspace": self.worktree}
        )
        # 写时边界：文件工具在 I/O 之前就会拒绝 write_scope 外的路径。
        # 事后 `_within_scope` 的 diff 校验仍保留，作为第二道防线。
        tools = [
            tool
            for tool in build_tools(
                scoped,
                allow_write=True,
                sandbox=self.sandbox,
                write_scope=(
                    self.scopes
                    if self.settings.orchestrator_write_scope_enforce
                    else None
                ),
                # worktree 内独立缓存；写路径会失效，不能与主工作区共享。
                read_cache=ReadCache(self.settings.read_cache_max_bytes),
            )
            if tool.name in IMPLEMENT_TOOL_NAMES
        ]
        # 静态白名单断言：implement worker 永远不应拿到 shell/git/deps/test。
        assert set(tool.name for tool in tools) <= IMPLEMENT_TOOL_NAMES
        self.tools = tools
        self.tool_map = {tool.name: tool for tool in tools}
        self.child_llm = self.llm.bind_tools(tools)
        self.messages = _build_worker_messages(
            task=self.task,
            worktree=self.worktree,
            scopes=self.scopes,
            max_rounds=self.max_rounds,
            memories=self.memories,
            instructions=self.instructions,
            parent_request=self.parent_request,
            session_brief=self.session_brief,
            repo_map=str(self.task.get("repo_map") or "").strip(),
            retry_note=self.retry_note,
        )

    async def _invoke_call(
        self, round_index: int, index: int, call: dict[str, Any]
    ) -> bool:
        """执行一条工具调用并回灌结果；返回 True 表示已被取消。"""
        if self.cancel is not None and self.cancel():
            return True
        name = str(call.get("name", ""))
        call_id = str(call.get("id", "")) or f"r{round_index}-{index}"
        call_args = dict(call.get("args") or {})
        tool = self.tool_map.get(name)
        tool_started = time.monotonic()
        artifact: dict[str, Any] | None = None
        if tool is None:
            content = (
                f"错误：实现 worker 没有 {name} 工具。"
                f"可用工具：{sorted(self.tool_map)}"
            )
            ok = False
        else:
            try:
                with tool_execution_context(
                    f"worker:{self.task_id}", call_id
                ):
                    raw = await self._call_thread(
                        tool.invoke,
                        call_args,
                        self.child_config,
                    )
                content, artifact = unpack(raw)
                ok = (
                    bool(artifact.get("ok", True))
                    if isinstance(artifact, dict)
                    else True
                )
            except Exception as exc:  # noqa: BLE001
                content = f"工具执行异常：{type(exc).__name__}: {exc}"
                ok = False
        duration_ms = int((time.monotonic() - tool_started) * 1000)
        self.tool_ms += duration_ms
        if (
            isinstance(artifact, dict)
            and artifact.get("rejected")
            and name in {WRITE_TOOL_NAME, EDIT_TOOL_NAME}
        ):
            bad_path = str(artifact.get("path") or "")
            if bad_path:
                self.scope_violations.append(bad_path)
        self.records.append(
            {
                "call_id": call_id,
                "name": name,
                "args": call_args,
                "ok": ok,
                "duration_ms": duration_ms,
                "round": round_index,
            }
        )
        self.messages.append(
            ToolMessage(
                content=content,
                tool_call_id=call_id,
                name=name,
            )
        )
        return False

    async def _run_rounds(self) -> str:
        for round_index in range(1, self.max_rounds + 1):
            if self.cancel is not None and self.cancel():
                self.cancelled = True
                return "cancelled"
            llm_started = time.monotonic()
            response = await retry_async(
                lambda: self.child_llm.ainvoke(
                    self.messages, self.child_config
                ),
                attempts=self.settings.retry_attempts,
                base_delay=self.settings.retry_base_delay_s,
                max_delay=self.settings.retry_max_delay_s,
                max_total_wait=self.settings.retry_max_total_wait_s,
                label=f"implement:{self.task_id}",
            )
            self.llm_ms += int(
                (time.monotonic() - llm_started) * 1000
            )
            used_in, used_out = _usage(response)
            self.input_tokens += used_in
            self.output_tokens += used_out
            self.messages.append(response)
            if (
                self.max_tokens > 0
                and self.input_tokens + self.output_tokens >= self.max_tokens
            ):
                self.budget_exhausted = True
                return "budget"
            calls = getattr(response, "tool_calls", None) or []
            if not calls:
                break
            for index, call in enumerate(calls):
                if await self._invoke_call(round_index, index, call):
                    self.cancelled = True
                    return "cancelled"
        return "done"

    async def _verify(self) -> str:
        changed = await self._call_thread(
            _changed_files, self.sandbox, self.worktree, self.base_ref
        )
        if not changed:
            return "no_changes"
        related = await self._call_thread(
            _impacted_tests, self.sandbox, self.worktree, list(changed)
        )
        result = await self._call_thread(
            run_verification,
            self.sandbox,
            self.worktree,
            allow_manifest=False,
            only=tuple(related),
            # 有相关测试时 worker 只跑子集；合并后的主 agent
            # 仍会跑完整 verify，避免多 worker 各跑一遍全量。
            full_after_selected=not related,
        )
        self.verification = result.model_dump()
        if result.status == "ok":
            return "ok"
        if _peer_dependency_failure(
            self.verification, list(changed), self.scopes
        ):
            self.verification["peer_dependency"] = True
            self.verification["note"] = (
                "测试依赖同批其它 implement worker 的改动；"
                "等待合并后由主 agent 完整验证"
            )
            return "peer"
        return "failed"

    async def _loop(self) -> None:
        repairs = int(
            self.task.get("max_repairs")
            if self.task.get("max_repairs") is not None
            else self.max_repairs
        )
        for attempt in range(repairs + 1):
            if await self._run_rounds() != "done":
                return
            outcome = await self._verify()
            if outcome in {"ok", "peer", "no_changes"}:
                return
            if attempt < repairs:
                self.messages.append(
                    HumanMessage(
                        content=format_verification_feedback(
                            self.verification,
                            attempt=attempt + 1,
                            limit=repairs + 1,
                        )
                    )
                )
                continue
            return

    async def _run_with_timeout(self) -> None:
        try:
            await asyncio.wait_for(self._loop(), timeout=self.timeout)
        except TimeoutError:
            self.timed_out = True
            self.error = f"实现 worker 超时（>{self.timeout}s）"
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
        if self.budget_exhausted and not self.error:
            self.error = f"任务 token 预算已用尽（>{self.max_tokens}）"
        # 取消可能在最后一条工具调用之后才到：此时补丁还没合并，
        # 直接丢弃 worktree 比先落 patch 再由 dispatch 拦更干净。
        if not self.cancelled and self.cancel is not None and self.cancel():
            self.cancelled = True

    async def _finish(self) -> WorkerResult:
        if self.cancelled:
            return WorkerResult(
                task_id=self.task_id,
                role="implement",
                worker_id=self.worker_id,
                goal=self.goal,
                status="cancelled",
                summary="已按用户要求取消，改动未合并。",
                input_tokens=self.input_tokens,
                output_tokens=self.output_tokens,
                duration_ms=int(
                    (time.monotonic() - self.started) * 1000
                ),
                llm_ms=self.llm_ms,
                tool_ms=self.tool_ms,
                timed_out=self.timed_out,
                scope_violations=list(self.scope_violations),
                tool_calls=self.records,
                seed_files=list(self.seed_files),
                seed_bytes=self.seed_bytes,
                budget_used={
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "tool_calls": len(self.records),
                },
            )

        changed = await self._call_thread(
            _changed_files, self.sandbox, self.worktree, self.base_ref
        )
        out_of_scope = [
            path
            for path in changed
            if not _within_scope(path, self.scopes)
        ]
        patch_path = ""
        status = "failed"
        summary = (
            text_of(self.messages[-1]).strip() if self.messages else ""
        )

        if out_of_scope:
            status = "failed"
            self.error = (
                self.error
                or f"修改了 write_scope 外的文件：{', '.join(out_of_scope)}"
            )
        elif not changed:
            status = "failed"
            self.error = self.error or "实现 worker 没有产生任何文件改动"
        else:
            patch_path = await self._call_thread(
                _make_patch,
                self.sandbox,
                self.worktree,
                self.root,
                self.export_id,
                self.base_ref,
            )
            if not patch_path:
                status = "failed"
                self.error = self.error or "无法生成补丁"
            elif self.verification.get("status") == "ok":
                status = "success"
            else:
                status = "partial"

        if not summary:
            summary = (
                str(self.verification.get("summary") or "")
                or self.error
                or status
            )
        findings = [
            f"{issue.get('location', '')} {issue.get('message', '')}".strip()
            for issue in (self.verification.get("issues") or [])
        ]
        if out_of_scope:
            findings.append("越界修改：" + ", ".join(out_of_scope))

        return WorkerResult(
            task_id=self.task_id,
            role="implement",
            worker_id=self.worker_id,
            goal=self.goal,
            status=status,
            summary=summary,
            files=changed,
            findings=findings,
            verification=self.verification,
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            duration_ms=int((time.monotonic() - self.started) * 1000),
            llm_ms=self.llm_ms,
            tool_ms=self.tool_ms,
            timed_out=self.timed_out,
            error=self.error,
            patch_path=patch_path,
            tool_calls=self.records,
            seed_files=list(self.seed_files),
            seed_bytes=self.seed_bytes,
            scope_violations=sorted(
                {*self.scope_violations, *out_of_scope}
            ),
            budget_used={
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "tool_calls": len(self.records),
            },
        )


def make_implement_runner(
    *,
    llm: BaseChatModel,
    sandbox: WslSandbox,
    root: str,
    settings: Settings,
) -> ImplementRunner:
    default_rounds = max(int(settings.orchestrator_worker_rounds), 1)
    max_repairs = max(int(settings.orchestrator_worker_repairs), 0)
    default_timeout = max(int(settings.orchestrator_worker_timeout), 1)

    async def run(
        task: dict[str, Any],
        *,
        parent_request: str,
        cwd: str = "",
        config: RunnableConfig | None = None,
        cancel: Callable[[], bool] | None = None,
        session_brief: str = "",
        memories: list[str] | None = None,
        instructions: str = "",
    ) -> WorkerResult:
        engine = _ImplementRun(
            llm=llm,
            sandbox=sandbox,
            root=root,
            settings=settings,
            default_rounds=default_rounds,
            max_repairs=max_repairs,
            default_timeout=default_timeout,
            task=task,
            parent_request=parent_request,
            cwd=cwd,
            config=config,
            cancel=cancel,
            session_brief=session_brief,
            memories=memories,
            instructions=instructions,
        )
        return await engine.run()

    return run
