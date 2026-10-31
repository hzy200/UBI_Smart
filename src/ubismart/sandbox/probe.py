"""沙箱探活：`doctor` / `sandbox-init` 需要的几件固定小事。

为什么放在 A4 而不是 CLI：前端层不得有执行能力（不变量 5），但 `doctor` 又
必须能回答"沙箱跑不跑得起来、跑在谁名下、有没有 ripgrep"。这里把这几个问题
做成**固定意图**（`identity` / `which`），不提供"执行任意命令"的入口 ——
也就没有需要兜底的命令分级，这正是 TD-032 / TD-034 收口后的形态。

目录相关的两件事（存在性、创建）走 `SandboxFs`，复用它的 realpath 守卫。
"""

from __future__ import annotations

import posixpath
import re
import shlex
from dataclasses import dataclass

from coding_agent.sandbox.fs import SandboxFs, SandboxFsError
from coding_agent.sandbox.pathguard import SandboxPathError
from coding_agent.sandbox.wsl_exec import WslSandbox

# 拼进 `command -v` 的名字：只允许普通命令名，杜绝空格/分隔符/重定向。
_NAME_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,64}$")


class SandboxProbeError(RuntimeError):
    """探活无法完成（沙箱不可用、输出无法解析、命令名非法）。"""


@dataclass(frozen=True, slots=True)
class ShellIdentity:
    """沙箱里的运行身份；`kernel` 取不到时为空串，不影响用户判定。"""

    user: str
    kernel: str


def identity(sandbox: WslSandbox) -> ShellIdentity:
    """一次调用拿到运行用户与内核版本。

    "沙箱能不能执行命令"不再单独探一次 —— 这条命令本身跑通就说明能执行，
    顺带把 `uname -r` 一起取回来，比原来两条命令各起一个进程省一半。
    """
    script = (
        'echo "USER=$(whoami 2>/dev/null)"\n'
        'echo "KERNEL=$(uname -r 2>/dev/null)"\n'
    )
    result = sandbox.run(script)
    if not result.ok:
        raise SandboxProbeError(f"探活失败：{result.render(300)}")
    return ShellIdentity(
        user=_value(result.stdout, "USER"),
        kernel=_value(result.stdout, "KERNEL"),
    )


def which(sandbox: WslSandbox, name: str) -> str:
    """命令是否可执行：返回路径，空串表示没装。"""
    if not _NAME_RE.match(str(name or "")):
        raise SandboxProbeError(f"非法的命令名：{name!r}")
    # `|| true`：没装时 `command -v` 返回非零，不该被当成"探活失败"。
    result = sandbox.run(f"command -v {shlex.quote(name)} 2>/dev/null || true")
    if not result.ok:
        raise SandboxProbeError(f"探活失败：{result.render(300)}")
    first = result.stdout.strip().splitlines()
    return first[0].strip() if first else ""


def dir_exists(sandbox: WslSandbox, path: str) -> bool:
    """路径是否是已存在的**目录**（符号链接指向文件时返回 False）。

    探不到（沙箱不可用、路径越界）一律按"不存在"处理：doctor 的这行输出是
    提示性的，不值得为它中断整个体检。
    """
    try:
        info = SandboxFs(sandbox, _parent(path)).stat(path)
    except (SandboxFsError, SandboxPathError):
        return False
    return info.exists and not info.is_file


def ensure_dir(sandbox: WslSandbox, path: str) -> str:
    """创建目录（含父目录），返回 realpath。"""
    try:
        return SandboxFs(sandbox, _parent(path)).mkdir(path)
    except (SandboxFsError, SandboxPathError) as exc:
        raise SandboxProbeError(f"无法创建工作区目录 {path}：{exc}") from exc


def _parent(path: str) -> str:
    """用父目录当 `SandboxFs` 的根：探活的目标可能是根下第一层目录。"""
    return posixpath.dirname(str(path)) or "/"


def _value(stdout: str, key: str) -> str:
    for line in stdout.splitlines():
        head, sep, rest = line.partition("=")
        if sep and head.strip() == key:
            return rest.strip()
    return ""
