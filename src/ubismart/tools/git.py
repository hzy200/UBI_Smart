"""Git 工具。

为什么要做成结构化工具而不是让模型拼 `shell_exec "git ..."`：

- **参数不能逃逸**：提交信息、路径、分支名里出现空格、引号、`;` 时，
  由宿主逐参数 `shlex.quote`，模型无法构造出第二条命令。
- **等级可判定**：`git add` 只动索引（L1），`git commit` 产生提交（L2），
  分级不再依赖对整条命令字符串的正则猜测。
- **审计可读**：日志里记的是 `git_commit` + 结构化参数，而不是一串原始命令。

另外这里守一条纪律：**agent 自己的工作目录（`.agent/`，放备份与审计）不许进版本控制**。
它落在工作区里，一个 `git add -A` 就会把 agent 的备份提交进用户的仓库。
"""

from __future__ import annotations

import posixpath
import shlex

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from coding_agent.config import Settings
from coding_agent.sandbox.pathguard import SandboxPathError, ensure_inside, is_within
from coding_agent.sandbox.policy import CommandLevel
from coding_agent.sandbox.snapshots import AGENT_STATE_DIRNAME
from coding_agent.sandbox.wsl_exec import WslSandbox, resolve_workspace
from coding_agent.security.read_policy import protected_read_reason
from coding_agent.security.secrets import redact_secrets
from coding_agent.tools.artifacts import ShellArtifact, pack

GIT_STATUS = "git_status"
GIT_DIFF = "git_diff"
GIT_LOG = "git_log"
GIT_ADD = "git_add"
GIT_COMMIT = "git_commit"

# 子命令对应的风险等级；用于回报与展示，真正的放行由 approval_gate 决定
_SUBCOMMAND_LEVELS: dict[str, CommandLevel] = {
    "status": CommandLevel.READ,
    "diff": CommandLevel.READ,
    "log": CommandLevel.READ,
    "add": CommandLevel.LOW_WRITE,
    "commit": CommandLevel.MUTATE,
}

CWD_DESCRIPTION = "仓库所在目录（WSL 绝对路径或工作区内相对路径），省略则用工作区根目录"


def _header_paths(head: str) -> list[str]:
    """从 `diff --git` 行的剩余部分取出 a/b 两端路径。

    git 在路径含空格或特殊字符时会加 C 风格引号（`"a/my file"`），所以先按
    shell 规则切；切不动就退回按空白切 —— 两种都比"整行 split()"更保守。
    """
    try:
        parts = shlex.split(head)
    except ValueError:
        parts = head.split()
    return [part[2:] for part in parts[:2] if part[:2] in ("a/", "b/")]


def _protected_paths(output: str, extra_globs: str) -> list[str]:
    """从 git 输出里找出命中「秘密文件读保护」的路径。

    `git diff` / `git log -p` 会把文件内容整段塞进模型上下文。一个被误提交
    的 `.env`，`file_read` 挡得住（路径与 realpath 都查），这里的 diff 却
    挡不住 —— 值⁄路径两个维度必须都堵：

    - 值：`redact_secrets` 只能认出「看起来像密钥」的字符串，配置里的明文
      口令、数据库连接串躲得过它；
    - 路径：按文件清单再判一次，命中的整个输出都不返回。

    只认 `diff --git` 头与 `rename from/to` 两类元数据行，**不解析**
    `--- `/`+++ `：这两种前缀在 `git log -p` 的**提交正文**里同样可能出现
    （"+++ 补充说明"），按行首解析会把合法输出整份拒掉。`diff --git` 行本身
    就带着 a/b 两端路径，信息不会丢。
    """
    candidates: list[str] = []
    for line in output.splitlines():
        stripped = line.strip()
        if stripped.startswith("diff --git "):
            candidates.extend(_header_paths(stripped[len("diff --git ") :]))
        elif stripped.startswith(("rename from ", "rename to ")):
            candidates.append(stripped.split(" ", 2)[-1].strip())
    hits: list[str] = []
    for candidate in candidates:
        clean = candidate.strip()
        if not clean or clean in hits:
            continue
        if protected_read_reason(clean, extra_globs):
            hits.append(clean)
    return hits


class _GitInput(BaseModel):
    reason: str = Field(description="这次 git 操作的意图，一句话说明")
    cwd: str | None = Field(default=None, description=CWD_DESCRIPTION)


class GitStatusInput(_GitInput):
    pass


class GitDiffInput(_GitInput):
    path: str | None = Field(default=None, description="只看某个文件或目录的改动")
    staged: bool = Field(default=False, description="是否查看已暂存（--cached）的改动")


class GitLogInput(_GitInput):
    limit: int = Field(default=10, ge=1, le=100, description="显示多少条提交")


class GitAddInput(_GitInput):
    paths: list[str] = Field(description="要暂存的文件或目录，工作区内路径")


class GitCommitInput(_GitInput):
    message: str = Field(description="提交信息，一句话说明这次改动做了什么")


def build_git_tools(settings: Settings, sandbox: WslSandbox) -> list[BaseTool]:
    workspace = resolve_workspace(settings, sandbox)
    agent_state = posixpath.join(workspace, AGENT_STATE_DIRNAME)

    def _is_agent_state(path: str) -> bool:
        """这个暂存目标会不会把 `.agent/` 一起带进去。

        两种情形都要认（A7）：

        1. 目标就在 `.agent/` 里；
        2. 目标**是 `.agent/` 的祖先** —— `git_add(".")` 经 `ensure_inside(".")`
           正好归一化成工作区根，而 `git add -- <工作区根>` 会把备份与审计一并
           暂存。原先只判第一种，于是 `.` 与工作区根整个绕过了过滤。
        """
        return is_within(path, agent_state) or is_within(agent_state, path)

    def _reject(message: str, level: CommandLevel) -> str:
        return pack(
            message,
            ShellArtifact(
                ok=False,
                rejected=True,
                decision="rejected",
                level=int(level),
                level_label=level.label,
            ),
        )

    def _invoke(args: list[str], reason: str, cwd: str | None) -> str:
        level = _SUBCOMMAND_LEVELS.get(args[0], CommandLevel.MUTATE)
        try:
            workdir = ensure_inside(cwd, workspace) if cwd else workspace
        except SandboxPathError as exc:
            artifact = ShellArtifact(
                ok=False,
                rejected=True,
                level=int(level),
                level_label=level.label,
                decision="rejected",
            )
            return pack(f"工作目录非法：{exc}", artifact)

        command = " ".join(["git", *(shlex.quote(part) for part in args)])
        result = sandbox.run(command, cwd=workdir)
        rendered = result.render(settings.max_output_chars)
        leaked = _protected_paths(rendered, settings.read_deny_globs)
        if leaked:
            return _reject(
                f"拒绝返回：这次 git 输出涉及受保护的秘密文件（{', '.join(leaked)}）。\n"
                f"这些文件的内容不进模型上下文 —— 需要检查请让用户读对应的 "
                f".example/.sample 模板，或由管理员调整 AGENT_READ_DENY_GLOBS。",
                level,
            )
        artifact = ShellArtifact(
            command=command,
            ok=result.ok,
            exit_code=result.exit_code,
            duration_ms=result.duration_ms,
            timed_out=result.timed_out,
            level=int(level),
            level_label=level.label,
        )
        # 输出直接进模型上下文；即使命令本身合法（`git log -p`、别人的提交），
        # 也可能带出凭据。回灌前统一脱敏 —— 与 shell/file_read 同一道底线。
        text = (
            f"[{level.label}] {reason}\n$ {command}\n{redact_secrets(rendered)}"
        )
        return pack(text, artifact)

    def _status(reason: str, cwd: str | None = None) -> str:
        return _invoke(["status", "--short", "--branch"], reason, cwd)

    def _diff(
        reason: str, path: str | None = None, staged: bool = False, cwd: str | None = None
    ) -> str:
        args = ["diff"]
        if staged:
            args.append("--cached")
        if path:
            try:
                target = ensure_inside(path, workspace, cwd=workspace)
            except SandboxPathError as exc:
                return pack(f"路径非法：{exc}", ShellArtifact(ok=False, rejected=True))
            # 与 file_read 同一口径：显式要看某个秘密文件的 diff，等同于读它。
            protected = protected_read_reason(target, settings.read_deny_globs)
            if protected:
                return _reject(protected, CommandLevel.READ)
            args += ["--", target]
        return _invoke(args, reason, cwd)

    def _log(reason: str, limit: int = 10, cwd: str | None = None) -> str:
        return _invoke(
            ["log", f"--max-count={int(limit)}", "--oneline", "--decorate"], reason, cwd
        )

    def _add(reason: str, paths: list[str], cwd: str | None = None) -> str:
        if not paths:
            return _reject("paths 不能为空：请明确要暂存哪些文件。", CommandLevel.LOW_WRITE)
        try:
            targets = [ensure_inside(p, workspace, cwd=workspace) for p in paths]
        except SandboxPathError as exc:
            return _reject(f"路径非法：{exc}", CommandLevel.LOW_WRITE)

        intruding = [t for t in targets if _is_agent_state(t)]
        if intruding:
            return _reject(
                f"拒绝暂存 agent 自己的工作目录（或它的上层目录）：{', '.join(intruding)}。\n"
                f"`{AGENT_STATE_DIRNAME}/` 存放备份与审计，不应纳入版本控制 —— "
                f"暂存它的上层目录会把它一并带进去。请逐个列出要暂存的文件。",
                CommandLevel.LOW_WRITE,
            )
        return _invoke(["add", "--", *targets], reason, cwd)

    def _staged_agent_state(cwd: str | None) -> list[str]:
        """列出已经暂存、且属于 agent 自己目录的文件。"""
        workdir = ensure_inside(cwd, workspace) if cwd else workspace
        probe = sandbox.run("git diff --cached --name-only", cwd=workdir)
        if not probe.ok:
            return []  # 拿不到就交给 git commit 自己报错
        return [
            line.strip()
            for line in probe.stdout.splitlines()
            if line.strip()
            and _is_agent_state(posixpath.join(workdir, line.strip()))
        ]

    def _commit(reason: str, message: str, cwd: str | None = None) -> str:
        if not message.strip():
            return _reject("提交信息不能为空。", CommandLevel.MUTATE)

        try:
            staged = _staged_agent_state(cwd)
        except SandboxPathError as exc:
            return _reject(f"路径非法：{exc}", CommandLevel.MUTATE)

        if staged:
            # 用 shell 的 `git add -A` 绕过 add 工具时，这里是最后一道闸
            return _reject(
                f"拒绝提交：暂存区里有 agent 自己的工作目录文件（{', '.join(staged)}）。\n"
                f"请先取消暂存它们（git restore --staged <路径>），再重新提交。",
                CommandLevel.MUTATE,
            )
        return _invoke(["commit", "-m", message], reason, cwd)

    return [
        StructuredTool.from_function(
            func=_status,
            name=GIT_STATUS,
            description="查看仓库当前分支与改动概况。只读。",
            args_schema=GitStatusInput,
        ),
        StructuredTool.from_function(
            func=_diff,
            name=GIT_DIFF,
            description="查看工作区改动的内容。只读。staged=true 时看已暂存的改动。",
            args_schema=GitDiffInput,
        ),
        StructuredTool.from_function(
            func=_log,
            name=GIT_LOG,
            description="查看最近的提交历史。只读。",
            args_schema=GitLogInput,
        ),
        StructuredTool.from_function(
            func=_add,
            name=GIT_ADD,
            description="把指定文件加入暂存区。只影响索引，不会产生提交。",
            args_schema=GitAddInput,
        ),
        StructuredTool.from_function(
            func=_commit,
            name=GIT_COMMIT,
            description=(
                "提交已暂存的改动。会写入仓库历史，需要用户确认。"
                "提交前请先用 git_status / git_diff 确认改动内容。"
            ),
            args_schema=GitCommitInput,
        ),
    ]
