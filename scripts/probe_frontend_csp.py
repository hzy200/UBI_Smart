"""Full pywebview smoke test for the CSP build with stubbed API."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

import webview

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "coding_agent" / "web" / "static"

CONFIG = {
    "schemaVersion": 1,
    "apiBaseUrl": "",
    "apiVersion": "v0",
    "authMode": "desktop-token",
    "auth": {
        "loginUrl": "",
        "refreshUrl": "",
        "logoutUrl": "",
        "csrfHeader": "X-CSRF-Token",
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

STUBS = {
    "/config.json": CONFIG,
    "/healthz": {
        "api_version": "v0",
        "min_frontend_version": "0.1.0",
        "version": "0.1.0",
        "capabilities_version": 1,
    },
    "/api/health": {
        "reachable": True,
        "model": "stub-model",
        "user": "local",
        "workspace": "/tmp/ws",
    },
    "/api/capabilities": {
        "read_only": True,
        "server_write_enabled": False,
        "multi_user": False,
        "desktop_mode": True,
        "workspace_registry": False,
        "host_mode": False,
        "workspace_upload_max_bytes": 0,
        "max_upload_bytes": 0,
        "run_cost_budget_usd": 0,
        "capabilities_version": 1,
    },
    "/api/providers": {"providers": []},
    "/api/provider-profiles": {"profiles": [], "profiles_enabled": False},
    "/api/workspace": {"workspace": "/tmp/ws"},
    "/api/sessions": {"sessions": []},
    "/api/project": {
        "workspace": "/tmp/ws",
        "mode": "native",
        "project": {"python": "", "version": "", "source": "system"},
        "agent_python": "",
        "agent_version": "",
        "package_manager": "",
        "lockfile": "",
        "test_command": "",
        "warnings": [],
    },
    "/api/consent/pending": {"pending": []},
    "/api/memory": {"facts": []},
    "/api/audit": {"records": []},
    "/api/recovery": {"sessions": []},
    "/api/workspaces": {"workspaces": []},
}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self' data:; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        return

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in STUBS:
            self._json(STUBS[path])
            return
        if path.startswith("/api/"):
            self._json({})
            return
        if path in ("/", "/index.html"):
            file = STATIC / "index.html"
        elif path.startswith("/static/"):
            file = STATIC / path[len("/static/"):]
        else:
            self.send_response(404)
            self.end_headers()
            return
        if not file.is_file():
            self.send_response(404)
            self.end_headers()
            return
        suffix = file.suffix.lower()
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript",
            ".css": "text/css",
            ".svg": "image/svg+xml",
        }.get(suffix, "application/octet-stream")
        body = file.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if file.name == "index.html":
            self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    window = webview.create_window(
        "CSP smoke", url=f"http://127.0.0.1:{port}/", width=1200, height=800
    )

    def check() -> None:
        time.sleep(3.0)
        results = {}
        results["app_display"] = window.evaluate_js(
            "getComputedStyle(document.querySelector('#app')).display"
        )
        results["screens"] = window.evaluate_js(
            "({boot: getComputedStyle(document.querySelector('#boot')).display,"
            " login: getComputedStyle(document.querySelector('#login')).display,"
            " config: getComputedStyle(document.querySelector('#config-error')).display,"
            " version: getComputedStyle(document.querySelector('#version-error')).display,"
            " configText: document.querySelector('#config-error').textContent.trim(),"
            " versionText: document.querySelector('#version-error').textContent.trim(),"
            " loginText: document.querySelector('#login').textContent.trim()})"
        )
        results["activity_count"] = window.evaluate_js(
            "document.querySelectorAll('.activity-item').length"
        )
        window.evaluate_js(
            "[...document.querySelectorAll('.activity-item')]"
            ".find(b => b.getAttribute('title') === '文件树').click()"
        )
        time.sleep(0.4)
        results["sidebar_title"] = window.evaluate_js(
            "document.querySelector('.sidebar-head h3').textContent.trim()"
        )
        window.evaluate_js(
            "document.querySelector('header .icon-btn[aria-label=\"打开设置\"]').click()"
        )
        time.sleep(0.4)
        results["settings_display"] = window.evaluate_js(
            "getComputedStyle(document.querySelector('.settings-shell')).display"
        )
        window.evaluate_js(
            "[...document.querySelectorAll('.settings-nav button')]"
            ".find(b => b.textContent.trim() === '模型').click()"
        )
        time.sleep(0.4)
        results["settings_model_visible"] = window.evaluate_js(
            "getComputedStyle(document.querySelectorAll('.settings-view')[1]).display"
        )
        window.evaluate_js(
            "document.querySelector('header .icon-btn[aria-label=\"打开设置\"]').click()"
        )
        time.sleep(0.3)
        window.evaluate_js("document.querySelector('.command-trigger').click()")
        time.sleep(0.4)
        results["command_display"] = window.evaluate_js(
            "getComputedStyle(document.querySelector('.command-modal')).display"
        )
        results["command_focus"] = window.evaluate_js(
            "document.activeElement && document.activeElement.getAttribute('aria-label')"
        )
        window.evaluate_js(
            "document.querySelectorAll('.session-filters button')[1].click()"
        )
        time.sleep(0.2)
        results["session_filter_class"] = window.evaluate_js(
            "document.querySelectorAll('.session-filters button')[1].className"
        )
        window.evaluate_js(
            "[...document.querySelectorAll('.inspector .tabs button')]"
            ".find(b => b.textContent.trim() === '工具').click()"
        )
        time.sleep(0.2)
        results["inspector_tool_display"] = window.evaluate_js(
            "getComputedStyle(document.querySelectorAll('.inspector .panel')[1]).display"
        )
        window.evaluate_js(
            "const i=document.querySelector('.command-input-row input');"
            "i.value='主题：浅色';i.dispatchEvent(new Event('input',{bubbles:true}));"
        )
        time.sleep(0.3)
        window.evaluate_js(
            "[...document.querySelectorAll('.command-item')]"
            ".find(b => b.textContent.includes('主题：浅色')).click()"
        )
        time.sleep(0.3)
        results["theme_class"] = window.evaluate_js("document.body.className")
        results["theme_light"] = "theme-light" in results["theme_class"]
        print(json.dumps(results, ensure_ascii=False))
        window.destroy()

    webview.start(check)
    server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
