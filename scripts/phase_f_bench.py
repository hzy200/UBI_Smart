"""Phase F：可复现的性能基线（不调用 LLM）。

    python scripts/phase_f_bench.py
    python scripts/phase_f_bench.py --mysql-url "mysql+pymysql://root:...@127.0.0.1:3306/coding_agent_test"

测量：
  1. ExecutionService 代理相对 backend 直调的开销；
  2. 控制面 SQLite/MySQL 的会话写入与审计索引耗时、库体积；
  3. A2 代理 SSE 的首事件延迟（fake A3，不发真实模型请求）。

不设阈值；输出 JSON，便于版本间比较。长会话渲染需要浏览器，未纳入本脚本。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import secrets
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from coding_agent.control.store import ControlStore  # noqa: E402
from coding_agent.execution.contracts import ExecutionRequest  # noqa: E402
from coding_agent.execution.service import ExecutionService  # noqa: E402

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _stats(values: list[float]) -> dict:
    if not values:
        return {"count": 0}
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean_ms": round(sum(ordered) / len(ordered) * 1000, 3),
        "p50_ms": round(ordered[len(ordered) // 2] * 1000, 3),
        "p95_ms": round(
            ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] * 1000,
            3,
        ),
    }


class _FakeBackend:
    def capabilities(self) -> dict:
        return {}

    def run(self, command: str, **kwargs) -> Any:
        return SimpleNamespace(
            command=command,
            exit_code=0,
            stdout="ok",
            stderr="",
            duration_ms=0,
            ok=True,
            timed_out=False,
        )


def measure_execution_proxy(workdir: Path, rounds: int = 400) -> dict:
    workspace = workdir / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    backend = _FakeBackend()
    service = ExecutionService(
        backend, str(workspace), journal_path=workdir / "receipts.jsonl"
    )
    direct: list[float] = []
    proxied: list[float] = []
    for index in range(rounds):
        start = time.perf_counter()
        backend.run("true", cwd=str(workspace), timeout=5, capture_limit=0)
        direct.append(time.perf_counter() - start)

        start = time.perf_counter()
        service.execute(
            ExecutionRequest(
                op_id=f"bench-{index}",
                kind="run",
                run_id="bench",
                payload={"command": "true"},
                timeout_s=5,
            )
        )
        proxied.append(time.perf_counter() - start)
    return {
        "direct": _stats(direct),
        "proxied": _stats(proxied),
        "journal_bytes": (workdir / "receipts.jsonl").stat().st_size,
    }


def _db_bytes_sqlite(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0


def _db_bytes_mysql(url: str) -> int:
    from sqlalchemy import create_engine

    database = make_url(url).database
    engine = create_engine(url, future=True)
    try:
        with engine.connect() as connection:
            value = connection.execute(
                text(
                    "SELECT COALESCE(SUM(data_length + index_length), 0) "
                    "FROM information_schema.tables "
                    "WHERE table_schema = :name"
                ),
                {"name": database},
            ).scalar()
        return int(value or 0)
    finally:
        engine.dispose()


def measure_control_store(
    url: str, *, mysql: bool = False, rounds: int = 60
) -> dict:
    store = ControlStore(url)
    sessions = [
        {"thread_id": f"bench-{index}", "title": "bench"}
        for index in range(10)
    ]
    writes: list[float] = []
    for index in range(rounds):
        sessions[0]["title"] = f"bench-{index}"
        start = time.perf_counter()
        store.save_sessions("bench", sessions, set())
        writes.append(time.perf_counter() - start)

    records = [
        {
            "hash": f"bench-hash-{index}",
            "tool": "shell_exec",
            "level": "L0",
            "decision": "auto",
            "ok": True,
        }
        for index in range(500)
    ]
    start = time.perf_counter()
    indexed = store.index_audit("bench", records)
    audit_s = time.perf_counter() - start
    size = _db_bytes_mysql(url) if mysql else _db_bytes_sqlite(
        Path(make_url(url).database or "")
    )
    store.close()
    return {
        "save_sessions": _stats(writes),
        "index_audit_500_ms": round(audit_s * 1000, 3),
        "indexed": indexed,
        "db_bytes": size,
    }


class _FakeA3Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/v1/meta":
            self._json(
                {
                    "service": "agent-server",
                    "version": "0.1.0",
                    "api_version": 1,
                    "execution_backend": "local",
                    "capabilities": {},
                }
            )
        elif self.path == "/healthz":
            self._json({"status": "ok"})
        else:
            self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        if length:
            self.rfile.read(length)
        if self.path != "/v1/runs":
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for seq in range(1, 4):
            payload = {
                "type": "assistant_token",
                "node": "respond",
                "text": "x",
            }
            self.wfile.write(
                f"id: {seq}\ndata: {json.dumps(payload)}\n\n".encode()
            )
            self.wfile.flush()
        payload = {"type": "run_finished", "thread_id": "t", "answer": "ok"}
        self.wfile.write(
            f"id: 4\ndata: {json.dumps(payload)}\n\n".encode()
        )
        self.wfile.flush()
        self.close_connection = True

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


def _load_smoke():
    path = ROOT / "scripts" / "phase_d_smoke.py"
    spec = importlib.util.spec_from_file_location("phase_d_smoke_bench", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_sse_once(base: str, token: str, thread_id: str) -> float:
    body = json.dumps({"prompt": "bench", "thread_id": thread_id}).encode()
    request = urllib.request.Request(
        base + "/api/run",
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    start = time.perf_counter()
    first: float | None = None
    with _OPENER.open(request, timeout=10) as response:
        while True:
            line = response.readline()
            if not line:
                break
            if line.startswith(b"data:"):
                first = time.perf_counter() - start
                break
        response.read()
    if first is None:
        raise RuntimeError("SSE 未返回 data 帧")
    return first


def measure_sse_proxy(workdir: Path, rounds: int = 20) -> dict:
    smoke = _load_smoke()
    fake = ThreadingHTTPServer(("127.0.0.1", 0), _FakeA3Handler)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    fake_port = int(fake.server_address[1])
    a2_port = smoke._free_ports(1)[0]
    token = secrets.token_urlsafe(16)
    env = smoke._clean_env()
    env.update(
        {
            "AGENT_INTERNAL_URL": f"http://127.0.0.1:{fake_port}",
            "AGENT_INTERNAL_TOKEN": "bench",
            "AGENT_CONTROL_DB_URL": (
                f"sqlite:///{(workdir / 'control.db').as_posix()}"
            ),
            "AGENT_CONTROL_DB_AUTO_CREATE": "true",
            "AGENT_AUDIT_DIR": str(workdir / "a2-audit"),
            "AGENT_WEB_DATA_ROOT": str(workdir / "web-data"),
            "AGENT_WSL_WORKSPACE": str(workdir / "ws"),
        }
    )
    proc = smoke._start(
        [
            sys.executable,
            "-m",
            "coding_agent.control_server",
            "--host",
            "127.0.0.1",
            "--port",
            str(a2_port),
            "--token-stdin",
            "--desktop",
            "--log-level",
            "warning",
        ],
        env,
        workdir / "a2.log",
        token,
    )
    base = f"http://127.0.0.1:{a2_port}"
    first_events: list[float] = []
    try:
        smoke._wait(
            lambda: smoke._health(base).get("agent_state") == "ok",
            30,
            "A2 握手",
        )
        # 预热同一会话，排除首次建会话/写 SQLite 的冷启动成本。
        for _ in range(2):
            _run_sse_once(base, token, "bench-sse")
        for _ in range(rounds):
            first_events.append(_run_sse_once(base, token, "bench-sse"))
    finally:
        smoke._stop(proc)
        fake.shutdown()
        fake.server_close()
    return {
        "first_event": _stats(first_events),
        "a2_to_fake_a3": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Phase F 性能基线")
    parser.add_argument("--mysql-url", default="")
    parser.add_argument("--rounds", type=int, default=400)
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="phase-f-bench-"))
    report: dict = {}
    try:
        report["execution_proxy"] = measure_execution_proxy(
            tmp / "exec", rounds=max(20, args.rounds)
        )
        sqlite_url = f"sqlite:///{(tmp / 'control.db').as_posix()}"
        report["control_store_sqlite"] = measure_control_store(sqlite_url)
        if args.mysql_url:
            database = make_url(args.mysql_url).database or ""
            if "test" not in database:
                raise RuntimeError("MySQL 库名必须包含 test")
            report["control_store_mysql"] = measure_control_store(
                args.mysql_url, mysql=True
            )
        report["sse_proxy"] = measure_sse_proxy(tmp / "sse")
    except Exception as exc:  # noqa: BLE001 - 基线脚本要可读
        print(
            json.dumps(
                {"ok": False, "error": f"{type(exc).__name__}: {exc}", **report},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1
    finally:
        if not args.keep:
            shutil.rmtree(tmp, ignore_errors=True)

    print(json.dumps({"ok": True, **report}, ensure_ascii=False, indent=2))
    if args.keep:
        print(f"临时目录：{tmp}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
