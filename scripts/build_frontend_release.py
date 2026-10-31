"""生成可独立部署的前端发布包。

产物目录约定（`dist/frontend/<version>/`）：

    index.html                引用带内容哈希的静态资源
    sw.js                     CACHE 名包含 releaseId
    manifest.webmanifest
    config.example.json       部署侧复制为 config.json 后填写 apiBaseUrl
    manifest.json             版本、兼容范围、文件 sha256
    SHA256SUMS
    static/                   app.<hash>.js / style.<hash>.css / vendor/*

只写 `dist/`，不修改 Python 包内的 `web/static/`：桌面与 PyInstaller 仍使用
包内产物作为离线回退。设计见 docs/RELEASING.md 与 ADR-0020。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "src" / "ubismart" / "web" / "frontend"
STATIC = ROOT / "src" / "ubismart" / "web" / "static"
VERSION_TS = FRONTEND / "src" / "version.ts"
WEB_VERSION_PY = ROOT / "src" / "ubismart" / "web" / "version.py"

# 图标随源图一起换内容，所以要参与哈希，否则 CDN/浏览器会一直用旧图标。
HASH_EXTS = {".js", ".css", ".svg", ".png", ".woff", ".woff2"}
HASH_LEN = 12
RELEASE_FILES = ("manifest.json", "SHA256SUMS")
CACHE_PREFIX = re.compile(r"codingagent-shell-[^'\"]*")


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 固定 LF，避免 Windows 换行让内容哈希在不同平台漂移。
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_frontend_version() -> str:
    match = re.search(
        r"FRONTEND_VERSION\s*=\s*['\"]([^'\"]+)['\"]",
        _read_text(VERSION_TS),
    )
    if match is None:
        raise SystemExit(f"无法从 {VERSION_TS} 读取 FRONTEND_VERSION")
    return match.group(1)


def read_web_version() -> dict[str, object]:
    text = _read_text(WEB_VERSION_PY)

    def grab(name: str, default: str | None = None) -> str:
        match = re.search(
            rf"^{name}\s*=\s*['\"]([^'\"]+)['\"]", text, re.MULTILINE
        )
        if match is not None:
            return match.group(1)
        if default is not None:
            return default
        raise SystemExit(f"无法从 {WEB_VERSION_PY} 读取 {name}")

    return {
        "apiVersion": grab("WEB_API_VERSION"),
        # 阶段 3 之前没有 MIN_API_VERSION；缺省视为 v0 兼容窗口。
        "minApiVersion": grab("MIN_API_VERSION", "v0"),
        "minFrontendVersion": grab("MIN_FRONTEND_VERSION"),
        "capabilitiesVersion": int(grab("CAPABILITIES_VERSION", "1")),
    }


def git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _content_hash(path: Path) -> str:
    return _sha256(path)[:HASH_LEN]


def _hash_name(name: str, digest: str) -> str:
    path = Path(name)
    return f"{path.stem}.{digest}{path.suffix}"


def _collect_files(root: Path) -> list[Path]:
    return sorted(
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.relative_to(root).as_posix() not in RELEASE_FILES
    )


def _copy_and_hash(static_dst: Path) -> dict[str, str]:
    """复制静态资源并重命名带哈希；返回旧相对路径 -> 新相对路径。"""
    mapping: dict[str, str] = {}
    for source in sorted(STATIC.rglob("*")):
        if not source.is_file():
            continue
        relative = source.relative_to(STATIC)
        # index/sw/manifest 单独放到发布目录根；README 只是来源说明。
        if relative.as_posix() in {"index.html", "sw.js", "manifest.webmanifest"}:
            continue
        if relative.suffix.lower() == ".md":
            continue
        target = static_dst / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        if target.suffix.lower() in HASH_EXTS:
            digest = _content_hash(target)
            hashed = target.with_name(_hash_name(target.name, digest))
            target.replace(hashed)
            mapping[relative.as_posix()] = hashed.relative_to(static_dst).as_posix()
        else:
            mapping[relative.as_posix()] = relative.as_posix()
    return mapping


def _rewrite_static_refs(text: str, mapping: dict[str, str]) -> str:
    # 长名字优先，避免 `alpine.min.js` 抢在 `alpine-csp.min.js` 前匹配。
    for old in sorted(mapping, key=len, reverse=True):
        new = mapping[old]
        text = text.replace(f"/static/{old}", f"/static/{new}")
    return text


def _rewrite_sw(text: str, mapping: dict[str, str], release_id: str) -> str:
    text = _rewrite_static_refs(text, mapping)
    # 按前缀替换而不是写死 `-v1`：换图标/换壳时必然会手动抬版本号，
    # 写死会让发布包的 CACHE 名和 releaseId 悄悄不再对齐。
    text = CACHE_PREFIX.sub(f"codingagent-shell-{release_id}", text)
    # 发布包首页在根目录，manifest 也在根目录；static 下不再有 index.html。
    text = text.replace("  '/',\n  '/static/index.html',\n", "  '/',\n")
    text = text.replace("  '/static/manifest.webmanifest',\n", "")
    return text


def _rewrite_manifest(text: str, mapping: dict[str, str], base_path: str) -> str:
    text = _rewrite_static_refs(text, mapping)
    if base_path:
        data = json.loads(text)
        data["start_url"] = f"{base_path}/"
        data["scope"] = f"{base_path}/"
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    return text


def _bundle_hash(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in _collect_files(directory):
        relative = path.relative_to(directory).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(_sha256(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()[:HASH_LEN]


def _default_built_at() -> str:
    # 允许通过 SOURCE_DATE_EPOCH 固定时间，便于构建可复现。
    import os

    epoch = os.environ.get("SOURCE_DATE_EPOCH", "").strip()
    if epoch.isdigit():
        moment = datetime.fromtimestamp(int(epoch), tz=UTC)
    else:
        moment = datetime.now(UTC)
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def build_release(
    out_root: Path,
    *,
    version: str,
    base_path: str = "",
    built_at: str = "",
    skip_build: bool = False,
) -> Path:
    base_path = base_path.rstrip("/")
    if base_path and not base_path.startswith("/"):
        raise SystemExit("--base-path 必须以 / 开头")
    if base_path.endswith("/api"):
        raise SystemExit("--base-path 不能以 /api 结尾")

    if not skip_build:
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_frontend.py")],
            cwd=ROOT,
            check=False,
        )
        if result.returncode != 0:
            raise SystemExit(f"前端构建失败：{result.returncode}")

    release_dir = out_root / version
    if release_dir.exists():
        shutil.rmtree(release_dir)
    static_dst = release_dir / "static"
    static_dst.mkdir(parents=True, exist_ok=True)

    mapping = _copy_and_hash(static_dst)
    index_html = _rewrite_static_refs(
        _read_text(STATIC / "index.html"), mapping
    )
    if base_path:
        index_html = index_html.replace("/static/", f"{base_path}/static/")
    _write_text(release_dir / "index.html", index_html)

    # 管理端是独立静态入口：发布到根目录 admin/，资源仍走 /static/admin/*。
    admin_source = STATIC / "admin" / "index.html"
    if admin_source.is_file():
        admin_html = _rewrite_static_refs(
            _read_text(admin_source), mapping
        )
        if base_path:
            admin_html = admin_html.replace(
                "/static/", f"{base_path}/static/"
            )
        _write_text(release_dir / "admin" / "index.html", admin_html)

    manifest_web = _rewrite_manifest(
        _read_text(STATIC / "manifest.webmanifest"), mapping, base_path
    )
    _write_text(release_dir / "manifest.webmanifest", manifest_web)

    bundle_hash = _bundle_hash(release_dir)
    commit = git_commit()
    release_id = f"{version}+{commit or 'local'}.{bundle_hash}"

    sw_source = _read_text(STATIC / "sw.js")
    if base_path:
        sw_source = sw_source.replace("/static/", f"{base_path}/static/")
    _write_text(
        release_dir / "sw.js",
        _rewrite_sw(sw_source, mapping, release_id),
    )

    web = read_web_version()
    config = {
        "schemaVersion": 1,
        "apiBaseUrl": "",
        "apiVersion": web["apiVersion"],
        "authMode": "bearer",
        "auth": {
            "loginUrl": "/auth/login",
            "refreshUrl": "/auth/refresh",
            "logoutUrl": "/auth/logout",
            "csrfHeader": "X-CSRF-Token",
        },
        "cost": {
            "currency": "CNY",
            "usdToCny": 7.2,
        },
        "sse": {
            "heartbeatMs": 15000,
            "idleTimeoutMs": 45000,
            "reconnectBaseMs": 500,
            "reconnectMaxMs": 30000,
            "reconnectMaxAttempts": 3,
        },
        "requestIdHeader": "X-Request-ID",
    }
    _write_text(
        release_dir / "config.example.json",
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
    )

    files = []
    for path in _collect_files(release_dir):
        relative = path.relative_to(release_dir).as_posix()
        files.append(
            {
                "path": relative,
                "sha256": _sha256(path),
                "size": path.stat().st_size,
            }
        )

    manifest = {
        "schemaVersion": 1,
        "name": "ubismart-frontend",
        "version": version,
        "releaseId": release_id,
        "gitCommit": commit,
        "builtAt": built_at or _default_built_at(),
        "basePath": base_path,
        "apiVersion": web["apiVersion"],
        "minApiVersion": web["minApiVersion"],
        "minFrontendVersion": web["minFrontendVersion"],
        "capabilitiesVersion": web["capabilitiesVersion"],
        "entry": "index.html",
        "configFile": "config.example.json",
        "files": files,
    }
    _write_text(
        release_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
    )
    sums = "".join(f"{item['sha256']}  {item['path']}\n" for item in files)
    _write_text(release_dir / "SHA256SUMS", sums)
    return release_dir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(ROOT / "dist" / "frontend"),
        help="发布根目录，默认 dist/frontend",
    )
    parser.add_argument("--version", default="", help="默认读取 version.ts")
    parser.add_argument(
        "--base-path",
        default="",
        help="子路径部署前缀，如 /app（默认根部署）",
    )
    parser.add_argument(
        "--built-at",
        default="",
        help="ISO8601 时间；默认当前 UTC，可用 SOURCE_DATE_EPOCH 固定",
    )
    parser.add_argument(
        "--skip-build",
        action="store_true",
        help="跳过 esbuild/build-ui，直接打包现有 static/",
    )
    args = parser.parse_args(argv)

    version = args.version or read_frontend_version()
    release_dir = build_release(
        Path(args.out).resolve(),
        version=version,
        base_path=args.base_path,
        built_at=args.built_at,
        skip_build=args.skip_build,
    )
    print(f"built {release_dir}")
    print(f"releaseId {json.loads(_read_text(release_dir / 'manifest.json'))['releaseId']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
