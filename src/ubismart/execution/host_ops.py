"""宿主侧内部执行：合并 / worktree 维护等确定性 git 操作的唯一入口。

模型发起的命令一律走 `tools/` → 审批 → 沙箱；本模块只服务宿主自己：
implement 补丁合并、worktree 生命周期、仓库画像与补丁生成。这些操作不
经过命令分级，因此调用方必须**显式声明** `origin="host"` 与 `kind`，
且 `kind` 必须在白名单里、命令前缀必须与 `kind` 匹配。

未知 origin 或未登记的 kind 一律拒绝（fail-closed）。新增一条宿主路径时，
先更新 ADR-0011 与这份白名单，再补测试。
"""

from __future__ import annotations

import re
from typing import Any

HOST_ORIGIN = "host"

# kind -> 允许的命令前缀。前缀是第二道校验：光声明 kind 不能执行任意命令。
HOST_KINDS: dict[str, tuple[str, ...]] = {
    # 补丁合并（check / numstat / apply / apply -R / --3way）
    "git_apply": ("git apply",),
    # 3-way 预演用的临时 index
    "git_read_tree": ("git read-tree",),
    "git_reset": ("git reset",),
    # worktree 创建 / 移除 / 列表 / prune
    "git_worktree": ("git worktree",),
    # 补丁生成：暂存与 diff
    "git_patch_prepare": ("git add ", "git diff "),
    # 写 worker 前置检查脚本（一次进程完成多条只读 git 查询）
    "git_probe": ("inside=$(git",),
    # 顺序写：读取主工作区 tracked/untracked 改动，生成基线补丁
    "git_seed_probe": ("git diff ", "git ls-files "),
    # 顺序写：在 worktree 内记录 seed 后的基线树（不移动 HEAD）
    "git_seed_baseline": ("git add ", "git write-tree", "git reset"),
    # worktree 目录准备与清理
    "worktree_prepare": ("mkdir -p ",),
    "worktree_cleanup": ("rm -rf ",),
    # 3-way 临时 index 清理
    "merge_temp_cleanup": ("rm -f ",),
    # 合并前读取主工作区文件内容（批量快照）
    "snapshot_read": ("python3 - ",),
    # 编排前的只读仓库画像
    "repo_survey": ('echo "AGENT_DIRS"',),
    # 工作区导入：准备 staging、tar 管道复制、失败清理（RFC-0001）
    "workspace_import_prepare": ("mkdir -p ",),
    "workspace_import": ("tar ",),
    "workspace_import_cleanup": ("rm -rf ",),
    # revision：本地 git 初始化/提交、回滚、归档删除
    "workspace_git_init": ("git init",),
    "workspace_revision_add": ("git add ",),
    "workspace_revision_commit": ("git commit ",),
    "workspace_revision_probe": ("git rev-parse ",),
    "workspace_rollback": ("git reset --hard ",),
    "workspace_archive_delete": ("rm -f ",),
}


class HostExecutionDenied(PermissionError):
    """宿主执行路径未被白名单授权。"""


def check_host_origin(origin: str) -> None:
    if origin != HOST_ORIGIN:
        raise HostExecutionDenied(
            f"拒绝宿主执行：origin={origin!r}，只允许 {HOST_ORIGIN!r}"
        )


# 只剥离**大写**命名的环境变量赋值（本项目用 `GIT_INDEX_FILE=...`），
# 避免把 shell 变量赋值 `inside=$(git ...)` 误当成 env 前缀。
_ENV_ASSIGNMENT = re.compile(
    r"""[A-Z_][A-Z0-9_]*=(?:'[^']*'|"[^"]*"|\S+)\s+"""
)


def _strip_env_assignments(command: str) -> str:
    """校验命令前缀时忽略 `KEY='value' cmd` 形式的环境变量前缀。"""
    rest = command.lstrip()
    while True:
        match = _ENV_ASSIGNMENT.match(rest)
        if match is None:
            return rest
        rest = rest[match.end() :]


def host_run(
    sandbox: Any,
    command: str,
    *,
    origin: str,
    kind: str,
    cwd: str,
) -> Any:
    """执行一条宿主侧内部命令；未知 origin/kind 或命令不匹配一律拒绝。"""
    check_host_origin(origin)
    prefixes = HOST_KINDS.get(kind)
    if prefixes is None:
        raise HostExecutionDenied(f"拒绝宿主执行：未知 kind={kind!r}")
    normalized = _strip_env_assignments(command)
    if not any(normalized.startswith(prefix) for prefix in prefixes):
        raise HostExecutionDenied(
            f"拒绝宿主执行：kind={kind!r} 不允许命令 {command[:120]!r}"
        )
    return sandbox.run(command, cwd=cwd)
