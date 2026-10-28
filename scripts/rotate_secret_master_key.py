"""轮换 provider Key 的 AES-GCM 主密钥。

    python scripts/rotate_secret_master_key.py \
      --old-key-file /run/secrets/old.key \
      --new-key-file /run/secrets/new.key --backup --dry-run

轮换前必须先备份数据库；轮换成功后立即更新部署侧
`AGENT_SECRET_MASTER_KEY`/`AGENT_SECRET_MASTER_KEY_VERSION`，旧密钥不再可用。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.control.backup import backup  # noqa: E402
from coding_agent.control.credentials import (  # noqa: E402
    CredentialError,
    CredentialStore,
    load_master_key,
)
from coding_agent.control.store import ControlStore  # noqa: E402


def _key(raw: str, path: str) -> bytes:
    return load_master_key(raw, path)


def main() -> int:
    parser = argparse.ArgumentParser(description="轮换 provider 主密钥")
    parser.add_argument(
        "--url", default=os.environ.get("AGENT_CONTROL_DB_URL", "")
    )
    parser.add_argument("--old-key", default="")
    parser.add_argument("--old-key-file", default="")
    parser.add_argument("--new-key", default="")
    parser.add_argument("--new-key-file", default="")
    parser.add_argument("--old-version", type=int, default=1)
    parser.add_argument("--new-version", type=int, default=0)
    parser.add_argument("--backup", action="store_true")
    parser.add_argument("--backup-dir", default="")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    url = str(args.url or "").strip()
    if not url:
        print("必须提供 --url 或设置 AGENT_CONTROL_DB_URL", file=sys.stderr)
        return 2
    try:
        old_key = _key(args.old_key, args.old_key_file)
        new_key = _key(args.new_key, args.new_key_file)
        if len(old_key) != 32 or len(new_key) != 32:
            raise CredentialError("新旧主密钥都必须是 32 字节")
        if old_key == new_key:
            raise CredentialError("新旧主密钥不能相同")
    except CredentialError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 2

    store = ControlStore(url, auto_create=False)
    try:
        credentials = CredentialStore(
            store.engine, old_key, key_version=args.old_version
        )
        count = credentials.count()
        if args.dry_run:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "dry_run": True,
                        "rows": count,
                        "new_version": args.new_version
                        or args.old_version + 1,
                    },
                    ensure_ascii=False,
                )
            )
            return 0
        if args.backup:
            backup(url, args.backup_dir or None)
        rotated = credentials.rotate(
            old_key,
            new_key,
            new_version=args.new_version or None,
        )
    except Exception as exc:  # noqa: BLE001 - CLI 边界
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}"},
                ensure_ascii=False,
            )
        )
        return 1
    finally:
        store.close()
    print(
        json.dumps(
            {
                "ok": True,
                "rows": rotated,
                "new_version": credentials.key_version,
                "note": (
                    "立即更新 AGENT_SECRET_MASTER_KEY 与 "
                    "AGENT_SECRET_MASTER_KEY_VERSION；旧密钥已无法解密"
                ),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
