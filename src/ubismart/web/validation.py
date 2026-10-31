"""Web 入参校验。"""

from __future__ import annotations

import re
import uuid

from fastapi import HTTPException

_THREAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def check_thread_id(thread_id: str) -> str:
    text = str(thread_id or "").strip()
    if not text:
        return uuid.uuid4().hex[:12]
    if not _THREAD_RE.match(text):
        raise HTTPException(
            status_code=422,
            detail="thread_id 只允许字母、数字、下划线、连字符，长度 1–64",
        )
    return text
