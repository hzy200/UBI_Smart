"""A2 Web 的共享请求/会话模型。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from coding_agent.access import ACCESS_READ_ONLY


class SessionRequest(BaseModel):
    """新建/绑定一个会话。

    workspace 与 access_mode 在**服务端**绑定，之后不能由单次 run 请求改写。
    """

    thread_id: str = Field(default="", max_length=64)
    workspace: str = Field(default="", max_length=4096)
    access_mode: Literal["read_only", "write_ask", "write_auto"] = "read_only"
    model: str = Field(default="", max_length=200)
    provider: str = Field(default="", max_length=64)


class ModelRequest(BaseModel):
    model: str = Field(default="", max_length=200)
    provider: str = Field(default="", max_length=64)


class RenameRequest(BaseModel):
    title: str = Field(default="", max_length=120)


@dataclass
class SessionConfig:
    thread_id: str
    workspace: str
    access_mode: str = ACCESS_READ_ONLY
    model: str = ""
    provider: str = ""
    uploaded_bytes: int = 0
    # GUI 重命名；空 = 使用首条提示词生成的标题。
    title: str = ""
    created_at: float = field(default_factory=time.monotonic)
