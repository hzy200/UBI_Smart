"""主密钥轮换与备份隔离演练（不需要真实 MySQL）。

流程：临时 SQLite → 写入密文 → 备份 → 轮换主密钥 → 验证明文恢复 →
确认备份目录没有主密钥 → 故意放入密钥文件时检查必须失败。
"""

from __future__ import annotations

import base64
import json
import tempfile
from pathlib import Path

from coding_agent.control.backup import backup
from coding_agent.control.credentials import (
    CredentialError,
    CredentialStore,
    find_master_key_material,
)
from coding_agent.control.store import ControlStore

OLD = b"a" * 32
NEW = b"b" * 32


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        url = f"sqlite:///{root / 'control.db'}"
        store = ControlStore(url)
        try:
            credentials = CredentialStore(store.engine, OLD)
            credentials.set("default", "alice", "deepseek", "sk-drill-1234")
            backup_dir = root / "backups"
            backup(url, backup_dir, keep=1)
            before = find_master_key_material(backup_dir, OLD)
            if before:
                raise AssertionError(f"备份目录误含主密钥：{before}")

            rotated = credentials.rotate(OLD, NEW, new_version=2)
            if rotated != 1:
                raise AssertionError("轮换行数不正确")
            if credentials.get("default", "alice", "deepseek") != (
                "sk-drill-1234"
            ):
                raise AssertionError("轮换后无法解密密文")
            try:
                CredentialStore(store.engine, OLD).get(
                    "default", "alice", "deepseek"
                )
            except CredentialError:
                pass
            else:
                raise AssertionError("旧主密钥仍能解密，轮换未生效")

            planted = backup_dir / "master.key"
            planted.write_text(
                base64.b64encode(OLD).decode("ascii"), encoding="utf-8"
            )
            found = find_master_key_material(backup_dir, OLD)
            if str(planted) not in found:
                raise AssertionError("检查器未发现故意放入的主密钥")
            planted.unlink()
            after = find_master_key_material(backup_dir, NEW)
            if after:
                raise AssertionError(f"轮换后备份误含主密钥：{after}")
        finally:
            store.close()
    print(
        json.dumps(
            {
                "ok": True,
                "checks": [
                    "ciphertext_write",
                    "backup_without_master_key",
                    "rotate_old_to_new",
                    "old_key_rejected",
                    "planted_key_detected",
                ],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
