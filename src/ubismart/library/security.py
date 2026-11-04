"""资料库内容的安全清洗。

资料库正文是不可信输入：可能带终端控制序列、超长空白或提示注入文本。
这里只做**数据层清洗**（控制序列/长度），不删除资料原意；提示注入的
最终闸门仍是工具等级与审批。
"""

from __future__ import annotations

import re

from ubismart.protocol.terminal_text import sanitize_terminal_text

_MAX_BLANK_LINES = re.compile(r"\n{4,}")
# 资料片段不写终端，SGR 颜色也一并去掉（保留会把 ESC 带进模型/前端）。
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")


def sanitize_excerpt(text: str, *, max_chars: int = 1_200) -> str:
    """去掉控制序列、压缩连续空行并截断；不剥离正常代码/标点。"""
    cleaned = _SGR_RE.sub("", sanitize_terminal_text(str(text or "")))
    cleaned = _MAX_BLANK_LINES.sub("\n\n\n", cleaned).strip()
    if max_chars > 0 and len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars].rstrip() + "…"
    return cleaned
