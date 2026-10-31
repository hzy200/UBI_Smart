"""更新检查（Phase 5）。

只做“检查 + 打开下载页”，不自动下载/安装：桌面版没有可信签名链时
自动替换 exe 风险过高。清单格式：

    {
      "version": "0.2.0",
      "download_url": "https://example.com/CodingAgent-Setup.exe",
      "sha256": "...",
      "notes": "修复 ..."
    }
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from urllib.parse import urlparse

_VERSION_RE = re.compile(r"^\d+(?:\.\d+)*$")


def manifest_url() -> str:
    return os.environ.get("AGENT_UPDATE_MANIFEST_URL", "").strip()


def parse_version(text: str) -> tuple[int, ...]:
    value = str(text or "").strip().lstrip("v")
    if not _VERSION_RE.match(value):
        return ()
    return tuple(int(part) for part in value.split("."))


def _newer(latest: str, current: str) -> bool:
    left = parse_version(latest)
    right = parse_version(current)
    if not left or not right:
        return False
    width = max(len(left), len(right))
    return left + (0,) * (width - len(left)) > right + (0,) * (
        width - len(right)
    )


def check_update(
    current: str, url: str, *, timeout: float = 5.0
) -> dict:
    parsed = urlparse(str(url or ""))
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return {
            "configured": True,
            "ok": False,
            "available": False,
            "error": "更新清单地址必须是 http(s)",
        }
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": "CodingAgent-updater"}
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read(256 * 1024).decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - 网络/JSON 错误都只是“检查失败”
        return {
            "configured": True,
            "ok": False,
            "available": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    if not isinstance(payload, dict):
        return {
            "configured": True,
            "ok": False,
            "available": False,
            "error": "更新清单不是 JSON 对象",
        }
    latest = str(payload.get("version") or "").strip()
    download = str(payload.get("download_url") or "").strip()
    if not parse_version(latest):
        return {
            "configured": True,
            "ok": False,
            "available": False,
            "error": "更新清单缺少合法 version",
        }
    target = urlparse(download)
    if target.scheme not in {"http", "https"} or not target.netloc:
        download = ""
    return {
        "configured": True,
        "ok": True,
        "current": current,
        "latest": latest,
        "available": _newer(latest, current),
        "download_url": download,
        "sha256": str(payload.get("sha256") or ""),
        "notes": str(payload.get("notes") or "")[:4000],
    }
