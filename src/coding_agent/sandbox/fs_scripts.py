"""`SandboxFs` 的脚本构造与输出解析（纯函数，不启动进程）。

这些函数是 WSL 边界的“协议层”：输入路径/原始 stdout，输出脚本或结构化
结果。拆出来是为了让它们能在没有 WSL 的机器上被直接单测。
"""

from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass


class SandboxFsError(RuntimeError):
    """文件系统操作失败（沙箱不可用、路径非法等）。"""


class SandboxFsTooLarge(SandboxFsError):
    """文件超过了本次操作允许的大小上限。

    单独成一个类型，是为了让调用方能**只对这一种失败**给退路：
    `file_read` 有 offset/limit 可以分段读，`file_edit` 没有。
    早先统一在错误后面拼一句「请用 offset/limit 分段读取」，
    于是文件不存在、是二进制时也被这么提示，模型会照着再试一轮白烧工具预算。
    """


@dataclass(slots=True)
class FileStat:
    exists: bool
    is_file: bool
    size: int
    real_path: str
    # 修改时间（epoch 秒，整数精度即可）。目录也有 mtime，用来判定锁是否过期：
    # 早先这条信息只能靠 `stat -c %Y` 拼脚本，等于把 shell 又开了一个口子。
    mtime: float = 0.0


def quote(path: str) -> str:
    return shlex.quote(path)


def parse_kv(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            result[key.strip()] = value
    return result


# 写盘时抢占文件失败的典型 errno 文案。这些是**瞬时**的：别的进程
# （编辑器、构建工具）短暂持有文件。值得短退避重试，而不是直接报给模型。
WRITE_RETRY_HINTS = (
    "text file busy",
    "device or resource busy",
    "resource temporarily unavailable",
    "temporarily unavailable",
    "interrupted system call",
    "ebusy",
    "etxtbsy",
    "eagain",
    "eintr",
)

# 重试次数与退避（秒）。总等待不超过 1s，避免把一次工具调用拖长。
WRITE_RETRY_ATTEMPTS = 3
WRITE_RETRY_DELAYS = (0.05, 0.15)


def is_retryable_write_failure(stderr: str) -> bool:
    text = (stderr or "").lower()
    return any(hint in text for hint in WRITE_RETRY_HINTS)


def stat_script(
    path: str,
    *,
    include_data: bool = False,
    max_bytes: int | None = None,
) -> str:
    """一次进程启动里同时拿到 realpath / 类型 / 大小（可选：内容）。

    每次 `wsl.exe` 调用都是一次进程启动（实测 0.2–0.3s），
    所以"顺手多查一点"比"多跑一趟"划算得多 —— 早先的实现读一个文件要启动三次。

    `max_bytes` 给了上限时，**脚本内**先比大小再决定是否 `base64`：
    若放在 Python 侧先读后拒，整份文件已经物化进内存，上限就形同虚设。
    """
    # 存在性判定用 `-e || -L`：悬空符号链接的 `-e` 为假，若走了 else 分支，
    # REAL 会退化成链接自身路径（在工作区内），realpath 校验就成了摆设。
    script = f"""\
p={quote(path)}
if [ -e "$p" ] || [ -L "$p" ]; then
  size=$(stat -c %s -- "$p" 2>/dev/null || echo 0)
  mtime=$(stat -c %Y -- "$p" 2>/dev/null || echo 0)
  if [ -e "$p" ]; then echo "EXISTS=1"; else echo "EXISTS=0"; fi
  if [ -f "$p" ]; then echo "FILE=1"; else echo "FILE=0"; fi
  echo "SIZE=$size"
  echo "MTIME=$mtime"
  echo "REAL=$(realpath -m -- "$p")"
else
  size=0
  echo "EXISTS=0"
  echo "FILE=0"
  echo "SIZE=0"
  echo "MTIME=0"
  echo "REAL=$(realpath -m -- "$(dirname -- "$p")")/$(basename -- "$p")"
fi
"""
    if include_data:
        # 文件不存在 / 不是普通文件 → 不输出 DATA；
        # 超过上限 → 同样不读（脚本里就不跑 base64）。
        guard = "" if max_bytes is None else f' && [ "$size" -le {int(max_bytes)} ]'
        script += f'if [ -f "$p" ]{guard}; then echo "DATA=$(base64 -w0 -- "$p")"; fi\n'
    return script


def parse_stat(stdout: str) -> tuple[FileStat, bytes | None]:
    data = parse_kv(stdout)
    info = FileStat(
        exists=data.get("EXISTS") == "1",
        is_file=data.get("FILE") == "1",
        size=int(data.get("SIZE") or 0),
        real_path=data.get("REAL", ""),
        mtime=float(data.get("MTIME") or 0),
    )
    raw = data.get("DATA")
    if raw is None:
        # 没有 DATA 行：文件不存在 / 非普通文件 / 超过上限未读
        return info, None
    if raw == "":
        # DATA= 是「读到了，就是空的」——空文件必须可读，不能当成失败
        return info, b""
    try:
        return info, base64.b64decode(raw, validate=True)
    except ValueError as exc:
        raise SandboxFsError(f"读取结果解码失败：{exc}") from exc


def wrap(payload: str, width: int = 76) -> str:
    """base64 折行，避免脚本里出现超长单行。"""
    return "\n".join(payload[i : i + width] for i in range(0, len(payload), width))
