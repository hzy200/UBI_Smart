"""A2 Web 的共享请求/会话模型。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, Field

from coding_agent.access import ACCESS_READ_ONLY


class RunRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=200_000)
    thread_id: str = Field(default="", max_length=64)
    # Web 刻意不提供写会话：所有写操作都要求本机 CLI/TUI 的显式 --write。
    # 旧版这里有一个 allow_write 字段，但从未传给 AgentRuntime，属于误导性 API。


class SteerRequest(BaseModel):
    action: Literal["pause", "cancel", "add", "replan"]
    text: str = Field(default="", max_length=4_000)
    role: str = "explore"


class ResumeRequest(BaseModel):
    decisions: dict[str, bool] = Field(default_factory=dict)


class CompactRequest(BaseModel):
    keep_recent: int = Field(default=6, ge=1, le=50)


class SessionRequest(BaseModel):
    """新建/绑定一个会话。

    workspace 与 access_mode 在**服务端**绑定，之后不能由单次 run 请求改写。
    """

    thread_id: str = Field(default="", max_length=64)
    # RFC-0001：注册表开启时必须用 workspace_id；关闭时仍接受裸路径。
    workspace_id: str = Field(default="", max_length=36)
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
    # 注册表开启时绑定；关闭时为空，保持旧语义。
    workspace_id: str = ""
    access_mode: str = ACCESS_READ_ONLY
    model: str = ""
    provider: str = ""
    uploaded_bytes: int = 0
    # GUI 重命名；空 = 使用首条提示词生成的标题。
    title: str = ""
    created_at: float = field(default_factory=time.monotonic)
