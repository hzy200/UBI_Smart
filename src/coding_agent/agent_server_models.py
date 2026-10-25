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


class WorkspaceImportBody(BaseModel):
    """把宿主目录导入到 context.workspace（RFC-0001）。"""

    source_path: str = Field(min_length=1, max_length=4096)
    mode: Literal["copy"] = "copy"
    quota_bytes: int = Field(default=0, ge=0)
    max_files: int = Field(default=200_000, ge=1)
    max_bytes: int = Field(default=0, ge=0)


class WorkspaceRollbackBody(BaseModel):
    revision_id: str = Field(min_length=1, max_length=36)
    git_commit: str = Field(min_length=7, max_length=64)


class WorkspaceArchiveDeleteBody(BaseModel):
    archive_path: str = Field(min_length=1, max_length=4096)
