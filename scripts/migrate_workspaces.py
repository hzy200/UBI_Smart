"""RFC-0001：把旧 control_sessions.workspace 回填为工作区记录。

默认 **dry-run**，只输出统计，不写库。确认真实影响后再加 `--apply`。

    python scripts/migrate_workspaces.py --url "sqlite:///control.db"
    python scripts/migrate_workspaces.py --url "mysql+pymysql://..." --apply

前置：数据库已执行 `control_server --migrate`（至少到 0002_workspaces）。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from ubismart.control.migrate import has_table  # noqa: E402
from ubismart.control.workspaces import (  # noqa: E402
    WorkspacePolicy,
    WorkspaceStore,
)


def _engine(url: str):
    return create_engine(url, future=True, poolclass=NullPool)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="回填旧 control_sessions.workspace（默认 dry-run）"
    )
    parser.add_argument(
        "--url",
        default=os.environ.get("AGENT_CONTROL_DB_URL", ""),
        help="控制面数据库 URL；默认读 AGENT_CONTROL_DB_URL",
    )
    parser.add_argument("--tenant", default="", help="只处理指定租户")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="真正写库；不加时只统计",
    )
    args = parser.parse_args(argv)

    url = str(args.url or "").strip()
    if not url:
        print(
            json.dumps(
                {"ok": False, "error": "缺少 --url 或 AGENT_CONTROL_DB_URL"},
                ensure_ascii=False,
            )
        )
        return 2
    if not has_table(url, "control_sessions"):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "control_sessions 不存在；请先执行 control_server --migrate",
                },
                ensure_ascii=False,
            )
        )
        return 2
    if not has_table(url, "workspaces"):
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "workspaces 不存在；请先迁移到 0002_workspaces",
                },
                ensure_ascii=False,
            )
        )
        return 2

    engine = _engine(url)
    try:
        store = WorkspaceStore(engine, WorkspacePolicy())
        report = store.migrate_legacy_sessions(
            tenant=args.tenant or None,
            dry_run=not args.apply,
        )
    except Exception as exc:  # noqa: BLE001 - 脚本边界
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1
    finally:
        engine.dispose()

    payload = {"ok": True, "url": url, **report}
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
