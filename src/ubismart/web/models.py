"""A2 Web 的共享请求/会话模型。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field

from coding_agent.identity.access import ACCESS_READ_ONLY


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


class SessionImportRequest(BaseModel):
    """导入一份导出的会话 JSON；工作区/权限/模型仍走正常创建校验。"""

    title: str = Field(default="", max_length=120)
    messages: list[dict[str, Any]] = Field(default_factory=list)
    transcript: list[dict[str, Any]] = Field(default_factory=list)
    workspace_id: str = Field(default="", max_length=36)
    workspace: str = Field(default="", max_length=4096)
    access_mode: Literal["read_only", "write_ask", "write_auto"] = "read_only"
    model: str = Field(default="", max_length=200)
    provider: str = Field(default="", max_length=64)


class ClientErrorReport(BaseModel):
    """前端主动上报的错误摘要；不包含对话内容。"""

    kind: str = Field(default="error", max_length=32)
    message: str = Field(min_length=1, max_length=500)
    stack: str = Field(default="", max_length=4000)
    url: str = Field(default="", max_length=500)
    version: str = Field(default="", max_length=64)
    locale: str = Field(default="", max_length=16)
    at: str = Field(default="", max_length=64)


class ModelRequest(BaseModel):
    model: str = Field(default="", max_length=200)
    provider: str = Field(default="", max_length=64)


class RenameRequest(BaseModel):
    title: str = Field(default="", max_length=120)


class SessionMetaRequest(BaseModel):
    pinned: bool | None = None
    archived: bool | None = None
    tags: list[str] | None = None
    unread: bool | None = None


@dataclass
class SessionConfig:
    thread_id: str
    workspace: str
    # 注册表开启时绑定；关闭时为空，保持旧语义。
    workspace_id: str = ""
    workspace_kind: str = "sandbox"
    a2l_id: str = ""
    root_handle: str = ""
    access_mode: str = ACCESS_READ_ONLY
    model: str = ""
    provider: str = ""
    uploaded_bytes: int = 0
    # GUI 重命名；空 = 使用首条提示词生成的标题。
    title: str = ""
    # 会话目录展示状态；仅影响列表，不影响审计/checkpoint。
    pinned: bool = False
    archived: bool = False
    tags: list[str] = field(default_factory=list)
    unread: bool = False
    last_message: str = ""
    created_at: float = field(default_factory=time.monotonic)
