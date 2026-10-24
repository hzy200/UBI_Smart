"""构建 A2/A3 原生可执行目录（Phase B 打包）。

    python scripts/build_services.py --target agent
    python scripts/build_services.py --target control

产物：
    dist/CodingAgentAgent/CodingAgentAgent(.exe)
    dist/CodingAgentControl/CodingAgentControl(.exe)
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "coding_agent" / "web" / "static"
CONTROL_DIR = ROOT / "src" / "coding_agent" / "control"
MIGRATIONS = CONTROL_DIR / "migrations"
ALEMBIC_INI = CONTROL_DIR / "alembic.ini"

TARGETS = {
    "agent": (
        ROOT / "src" / "coding_agent" / "agent_server.py",
        "CodingAgentAgent",
    ),
    "control": (
        ROOT / "src" / "coding_agent" / "control_server.py",
        "CodingAgentControl",
    ),
}

EXCLUDES = (
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
    "tkinter",
    "datasets",
)


def build(target: str) -> int:
    try:
        import PyInstaller.__main__
    except ImportError:
        print('未安装 PyInstaller。请运行：pip install -e ".[build]"')
        return 2
    entry, name = TARGETS[target]
    sep = os.pathsep
    args = [
        str(entry),
        "--name",
        name,
        "--clean",
        "--noconfirm",
        "--console",
        "--paths",
        str(ROOT / "src"),
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
    if target == "control":
        # 控制面启动时可能执行 Alembic 迁移，迁移脚本必须随包分发。
        args += [
            "--add-data",
            f"{MIGRATIONS}{sep}coding_agent/control/migrations",
            "--add-data",
            f"{ALEMBIC_INI}{sep}coding_agent/control",
            "--hidden-import",
            "alembic",
        ]
    for module in EXCLUDES:
        args += ["--exclude-module", module]
    print("PyInstaller", " ".join(args))
    try:
        PyInstaller.__main__.run(args)
    except SystemExit as exc:
        if isinstance(exc.code, int):
            return exc.code
        if exc.code:
            print(exc.code)
        return 1
    out = ROOT / "dist" / name / f"{name}.exe"
    if os.name != "nt":
        out = ROOT / "dist" / name / name
    print(f"产物：{out}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--target",
        choices=sorted(TARGETS),
        default="agent",
    )
    args = parser.parse_args()
    return build(args.target)


if __name__ == "__main__":
    sys.exit(main())
