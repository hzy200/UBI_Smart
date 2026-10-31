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


def build_vision_tools(
    settings: Settings,
    sandbox: Any | None = None,
    *,
    file_access: FileAccess | None = None,
    fs: Any | None = None,
    root: str | None = None,
    llm: Any | None = None,
) -> list[BaseTool]:
    if root is None:
        if sandbox is None:
            raise ValueError("build_vision_tools 需要 sandbox 或 root")
        root = resolve_workspace(settings, sandbox)
    if fs is None and sandbox is not None:
        fs = SandboxFs(sandbox, root)
    if file_access is not None:
        access: FileAccess = file_access
    elif fs is not None:
        access = SandboxFileAccess(fs, root)
    else:
        access = LocalFileAccess(root)

    def _display(target: str) -> str:
        try:
            return access.relative_to_root(target)
        except Exception:  # noqa: BLE001
            return str(target)

    def _error(path: str, message: str, *, rejected: bool = False) -> str:
        return pack(
            message,
            VisionArtifact(path=path, ok=False, rejected=rejected, error=message),
        )

    def _inspect(
        path: str, reason: str, question: str = ""
    ) -> str:
        suffix = Path(str(path or "")).suffix.lower()
        if suffix not in _IMAGE_SUFFIXES:
            return _error(
                path,
                "不支持的图片格式。支持 png/jpg/jpeg/webp/gif/bmp；"
                "SVG 不渲染也不作为图像输入。",
                rejected=True,
            )
        try:
            target = access.lexical(path)
        except Exception as exc:  # noqa: BLE001
            return _error(path, f"路径被拒绝：{exc}", rejected=True)
        display = _display(target)
        protected = protected_read_reason(display, settings.read_deny_globs)
        if protected:
            return _error(path, protected, rejected=True)
        try:
            info = access.stat(path)
        except Exception as exc:  # noqa: BLE001
            return _error(path, f"无法解析文件：{exc}", rejected=True)
        real_path = (
            str(info.get("real_path") or "")
            if isinstance(info, dict)
            else str(getattr(info, "real_path", "") or "")
        )
        if real_path:
            protected = protected_read_reason(real_path, settings.read_deny_globs)
            if protected:
                return _error(path, protected, rejected=True)
        is_file = (
            bool(info.get("is_file"))
            if isinstance(info, dict)
            else bool(getattr(info, "is_file", False))
        )
        if not is_file:
            return _error(path, "不是普通文件", rejected=True)

        max_bytes = int(getattr(settings, "image_max_bytes", 10 * 1024 * 1024))
        try:
            data = access.read_bytes(path, max_bytes=max_bytes)
            prepared = prepare_image(
                data,
                max_bytes=max_bytes,
                max_dimension=int(
                    getattr(settings, "image_max_dimension", 2048)
                ),
            )
        except VisionError as exc:
            return _error(path, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _error(path, f"读取图片失败：{type(exc).__name__}: {exc}")

        try:
            model = llm
            if model is None:
                from ubismart.llm.deepseek import build_llm

                model = build_llm(settings, streaming=False)
            description = invoke_vision(model, question or reason, prepared)
        except VisionError as exc:
            return _error(path, str(exc))
        except Exception as exc:  # noqa: BLE001 - 缺 key 等环境错误要可读
            return _error(
                path,
                f"图像理解不可用：{type(exc).__name__}: {exc}。"
                "请确认已配置可用的多模态模型。",
            )

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

    return [
        StructuredTool.from_function(
            func=_inspect,
            name=IMAGE_INSPECT,
            description=(
                "用多模态模型理解工作区内的图片（png/jpg/jpeg/webp/gif/bmp），"
                "可提取图中文字、表格、图表与界面元素。"
                "SVG 与远程 URL 不支持；只读，不修改文件。"
            ),
            args_schema=ImageInspectInput,
        )
    ]
