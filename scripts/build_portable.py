"""构建 Windows Portable 目录（Phase 1）。

先安装：pip install -e ".[web,desktop,build]"
然后：python scripts/build_portable.py
产物：dist/UBISmart/UBISmart.exe（onedir）
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "ubismart" / "web" / "static"


def main() -> int:
    try:
        import PyInstaller.__main__
    except ImportError:
        print('未安装 PyInstaller。请运行：pip install -e ".[build]"')
        return 2

    sep = os.pathsep
    args = [
        str(ROOT / "src" / "ubismart" / "desktop" / "launcher.py"),
        "--name",
        "UBISmart",
        "--noconsole",
        "--clean",
        "--noconfirm",
        "--paths",
        str(ROOT / "src"),
        "--collect-all",
        "webview",
        "--add-data",
        f"{STATIC}{sep}ubismart/web/static",
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
    out = ROOT / "dist" / "UBISmart"
    rootfs = ROOT / "packaging" / "wsl" / "CodingAgent-rootfs.tar"
    if rootfs.is_file():
        target = out / "wsl"
        target.mkdir(parents=True, exist_ok=True)
        shutil.copy2(rootfs, target / "CodingAgent-rootfs.tar")
    else:
        print("警告：未找到 packaging/wsl/CodingAgent-rootfs.tar")

    # A2 控制面必须是随包可执行的 onedir；否则打包后的 launcher 只能回退到
    # `UBISmart.exe -m ubismart.servers.control_server`（GUI 入口不认识 -m）。
    print("构建并合并 A2 控制面")
    build = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "build_services.py"),
            "--target",
            "control",
        ],
        check=False,
    )
    if build.returncode != 0:
        print("A2 控制面构建失败")
        return build.returncode or 1
    control_src = ROOT / "dist" / "UBISmartControl"
    control_dst = out / "control"
    if control_dst.exists():
        shutil.rmtree(control_dst)
    shutil.copytree(control_src, control_dst)
    print(f"已合并控制面：{control_dst}")

    print(f"产物：{out / 'UBISmart.exe'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
