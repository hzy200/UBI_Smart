"""备份 A2 控制面数据库（SQLite 或 MySQL）。

    python scripts/backup_control_db.py --out D:/backups --keep 7

默认读取 AGENT_CONTROL_DB_URL；不备份 checkpoint 或工作区。控制面只含
provider Key 的 AES-GCM 密文；若配置了主密钥，备份后会自动检查备份目录
不得出现主密钥材料。
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
from coding_agent.control.credentials import (  # noqa: E402
    find_master_key_material,
    load_master_key,
)


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
    try:
        key = load_master_key(
            os.environ.get("AGENT_SECRET_MASTER_KEY", ""),
            os.environ.get("AGENT_SECRET_MASTER_KEY_FILE", ""),
        )
    except Exception as exc:  # noqa: BLE001 - 备份成功但检查配置非法
        print(
            json.dumps(
                {"ok": False, "error": f"主密钥配置非法：{exc}"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    if key:
        findings = find_master_key_material(
            Path(result.path).parent,
            key,
            key_file=os.environ.get("AGENT_SECRET_MASTER_KEY_FILE", ""),
        )
        if findings:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": "备份目录包含主密钥材料，拒绝完成",
                        "findings": findings,
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 1
    print(json.dumps({"ok": True, **asdict(result)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
