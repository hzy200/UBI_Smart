"""终端文本清洗：阻止模型/工具输出注入控制序列。

只保留 SGR 颜色序列（`ESC[...m`），删除 OSC/DCS/APC 以及光标、清屏、
窗口标题、剪贴板等控制序列。CLI/TUI 在把外部文本交给终端前必须经过这里。
"""

from __future__ import annotations

import re

# OSC / DCS / APC / PM：以 BEL 或 ST（ESC \）结束，可能跨行。
_ESC_STRING_RE = re.compile(
    r"\x1b[\]P^_][\s\S]*?(?:\x07|\x1b\\)"
)
_CSI_RE = re.compile(r"\x1b\[([0-9;:?]*)([ -/]*)([@-~])")
_ESC_RE = re.compile(r"\x1b(?!\[)[@-_]")
# 末尾/孤立的 ESC（没有可识别的后续序列）。
_LONE_ESC_RE = re.compile(r"\x1b(?!\[)")
# C0 控制字符：只保留 \n 与 \t；\r 也会覆盖当前行，一并去掉。
# ESC(0x1b) 单独由上面的序列规则处理，不能在这里再次删掉。
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1a\x1c-\x1f\x7f]")


def _sgr(match: re.Match[str]) -> str:
    params, intermediates, final = match.group(1, 2, 3)
    if final != "m" or intermediates or "?" in params or ":" in params:
        return ""
    if not params:
        return "\x1b[0m"
    if all(part.isdigit() for part in params.split(";") if part):
        return match.group(0)
    return ""


def sanitize_terminal_text(value: object) -> str:
    """返回可安全写入终端的文本；无效输入按空串处理。"""
    text = str(value or "")
    if not text:
        return ""
    text = _ESC_STRING_RE.sub("", text)
    text = _CSI_RE.sub(_sgr, text)
    text = _ESC_RE.sub("", text)
    text = _LONE_ESC_RE.sub("", text)
    return _CTRL_RE.sub("", text)
