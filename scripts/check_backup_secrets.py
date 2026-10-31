"""检查备份目录中是否混入了主密钥。

    python scripts/check_backup_secrets.py --dir backups \
      --key-file /run/secrets/master.key

退出码：0 = 未发现；1 = 发现主密钥材料；2 = 参数/主密钥不可用。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ubismart.control.credentials import (  # noqa: E402
    CredentialError,
    find_master_key_material,
    load_master_key,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="检查备份目录是否含主密钥")
    parser.add_argument("--dir", required=True, help="备份目录")
    parser.add_argument(
        "--key",
        default=os.environ.get("AGENT_SECRET_MASTER_KEY", ""),
    )
    parser.add_argument(
        "--key-file",
        default=os.environ.get("AGENT_SECRET_MASTER_KEY_FILE", ""),
    )
    args = parser.parse_args()
    directory = Path(args.dir)
    if not directory.is_dir():
        print(
            json.dumps(
                {"ok": False, "error": f"目录不存在：{directory}"},
                ensure_ascii=False,
            )
        )
        return 2
    try:
        key = load_master_key(args.key, args.key_file)
    except CredentialError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2
    if not key:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": "未配置主密钥；无法执行检查",
                },
                ensure_ascii=False,
            )
        )
        return 2
    findings = find_master_key_material(
        directory, key, key_file=args.key_file
    )
    print(
        json.dumps(
            {
                "ok": not findings,
                "directory": str(directory),
                "findings": findings,
            },
            ensure_ascii=False,
        )
    )
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
