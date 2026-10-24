"""备份 A2 控制面数据库（SQLite 或 MySQL）。

    python scripts/backup_control_db.py --out D:/backups --keep 7

默认读取 AGENT_CONTROL_DB_URL；不备份 checkpoint、工作区或 API Key。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.control.backup import backup  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="备份控制面数据库")
    parser.add_argument(
        "--url", default=os.environ.get("AGENT_CONTROL_DB_URL", "")
    )
    parser.add_argument("--out", default="", help="备份目录")
    parser.add_argument("--keep", type=int, default=7, help="保留份数")
    args = parser.parse_args()
    if not str(args.url or "").strip():
        print("必须提供 --url 或设置 AGENT_CONTROL_DB_URL", file=sys.stderr)
        return 2
    try:
        result = backup(
            args.url,
            args.out or None,
            keep=max(0, args.keep),
        )
    except Exception as exc:  # noqa: BLE001 - CLI 要可读
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    print(json.dumps({"ok": True, **asdict(result)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
