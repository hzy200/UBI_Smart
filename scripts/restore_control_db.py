"""从备份恢复 A2 控制面数据库。

    python scripts/restore_control_db.py --source D:/backups/control-x.sql

恢复前默认先备份当前库；用 --no-backup-current 跳过（目标库为空时）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.control.backup import restore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="恢复控制面数据库")
    parser.add_argument(
        "--url", default=os.environ.get("AGENT_CONTROL_DB_URL", "")
    )
    parser.add_argument("--source", required=True, help="备份文件")
    parser.add_argument("--out", default="", help="恢复前备份目录")
    parser.add_argument(
        "--no-backup-current",
        action="store_true",
        help="不备份当前库（目标为空时使用）",
    )
    args = parser.parse_args()
    if not str(args.url or "").strip():
        print("必须提供 --url 或设置 AGENT_CONTROL_DB_URL", file=sys.stderr)
        return 2
    try:
        path = restore(
            args.url,
            args.source,
            backup_current=not args.no_backup_current,
            target_dir=args.out or None,
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
    print(json.dumps({"ok": True, "source": path}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
