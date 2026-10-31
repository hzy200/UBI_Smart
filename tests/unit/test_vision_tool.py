"""image_inspect 与图片预处理。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ubismart.config import Settings
from ubismart.llm.multimodal import VisionError, prepare_image
from ubismart.tools.artifacts import unpack
from ubismart.tools.vision import build_vision_tools


class _FakeLLM:
    def invoke(self, messages):
        class _Response:
            content = "图中有一个红色方块。"

        return _Response()


def _settings() -> Settings:
    return Settings(_env_file=None)


def test_image_inspect_returns_structured_artifact(tmp_path: Path) -> None:
    from PIL import Image

    Image.new("RGB", (32, 32), (255, 0, 0)).save(tmp_path / "a.png")
    tool = build_vision_tools(_settings(), root=str(tmp_path), llm=_FakeLLM())[0]
    text, artifact = unpack(
        tool.invoke({"path": "a.png", "reason": "test", "question": "颜色"})
    )
    assert artifact and artifact["ok"] is True
    assert artifact["width"] == 32
    assert artifact["height"] == 32
    assert "红色方块" in text


def test_svg_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "a.svg").write_text("<svg/>", encoding="utf-8")
    tool = build_vision_tools(_settings(), root=str(tmp_path), llm=_FakeLLM())[0]
    text, artifact = unpack(tool.invoke({"path": "a.svg", "reason": "test"}))
    assert artifact and artifact["rejected"] is True
    assert "SVG" in text


def test_prepare_image_scales_down() -> None:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (4000, 2000), (0, 0, 255)).save(buffer, format="PNG")
    prepared = prepare_image(buffer.getvalue(), max_dimension=1000)
    assert max(prepared.width, prepared.height) <= 1000


def test_prepare_image_rejects_non_image() -> None:
    with pytest.raises(VisionError):
        prepare_image(b"not an image")


def test_prepare_image_rejects_decompression_bomb() -> None:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (32, 32), (0, 0, 0)).save(buffer, format="PNG")
    with pytest.raises(VisionError):
        prepare_image(buffer.getvalue(), max_pixels=100)
