"""会话级文件访问模式。

放在中立层（不依赖 tools / sandbox），供前端、runtime 与策略层共用。
空串表示沿用旧的 `allow_write + approval_mode` 语义。
"""

from __future__ import annotations

ACCESS_READ_ONLY = "read_only"
ACCESS_WRITE_ASK = "write_ask"
ACCESS_WRITE_AUTO = "write_auto"
ACCESS_MODES = frozenset(
    {ACCESS_READ_ONLY, ACCESS_WRITE_ASK, ACCESS_WRITE_AUTO}
)

# UI 展示用标签；未知值按只读处理。
ACCESS_LABELS = {
    ACCESS_READ_ONLY: "只读",
    ACCESS_WRITE_ASK: "审批写",
    ACCESS_WRITE_AUTO: "自动写",
}


def normalize_access_mode(value: str | None) -> str:
    """归一化外部输入；空值保持为空（旧语义），未知值按最保守的只读处理。"""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    return text if text in ACCESS_MODES else ACCESS_READ_ONLY


def access_mode_from_allow_write(allow_write: bool) -> str:
    """旧 CLI/TUI 的 `--write` 语义：开启时等价于 write_auto。"""
    return ACCESS_WRITE_AUTO if allow_write else ""


_ACCESS_RANK = {
    ACCESS_READ_ONLY: 0,
    ACCESS_WRITE_ASK: 1,
    ACCESS_WRITE_AUTO: 2,
}


def clamp_access_mode(requested: str | None, limit: str | None) -> str:
    """把请求的访问模式压到账号上限以内；未知值按最保守的只读处理。"""
    current = normalize_access_mode(requested) or ACCESS_READ_ONLY
    cap = normalize_access_mode(limit) or ACCESS_WRITE_AUTO
    if _ACCESS_RANK[current] <= _ACCESS_RANK[cap]:
        return current
    return cap
