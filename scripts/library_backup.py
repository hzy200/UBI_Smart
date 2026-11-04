"""资料库备份/恢复/迁移。

备份的是 WSL 内一个用户的 `<library_root>` 目录；归档写到宿主路径。
恢复默认拒绝覆盖已有 catalog，必须显式 `--force`。

示例：
    python scripts/library_backup.py --root /home/u/agent-library/alice \
        --archive D:/backups/alice-library.zip
    python scripts/library_backup.py --root /home/u/agent-library/alice \
        --archive D:/backups/alice-library.zip --restore
    python scripts/library_backup.py --root /home/u/agent-library/alice --migrate
"""

from __future__ import annotations

import argparse
import json
import posixpath
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ubismart.config import Settings  # noqa: E402
from ubismart.library.store import LibraryStore  # noqa: E402
from ubismart.sandbox.fs import SandboxFs  # noqa: E402
from ubismart.sandbox.wsl_exec import WslSandbox  # noqa: E402

MAX_FILE_BYTES = 1_500_000_000


def _safe_name(name: str) -> str:
    rel = str(name or "").replace("\\", "/").strip()
    normalized = posixpath.normpath(rel)
    if (
        not normalized
        or normalized in {".", ".."}
        or normalized.startswith("../")
        or normalized.startswith("/")
        or "\x00" in normalized
    ):
        raise ValueError(f"归档包含非法路径：{name!r}")
    return normalized


def backup(fs: SandboxFs, root: str, archive: Path) -> dict:
    entries = fs.list_dir(root, max_depth=20, limit=200_000)
    files = [
        (kind, rel)
        for kind, rel in entries
        if kind == "f"
    ]
    archive.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for _, rel in files:
            data = fs.read_bytes(
                posixpath.join(root, rel), max_bytes=MAX_FILE_BYTES
            )
            bundle.writestr(rel, data)
            total += len(data)
    return {"files": len(files), "bytes": total, "archive": str(archive)}


def restore(fs: SandboxFs, root: str, archive: Path, *, force: bool) -> dict:
    fs.mkdir(root)
    if not force:
        info = fs.stat(posixpath.join(root, "catalog.json"))
        if info.exists:
            raise SystemExit("目标目录已有 catalog.json；如需覆盖请加 --force")
    restored = 0
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            if info.is_dir():
                continue
            rel = _safe_name(info.filename)
            fs.write_bytes(
                posixpath.join(root, rel), bundle.read(info)
            )
            restored += 1
    return {"files": restored, "archive": str(archive)}


def main() -> int:
    parser = argparse.ArgumentParser(description="资料库备份/恢复/迁移")
    parser.add_argument("--root", required=True, help="WSL 内 library_root")
    parser.add_argument("--archive", help="宿主侧 zip 归档路径")
    parser.add_argument("--restore", action="store_true", help="从归档恢复")
    parser.add_argument("--force", action="store_true", help="恢复时覆盖已有数据")
    parser.add_argument("--migrate", action="store_true", help="迁移 catalog")
    args = parser.parse_args()

    settings = Settings()
    sandbox = WslSandbox(settings)
    root = str(args.root).strip().rstrip("/")
    sandbox.ensure_directory(root)
    fs = SandboxFs(sandbox, root)
    if args.migrate:
        result = LibraryStore(fs, root).migrate()
        print(json.dumps(result, ensure_ascii=False))
        return 0
    if not args.archive:
        parser.error("--archive 在备份/恢复时必填")
    archive = Path(args.archive)
    if args.restore:
        print(json.dumps(restore(fs, root, archive, force=args.force), ensure_ascii=False))
    else:
        print(json.dumps(backup(fs, root, archive), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
