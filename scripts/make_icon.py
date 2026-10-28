"""生成桌面窗口与安装器使用的多尺寸图标。

造型与 `web/static/icon.svg` 保持一致：深色圆角底、蓝色折线、浅色下划线。
直接运行：python scripts/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BASE = Path(__file__).resolve().parents[1] / "src" / "coding_agent" / "web" / "static"
CANVAS = 1024
SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def render(size: int = CANVAS) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    pad = round(size * 0.02)
    draw.rounded_rectangle(
        (pad, pad, size - pad, size - pad),
        radius=round(size * 0.22),
        fill="#0f1115",
        outline="#2b3242",
        width=max(1, round(size * 0.012)),
    )
    stroke = round(size * 0.075)
    draw.line(
        [
            (round(size * 0.24), round(size * 0.32)),
            (round(size * 0.40), round(size * 0.50)),
            (round(size * 0.24), round(size * 0.68)),
        ],
        fill="#7aa2f7",
        width=stroke,
        joint="curve",
    )
    draw.line(
        [
            (round(size * 0.52), round(size * 0.68)),
            (round(size * 0.76), round(size * 0.68)),
        ],
        fill="#c0caf5",
        width=stroke,
    )
    return image


def main() -> None:
    target = BASE / "icon.ico"
    render().save(target, format="ICO", sizes=SIZES)
    print(f"wrote {target} ({target.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
