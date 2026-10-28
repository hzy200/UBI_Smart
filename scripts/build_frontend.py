"""生成用户端与管理端静态产物。

管理端使用 React，依赖本地 `node_modules`（CI 会先执行 `npm ci`）；
构建入口统一为 `node esbuild.mjs`，同时产出 app.js、admin/app.js 与
admin/app.css，避免两套构建参数漂移。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "src" / "coding_agent" / "web" / "frontend"


def main() -> int:
    node = shutil.which("node") or shutil.which("node.exe")
    if node is None:
        print("未找到 node；请安装 Node.js 18+")
        return 2
    if not (FRONTEND / "node_modules" / "esbuild").exists():
        print("未安装前端依赖；请先在 frontend 目录执行 npm ci")
        return 2
    if not (FRONTEND / "node_modules" / "react").exists():
        print("未安装 React 依赖；请先在 frontend 目录执行 npm ci")
        return 2
    result = subprocess.run(
        [node, "esbuild.mjs"], cwd=FRONTEND, check=False
    )
    return int(result.returncode)


if __name__ == "__main__":
    sys.exit(main())
