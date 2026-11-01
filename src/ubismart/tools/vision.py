"""`image_inspect`：用默认多模态 LLM 理解工作区内的图片。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from ubismart.config import Settings
from ubismart.execution.file_access import (
    FileAccess,
    LocalFileAccess,
    SandboxFileAccess,
)
from ubismart.llm.multimodal import VisionError, invoke_vision, prepare_image
from ubismart.sandbox.fs import SandboxFs
from ubismart.sandbox.wsl_exec import resolve_workspace
from ubismart.security.read_policy import protected_read_reason
from ubismart.tools.artifacts import VisionArtifact, pack

IMAGE_INSPECT = "image_inspect"
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


class ImageInspectInput(BaseModel):
    path: str = Field(description="工作区内图片的相对路径")
    reason: str = Field(description="理解图片的意图，一句话说明")
    question: str = Field(
        default="",
        description="希望模型重点回答的问题；留空则做通用描述与信息提取",
    )


def _resolve_root(
    settings: Settings, sandbox: Any | None, root: str | None
) -> str:
    if root is not None:
        return root
    if sandbox is None:
        raise ValueError("build_vision_tools 需要 sandbox 或 root")
    return resolve_workspace(settings, sandbox)


def _resolve_fs(sandbox: Any | None, root: str, fs: Any | None) -> Any | None:
    if fs is None and sandbox is not None:
        return SandboxFs(sandbox, root)
    return fs


def _resolve_access(
    root: str, fs: Any | None, file_access: FileAccess | None
) -> FileAccess:
    if file_access is not None:
        return file_access
    if fs is not None:
        return SandboxFileAccess(fs, root)
    return LocalFileAccess(root)


def _real_path(info: Any) -> str:
    if isinstance(info, dict):
        return str(info.get("real_path") or "")
    return str(getattr(info, "real_path", "") or "")


def _is_file_info(info: Any) -> bool:
    if isinstance(info, dict):
        return bool(info.get("is_file"))
    return bool(getattr(info, "is_file", False))


class _VisionContext:
    """封装 image_inspect 的路径校验、读取与模型调用。"""

    def __init__(self, settings: Settings, access: FileAccess, llm: Any | None):
        self.settings = settings
        self.access = access
        self.llm = llm

    def display(self, target: str) -> str:
        try:
            return self.access.relative_to_root(target)
        except Exception:  # noqa: BLE001
            return str(target)

    def error(self, path: str, message: str, *, rejected: bool = False) -> str:
        return pack(
            message,
            VisionArtifact(path=path, ok=False, rejected=rejected, error=message),
        )

    def resolve(self, path: str) -> tuple[str, str | None]:
        suffix = Path(str(path or "")).suffix.lower()
        if suffix not in _IMAGE_SUFFIXES:
            return "", self.error(
                path,
                "不支持的图片格式。支持 png/jpg/jpeg/webp/gif/bmp；"
                "SVG 不渲染也不作为图像输入。",
                rejected=True,
            )
        try:
            target = self.access.lexical(path)
        except Exception as exc:  # noqa: BLE001
            return "", self.error(path, f"路径被拒绝：{exc}", rejected=True)
        display = self.display(target)
        protected = protected_read_reason(
            display, self.settings.read_deny_globs
        )
        if protected:
            return "", self.error(path, protected, rejected=True)
        try:
            info = self.access.stat(path)
        except Exception as exc:  # noqa: BLE001
            return "", self.error(path, f"无法解析文件：{exc}", rejected=True)
        real_path = _real_path(info)
        if real_path:
            protected = protected_read_reason(
                real_path, self.settings.read_deny_globs
            )
            if protected:
                return "", self.error(path, protected, rejected=True)
        if not _is_file_info(info):
            return "", self.error(path, "不是普通文件", rejected=True)
        return display, None

    def prepare(self, path: str) -> tuple[Any | None, str | None]:
        max_bytes = int(
            getattr(self.settings, "image_max_bytes", 10 * 1024 * 1024)
        )
        try:
            data = self.access.read_bytes(path, max_bytes=max_bytes)
            prepared = prepare_image(
                data,
                max_bytes=max_bytes,
                max_dimension=int(
                    getattr(self.settings, "image_max_dimension", 2048)
                ),
            )
        except VisionError as exc:
            return None, self.error(path, str(exc))
        except Exception as exc:  # noqa: BLE001
            return None, self.error(
                path, f"读取图片失败：{type(exc).__name__}: {exc}"
            )
        return prepared, None

    def describe(
        self, path: str, reason: str, question: str, prepared: Any
    ) -> tuple[str, str | None]:
        try:
            model = self.llm
            if model is None:
                from ubismart.llm.deepseek import build_llm

                model = build_llm(self.settings, streaming=False)
            description = invoke_vision(model, question or reason, prepared)
        except VisionError as exc:
            return "", self.error(path, str(exc))
        except Exception as exc:  # noqa: BLE001 - 缺 key 等环境错误要可读
            return "", self.error(
                path,
                f"图像理解不可用：{type(exc).__name__}: {exc}。"
                "请确认已配置可用的多模态模型。",
            )
        return description, None

    def success(
        self, display: str, reason: str, description: str, prepared: Any
    ) -> str:
        artifact = VisionArtifact(
            path=display,
            ok=True,
            mime=prepared.mime,
            size=len(prepared.data),
            width=prepared.width,
            height=prepared.height,
            description=description,
            findings=[description],
        )
        header = (
            f"图片 {display}（{prepared.mime}，{prepared.width}x{prepared.height}）\n"
            f"{reason}"
        )
        return pack(f"{header}\n\n{description}", artifact)

    def inspect(self, path: str, reason: str, question: str = "") -> str:
        display, error = self.resolve(path)
        if error is not None:
            return error
        prepared, error = self.prepare(path)
        if error is not None:
            return error
        description, error = self.describe(path, reason, question, prepared)
        if error is not None:
            return error
        return self.success(display, reason, description, prepared)


def build_vision_tools(
    settings: Settings,
    sandbox: Any | None = None,
    *,
    file_access: FileAccess | None = None,
    fs: Any | None = None,
    root: str | None = None,
    llm: Any | None = None,
) -> list[BaseTool]:
    resolved_root = _resolve_root(settings, sandbox, root)
    resolved_fs = _resolve_fs(sandbox, resolved_root, fs)
    access = _resolve_access(resolved_root, resolved_fs, file_access)
    context = _VisionContext(settings, access, llm)
    return [
        StructuredTool.from_function(
            func=context.inspect,
            name=IMAGE_INSPECT,
            description=(
                "用多模态模型理解工作区内的图片（png/jpg/jpeg/webp/gif/bmp），"
                "可提取图中文字、表格、图表与界面元素。"
                "SVG 与远程 URL 不支持；只读，不修改文件。"
            ),
            args_schema=ImageInspectInput,
        )
    ]
