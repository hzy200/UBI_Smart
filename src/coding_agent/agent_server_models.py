"""A3 Agent Server HTTP 请求模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RunBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=200_000)
    thread_id: str = Field(min_length=1, max_length=64)


class ResumeBody(BaseModel):
    decisions: dict[str, bool] = Field(default_factory=dict)


class SteerBody(BaseModel):
    action: str = "cancel"
    text: str = ""
    role: str = "explore"


class RestoreBody(BaseModel):
    thread_id: str
    path: str = ""
    snapshot_id: str = ""
    prefix: str = ""
    dry_run: bool = False


class MemoryBody(BaseModel):
    action: Literal["remember", "forget", "clear", "pin", "priority"]
    text: str = ""
    index: int = -1
    scope: str = "project"
    pinned: bool = True
    priority: int = 0
    thread_id: str = ""


class DeleteUploadBody(BaseModel):
    path: str


class CompactBody(BaseModel):
    keep_recent: int = 6
