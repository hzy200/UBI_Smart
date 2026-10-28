"""校验前端发布包：清单、哈希、资源引用与版本契约。

用法：
    python scripts/check_frontend_release.py --dir dist/frontend/0.1.0

CI 在 build_frontend_release.py 之后运行本脚本；本地手工发布前也应先跑。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_frontend_release import HASH_LEN, read_web_version  # noqa: E402

HASH_NAME = re.compile(rf"\.[0-9a-f]{{{HASH_LEN}}}\.")
RELEASE_FILES = {"manifest.json", "SHA256SUMS"}
ASSET_REF = re.compile(r'(?:src|href)="([^"]+)"')
SW_SHELL = re.compile(r"const SHELL = \[(.*?)\];", re.DOTALL)
SINGLE_QUOTED = re.compile(r"'([^']+)'")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inside(root: Path, relative: str) -> Path | None:
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _resolve_asset(root: Path, ref: str, base_path: str) -> str | None:
    if base_path and ref.startswith(base_path + "/"):
        ref = ref[len(base_path):]
    if not ref.startswith("/static/"):
        return None
    return "static/" + ref[len("/static/"):]


def check(directory: Path) -> list[str]:
    errors: list[str] = []
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return [f"缺少 manifest.json：{directory}"]

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return [f"manifest.json 不是合法 JSON：{exc}"]

    if manifest.get("schemaVersion") != 1:
        errors.append("manifest.schemaVersion 必须是 1")

    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        errors.append("manifest.files 必须是非空数组")
        files = []

    listed: set[str] = set()
    for item in files:
        relative = str(item.get("path") or "")
        target = _inside(directory, relative)
        if target is None:
            errors.append(f"manifest 路径越界：{relative}")
            continue
        if not target.is_file():
            errors.append(f"manifest 登记的文件不存在：{relative}")
            continue
        if relative in listed:
            errors.append(f"manifest 重复登记：{relative}")
        listed.add(relative)
        if target.stat().st_size != item.get("size"):
            errors.append(f"文件大小不一致：{relative}")
        if _sha256(target) != item.get("sha256"):
            errors.append(f"文件哈希不一致：{relative}")

    for path in sorted(directory.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix()
        if relative in RELEASE_FILES:
            continue
        if relative not in listed:
            errors.append(f"文件未登记到 manifest：{relative}")

    entry = str(manifest.get("entry") or "")
    entry_path = _inside(directory, entry)
    if not entry or entry_path is None or not entry_path.is_file():
        errors.append(f"entry 不存在：{entry}")

    base_path = str(manifest.get("basePath") or "")
    if base_path and base_path.endswith("/api"):
        errors.append("basePath 不能以 /api 结尾")

    # index.html 的资源引用必须全部是已登记的 hash 资源。
    if entry_path is not None and entry_path.is_file():
        html = entry_path.read_text(encoding="utf-8")
        for ref in ASSET_REF.findall(html):
            relative = _resolve_asset(directory, ref, base_path)
            if relative is None:
                continue
            if relative not in listed:
                errors.append(f"index.html 引用了未登记资源：{ref}")
            if HASH_NAME.search(Path(relative).name) is None:
                errors.append(f"index.html 引用了未哈希资源：{ref}")

    # Service Worker：CACHE 与 releaseId 对齐，SHELL 指向真实文件。
    sw_path = _inside(directory, "sw.js")
    if sw_path is None or not sw_path.is_file():
        errors.append("缺少 sw.js")
    else:
        sw = sw_path.read_text(encoding="utf-8")
        release_id = str(manifest.get("releaseId") or "")
        if release_id and f"codingagent-shell-{release_id}" not in sw:
            errors.append("sw.js 的 CACHE 名与 releaseId 不一致")
        match = SW_SHELL.search(sw)
        if match is None:
            errors.append("sw.js 缺少 SHELL 清单")
        else:
            for ref in SINGLE_QUOTED.findall(match.group(1)):
                if ref == "/":
                    continue
                relative = _resolve_asset(directory, ref, base_path)
                if relative is None:
                    # manifest.webmanifest 等根路径资源。
                    target = _inside(directory, ref.lstrip("/"))
                else:
                    target = _inside(directory, relative)
                if target is None or not target.is_file():
                    errors.append(f"sw.js SHELL 指向不存在的资源：{ref}")

    # 运行时配置模板的版本必须与清单一致。
    config_path = _inside(directory, str(manifest.get("configFile") or ""))
    if config_path is None or not config_path.is_file():
        errors.append("缺少 config.example.json")
    else:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if config.get("apiVersion") != manifest.get("apiVersion"):
            errors.append("config.example.json 的 apiVersion 与 manifest 不一致")

    # 与源码契约对齐，防止发布包与后端版本漂移。
    source = read_web_version()
    for key in (
        "apiVersion",
        "minApiVersion",
        "minFrontendVersion",
        "capabilitiesVersion",
    ):
        if manifest.get(key) != source[key]:
            errors.append(
                f"manifest.{key}={manifest.get(key)!r} 与 web/version.py "
                f"的 {source[key]!r} 不一致"
            )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dir", required=True, help="发布包目录")
    args = parser.parse_args(argv)

    directory = Path(args.dir).resolve()
    errors = check(directory)
    if errors:
        for error in errors:
            print(f"FAIL {error}", file=sys.stderr)
        print(f"{len(errors)} 项校验失败：{directory}", file=sys.stderr)
        return 1
    print(f"OK {directory}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
