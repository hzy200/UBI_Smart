"""从统一源图生成各端图标。

源图随代码一起进版本控制（`web/frontend/assets/icon-source.png`），
本脚本只做缩放，不再用代码画一遍造型——否则 Web、桌面与安装器迟早会
各自漂移出不同的图标。

    web/static/favicon-32.png         浏览器标签
    web/static/icon-192.png           PWA 清单 / 管理端侧栏品牌位
    web/static/icon-512.png           PWA 清单（含 maskable）
    web/static/apple-touch-icon.png   iOS 添加到主屏
    web/static/icon.ico               Windows 窗口与安装器

直接运行：python scripts/make_icon.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (
    ROOT
    / "src"
    / "coding_agent"
    / "web"
    / "frontend"
    / "assets"
    / "icon-source.png"
)
STATIC = ROOT / "src" / "coding_agent" / "web" / "static"

# PNG 尺寸按各端实际需要给：iOS 要 180，PWA 清单要 192/512。
PNG_SIZES = {
    "favicon-32.png": 32,
    "icon-192.png": 192,
    "icon-512.png": 512,
    "apple-touch-icon.png": 180,
}
ICO_SIZES = [
    (16, 16),
    (24, 24),
    (32, 32),
    (48, 48),
    (64, 64),
    (128, 128),
    (256, 256),
]
ICO_CANVAS = 256


def _report(target: Path) -> None:
    print(f"wrote {target} ({target.stat().st_size} bytes)")


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit(f"缺少源图：{SOURCE}")
    source = Image.open(SOURCE).convert("RGBA")
    for name, size in PNG_SIZES.items():
        target = STATIC / name
        source.resize((size, size), Image.LANCZOS).save(target, optimize=True)
        _report(target)
    target = STATIC / "icon.ico"
    source.resize((ICO_CANVAS, ICO_CANVAS), Image.LANCZOS).save(
        target, format="ICO", sizes=ICO_SIZES
    )
    _report(target)


if __name__ == "__main__":
    main()
