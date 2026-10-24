"""构建 Windows Portable 目录（Phase 1）。

先安装：pip install -e ".[web,desktop,build]"
然后：python scripts/build_portable.py
产物：dist/CodingAgent/CodingAgent.exe（onedir）
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "coding_agent" / "web" / "static"


def main() -> int:
    try:
        import PyInstaller.__main__
    except ImportError:
        print('未安装 PyInstaller。请运行：pip install -e ".[build]"')
        return 2

    sep = os.pathsep
    args = [
        str(ROOT / "src" / "coding_agent" / "desktop" / "launcher.py"),
        "--name",
        "CodingAgent",
        "--noconsole",
        "--clean",
        "--noconfirm",
        "--paths",
        str(ROOT / "src"),
        "--collect-all",
        "webview",
        "--add-data",
        f"{STATIC}{sep}coding_agent/web/static",
        "--hidden-import",
        "uvicorn.logging",
        "--hidden-import",
        "uvicorn.loops.auto",
        "--hidden-import",
        "uvicorn.protocols.http.auto",
        "--hidden-import",
        "uvicorn.protocols.websockets.auto",
        "--hidden-import",
        "uvicorn.lifespan.on",
    ]
    # 可选 fastembed 会通过 PyInstaller 的静态扫描拖入 torch/onnx/cv2 等
    # 数 GB 依赖；桌面版默认不启用向量记忆，显式排除。
    for module in (
        "fastembed",
        "onnxruntime",
        "torch",
        "torchvision",
        "torchaudio",
        "transformers",
        "cv2",
        "pandas",
        "scipy",
        "sklearn",
        "matplotlib",
        "pyarrow",
    ):
        args += ["--exclude-module", module]
    print("PyInstaller", " ".join(args))
    try:
        PyInstaller.__main__.run(args)
    except SystemExit as exc:
        # PyInstaller 失败时可能把错误文本放进 SystemExit.code。
        if isinstance(exc.code, int):
            return exc.code
        if exc.code:
            print(exc.code)
        return 1
    out = ROOT / "dist" / "CodingAgent"
    rootfs = ROOT / "packaging" / "wsl" / "CodingAgent-rootfs.tar"
    if rootfs.is_file():
        target = out / "wsl"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rootfs, target / "CodingAgent-rootfs.tar")
    else:
        print("警告：未找到 packaging/wsl/CodingAgent-rootfs.tar")
    print(f"产物：{out / 'CodingAgent.exe'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
