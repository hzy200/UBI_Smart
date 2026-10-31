"""审计日志修复：把无法解析的行隔离出去，保证 `verify_audit_files` 可重复执行。

历史审计文件里出现过被中断写入的片段（例如 `_ms":362,...`）。它们不是
合法记录，却会让 `verify_audit_files` 在解析阶段直接失败，后续链校验也
无从进行。

安全原则：

- **默认 dry-run**，只报告，不修改任何文件；
- `--write` 时把坏行原样追加到 `<file>.corrupt`，再原子重写原文件；
- 不删除坏行、不改写合法记录；
- 修复后重新跑 `verify_audit_files`；链仍然不完整时明确报错，交人工处理。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ubismart.audit import AuditRecord, verify_audit_files  # noqa: E402
from ubismart.config import Settings  # noqa: E402


def _bad_reason(line: bytes) -> str:
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        return f"utf-8: {exc}"
    try:
        AuditRecord.model_validate_json(text)
    except Exception as exc:  # noqa: BLE001 - 任何解析失败都算坏行
        return f"json: {type(exc).__name__}: {exc}"
    return ""


def repair_file(path: Path, *, write: bool) -> dict:
    raw = path.read_bytes()
    lines = raw.split(b"\n")
    good: list[bytes] = []
    bad: list[dict] = []
    for index, line in enumerate(lines, 1):
        if not line.strip():
            continue
        reason = _bad_reason(line)
        if reason:
            bad.append(
                {
                    "line": index,
                    "reason": reason,
                    "raw": line.decode("utf-8", "backslashreplace"),
                }
            )
        else:
            good.append(line)

    written = False
    if bad and write:
        quarantine = path.with_suffix(path.suffix + ".corrupt")
        with quarantine.open("a", encoding="utf-8") as handle:
            for item in bad:
                handle.write(
                    json.dumps(
                        {"file": path.name, **item},
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        data = b"\n".join(good)
        if data:
            data += b"\n"
        fd, temporary = tempfile.mkstemp(
            dir=path.parent, prefix=path.name + ".repair-"
        )
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        written = True

    return {
        "file": str(path),
        "good": len(good),
        "bad": bad,
        "written": written,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="审计日志修复：隔离无法解析的行（默认 dry-run）"
    )
    parser.add_argument(
        "--dir",
        default="",
        help="审计目录；默认取 Settings.resolved_audit_dir",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="实际重写文件；坏行会追加到 <file>.corrupt",
    )
    parser.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    args = parser.parse_args(argv)

    directory = (
        Path(args.dir) if args.dir else Settings().resolved_audit_dir
    )
    if not directory.is_dir():
        print(f"审计目录不存在：{directory}")
        return 2

    files = sorted(directory.glob("*.jsonl"))
    results = [repair_file(path, write=args.write) for path in files]
    verify_ok, verify_message = verify_audit_files(files)

    bad_count = sum(len(item["bad"]) for item in results)
    summary = {
        "dir": str(directory),
        "files": len(files),
        "bad_lines": bad_count,
        "written": args.write and bad_count > 0,
        "verify_ok": verify_ok,
        "verify_message": verify_message,
        "details": results,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        for item in results:
            if not item["bad"]:
                continue
            print(f"{item['file']}: {len(item['bad'])} 个坏行")
            for bad in item["bad"]:
                print(
                    f"  line {bad['line']}: {bad['reason']}\n"
                    f"    {bad['raw'][:160]}"
                )
        mode = "已写入" if summary["written"] else "dry-run（加 --write 才会修改）"
        print(
            f"\n目录：{directory}\n文件：{len(files)}，坏行：{bad_count}，{mode}"
        )
        print(f"verify_audit_files：{verify_message}")

    if not verify_ok:
        return 1
    if bad_count and not args.write:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
