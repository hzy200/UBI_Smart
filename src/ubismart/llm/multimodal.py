"""多模态消息构造与图片预处理。

默认 LLM 视为多模态模型；这里只负责把图片变成 OpenAI 兼容的 content block。
端点实际不支持图片时，调用方会收到明确的 `VisionError`，不会静默退回纯文本。
"""

from __future__ import annotations

import base64
import io
import warnings
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

VISION_SYSTEM_PROMPT = """\
你是图像理解助手。只根据图片中可见的信息回答，不要臆造。
如果图中包含文字、表格、图表或界面元素，请按结构化方式列出；
不确定的内容明确标注“无法确认”。
"""


class VisionError(RuntimeError):
    """图片不可读，或当前模型/端点不支持图像输入。"""


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    mime: str
    width: int
    height: int


def prepare_image(
    data: bytes,
    *,
    max_bytes: int = 10 * 1024 * 1024,
    max_dimension: int = 2048,
    max_pixels: int = 40_000_000,
) -> PreparedImage:
    """校验、纠正方向、缩放并按需转码；不保留原始字节。"""
    if len(data) > max_bytes:
        raise VisionError(f"图片过大：{len(data)} 字节 > {max_bytes} 字节")
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise VisionError(
            "缺少图片处理依赖 Pillow。请安装：pip install 'ubismart[documents]'"
        ) from exc
    # 防止解压炸弹：超过 4000 万像素直接拒绝，而不是只发 warning。
    Image.MAX_IMAGE_PIXELS = max(1, int(max_pixels))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            image = Image.open(io.BytesIO(data))
            image.load()
            image = ImageOps.exif_transpose(image)
    except Exception as exc:  # noqa: BLE001 - Pillow 异常类型不稳定
        raise VisionError(f"图片无法识别或已损坏：{type(exc).__name__}: {exc}") from exc

    width, height = image.size
    if max(width, height) > max_dimension:
        image.thumbnail((max_dimension, max_dimension))
        width, height = image.size
    if image.mode in {"RGBA", "LA", "P"}:
        background = Image.new("RGB", image.size, (255, 255, 255))
        converted = image.convert("RGBA")
        background.paste(converted, mask=converted.split()[-1])
        image = background
    elif image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")

    # 照片保留 JPEG 以控制体积；带透明通道或线框图用 PNG。
    original_format = str(getattr(image, "format", "") or "").upper()
    use_jpeg = original_format in {"JPEG", "JPG", "WEBP"} and image.mode == "RGB"
    output = io.BytesIO()
    if use_jpeg:
        image.save(output, format="JPEG", quality=85, optimize=True)
        mime = "image/jpeg"
    else:
        image.save(output, format="PNG", optimize=True)
        mime = "image/png"
    return PreparedImage(
        data=output.getvalue(), mime=mime, width=width, height=height
    )


def image_block(mime: str, data: bytes) -> dict[str, Any]:
    encoded = base64.b64encode(data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }


def vision_messages(
    question: str, prepared: PreparedImage
) -> list[Any]:
    prompt = (
        question.strip()
        or "请描述这张图片的内容；若有文字、表格或图表，请提取关键信息。"
    )
    return [
        SystemMessage(content=VISION_SYSTEM_PROMPT),
        HumanMessage(
            content=[
                {"type": "text", "text": prompt},
                image_block(prepared.mime, prepared.data),
            ]
        ),
    ]


def invoke_vision(
    llm: Any,
    question: str,
    prepared: PreparedImage,
) -> str:
    """同步调用多模态模型；错误统一转成可操作的 `VisionError`。"""
    try:
        response = llm.invoke(vision_messages(question, prepared))
    except Exception as exc:  # noqa: BLE001 - provider 异常类型不稳定
        text = f"{type(exc).__name__}: {exc}"
        lowered = text.lower()
        if any(
            hint in lowered
            for hint in (
                "image",
                "vision",
                "multimodal",
                "content type",
                "invalid content",
                "400",
            )
        ):
            raise VisionError(
                "当前模型/端点不支持图像输入，或图片格式不被接受。"
                "请确认 DEEPSEEK_MODEL / AGENT_LLM_PROVIDERS 指向多模态模型。"
                f"原始错误：{text}"
            ) from exc
        raise VisionError(f"图像理解调用失败：{text}") from exc
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [
            str(block.get("text", ""))
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n".join(part for part in parts if part).strip()
    return str(content).strip()
