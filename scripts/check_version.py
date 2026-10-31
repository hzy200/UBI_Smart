"""校验版本号来源一致：Python 包与前端自己的两份版本。

前后端**独立升版**（见 docs/RELEASING.md），因此不要求两者相等：
- 后端：`pyproject.toml` 通过 hatch 动态读取 `ubismart.__version__`；
- 前端：`package.json` 与 `version.ts` 必须一致。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_TS = ROOT / "src" / "ubismart" / "web" / "frontend" / "src" / "version.ts"
PACKAGE_JSON = ROOT / "src" / "ubismart" / "web" / "frontend" / "package.json"


def _frontend_version() -> str:
    text = VERSION_TS.read_text(encoding="utf-8")
    match = re.search(
        r"FRONTEND_VERSION\s*=\s*['\"]([^'\"]+)['\"]", text
    )
    if match is None:
        raise SystemExit(f"{VERSION_TS} 缺少 FRONTEND_VERSION")
    return match.group(1)


def main() -> int:
    sys.path.insert(0, str(ROOT / "src"))
    from ubismart import __version__

    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    frontend = {
        "frontend/package.json": str(package.get("version") or ""),
        "frontend/src/version.ts": _frontend_version(),
    }
    if "" in frontend.values() or len(set(frontend.values())) != 1:
        print(
            json.dumps(
                {
                    "ok": False,
                    "backend": __version__,
                    "frontend": frontend,
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "backend": __version__,
                "frontend": next(iter(frontend.values())),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
