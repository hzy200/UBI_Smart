"""PWA 外壳资产与 Service Worker 路由。"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from coding_agent import web

STATIC = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "coding_agent"
    / "web"
    / "static"
)


def test_manifest_and_shell_assets_exist() -> None:
    manifest = json.loads(
        (STATIC / "manifest.webmanifest").read_text(encoding="utf-8")
    )
    assert manifest["start_url"] == "/"
    assert manifest["scope"] == "/"
    sources = {icon["src"] for icon in manifest["icons"]}
    assert "/static/icon-192.png" in sources
    assert "/static/icon-512.png" in sources
    assert any(
        icon.get("purpose") == "maskable" for icon in manifest["icons"]
    )
    for name in (
        "icon-192.png",
        "icon-512.png",
        "apple-touch-icon.png",
        "favicon-32.png",
    ):
        assert (STATIC / name).is_file()
    index = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'rel="manifest"' in index
    assert 'name="theme-color"' in index


def test_service_worker_keeps_api_network_only() -> None:
    source = (STATIC / "sw.js").read_text(encoding="utf-8")
    assert "isNetworkOnly" in source
    assert "url.pathname.startsWith('/api/')" in source
    assert "self.addEventListener('fetch'" in source
    assert "caches.open(CACHE)" in source


def test_service_worker_is_served_from_root(settings) -> None:
    client = TestClient(web.create_app(settings))
    response = client.get("/sw.js")
    assert response.status_code == 200
    assert response.headers["service-worker-allowed"] == "/"
    assert "no-cache" in response.headers["cache-control"]
    manifest = client.get("/manifest.webmanifest")
    assert manifest.status_code == 200
    assert manifest.json()["short_name"] == "CodingAgent"
