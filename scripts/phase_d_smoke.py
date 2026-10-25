"""Phase D 故障演练（协议级，不调用 LLM）。

    python scripts/phase_d_smoke.py
    python scripts/phase_d_smoke.py --keep

场景：
  1. A3/A2 启动与握手
  2. 会话 CRUD 与 SQLite 持久化
  3. A3 崩溃 → A2 降级 → A3 重启后恢复
  4. A2 崩溃重启 → 会话仍在
  5. 控制面数据库不可达 → 只读降级（GET 200 / POST 503）
  6. A3 协议版本不匹配 → agent_state=incompatible
  7. 迁移失败 → --migrate-only 非零退出

默认在 Windows/Linux 本机启动两个 loopback 进程，不依赖 WSL、MySQL 或 API Key。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_ENV_KEYS = (
    "AGENT_INTERNAL_URL",
    "AGENT_INTERNAL_TOKEN",
    "AGENT_CONTROL_DB_URL",
    "AGENT_CONTROL_DB_AUTO_CREATE",
    "AGENT_CONTROL_BACKUP_DIR",
    "AGENT_WEB_AUTH_TOKENS",
    "AGENT_WEB_AUTH_TOKENS_FILE",
    "AGENT_AUDIT_DIR",
    "AGENT_WSL_WORKSPACE",
    "AGENT_WEB_DATA_ROOT",
    "AGENT_CHECKPOINT_PATH",
    "AGENT_STEERING_DIR",
    "AGENT_AGENT_DATA_ROOT",
)


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    for key in _ENV_KEYS:
        env.pop(key, None)
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _free_ports(count: int) -> list[int]:
    sockets: list[socket.socket] = []
    ports: list[int] = []
    try:
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(int(sock.getsockname()[1]))
    finally:
        for sock in sockets:
            sock.close()
    return ports


def _http(
    base: str,
    method: str,
    path: str,
    *,
    token: str = "",
    body: dict | None = None,
) -> tuple[int, dict]:
    data = (
        json.dumps(body, ensure_ascii=False).encode("utf-8")
        if body is not None
        else None
    )
    request = urllib.request.Request(base + path, data=data, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with _OPENER.open(request, timeout=10) as response:
            raw = response.read()
            return response.status, (
                json.loads(raw) if raw else {}
            )
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {"detail": raw.decode("utf-8", "replace")}
        return exc.code, payload


def _health(base: str) -> dict:
    status, data = _http(base, "GET", "/healthz")
    if status != 200:
        raise RuntimeError(f"healthz {status}: {data}")
    return data


def _wait(check, timeout: float, label: str):
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            value = check()
            if value:
                return value
        except Exception as exc:  # noqa: BLE001 - 启动阶段持续重试
            last = exc
        time.sleep(0.25)
    detail = f"：{last}" if last else ""
    raise RuntimeError(f"{label} 超时{detail}")


def _start(
    command: list[str],
    env: dict[str, str],
    log_path: Path,
    token: str,
    *,
    cwd: Path | None = None,
) -> subprocess.Popen:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    flags = (
        getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    )
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=handle,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        env=env,
        cwd=str(cwd) if cwd is not None else None,
        creationflags=flags,
    )
    assert proc.stdin is not None
    proc.stdin.write(token + "\n")
    proc.stdin.flush()
    proc.stdin.close()
    return proc


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _tail(path: Path, lines: int = 15) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])


class _FakeMetaHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        if self.path == "/v1/meta":
            payload = {
                "service": "agent-server",
                "version": "0.0.0",
                "api_version": 999,
                "execution_backend": "local",
                "capabilities": {},
            }
            self._send(payload)
        elif self.path == "/healthz":
            self._send({"status": "ok"})
        else:
            self.send_error(404)

    def _send(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:
        return


def _fake_meta_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeMetaHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class Smoke:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.tmp = Path(tempfile.mkdtemp(prefix="phase-d-smoke-"))
        self.python = args.python
        self.procs: list[subprocess.Popen] = []
        self.results: list[tuple[str, bool, str]] = []
        self.a3_token = secrets.token_urlsafe(24)
        self.a2_token = secrets.token_urlsafe(24)
        self.a3_port, self.a2_port = _free_ports(2)
        self.a3_base = f"http://127.0.0.1:{self.a3_port}"
        self.a2_base = f"http://127.0.0.1:{self.a2_port}"
        self.a3: subprocess.Popen | None = None
        self.a2: subprocess.Popen | None = None
        self.fake: ThreadingHTTPServer | None = None

    # ---------- 进程 ----------

    def _a3_env(self) -> dict[str, str]:
        env = _clean_env()
        env.update(
            {
                "AGENT_AUDIT_DIR": str(self.tmp / "a3-audit"),
                "AGENT_AGENT_DATA_ROOT": str(self.tmp / "a3-agent"),
                "AGENT_CHECKPOINT_PATH": str(
                    self.tmp / "a3-checkpoints.sqlite"
                ),
                "AGENT_STEERING_DIR": str(self.tmp / "a3-steering"),
            }
        )
        return env

    def _a2_env(
        self,
        *,
        db_url: str,
        agent_url: str = "",
        agent_token: str = "",
        audit_name: str = "a2-audit",
    ) -> dict[str, str]:
        env = _clean_env()
        env.update(
            {
                "AGENT_CONTROL_DB_URL": db_url,
                "AGENT_CONTROL_DB_AUTO_CREATE": "true",
                "AGENT_CONTROL_BACKUP_DIR": str(self.tmp / "backups"),
                "AGENT_AUDIT_DIR": str(self.tmp / audit_name),
                "AGENT_WEB_DATA_ROOT": str(self.tmp / "web-data"),
                "AGENT_WSL_WORKSPACE": str(self.tmp / "workspaces"),
            }
        )
        if agent_url:
            env["AGENT_INTERNAL_URL"] = agent_url
            env["AGENT_INTERNAL_TOKEN"] = agent_token
        return env

    def start_a3(self) -> subprocess.Popen:
        proc = _start(
            [
                self.python,
                "-m",
                "coding_agent.agent_server",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.a3_port),
                "--token-stdin",
                "--log-level",
                "warning",
            ],
            self._a3_env(),
            self.tmp / "a3.log",
            self.a3_token,
            cwd=self.tmp,
        )
        self.procs.append(proc)
        return proc

    def start_a2(
        self,
        *,
        db_url: str,
        agent_url: str = "",
        agent_token: str = "",
        audit_name: str = "a2-audit",
        port: int | None = None,
        log_name: str = "a2.log",
    ) -> tuple[subprocess.Popen, str]:
        chosen = port or self.a2_port
        base = f"http://127.0.0.1:{chosen}"
        proc = _start(
            [
                self.python,
                "-m",
                "coding_agent.control_server",
                "--host",
                "127.0.0.1",
                "--port",
                str(chosen),
                "--token-stdin",
                "--desktop",
                "--log-level",
                "warning",
            ],
            self._a2_env(
                db_url=db_url,
                agent_url=agent_url,
                agent_token=agent_token,
                audit_name=audit_name,
            ),
            self.tmp / log_name,
            self.a2_token,
            cwd=self.tmp,
        )
        self.procs.append(proc)
        return proc, base

    # ---------- 场景 ----------

    def check(self, name: str, fn) -> None:
        try:
            detail = fn() or ""
        except Exception as exc:  # noqa: BLE001 - 场景失败要继续跑后续
            self.results.append((name, False, f"{type(exc).__name__}: {exc}"))
            print(f"[FAIL] {name}: {type(exc).__name__}: {exc}")
        else:
            self.results.append((name, True, detail))
            print(f"[PASS] {name} {detail}")

    def scenario_start_and_handshake(self) -> str:
        self.a3 = self.start_a3()
        _wait(
            lambda: _health(self.a3_base).get("status") == "ok",
            self.args.timeout,
            "A3 healthz",
        )
        self.a2, self.a2_base = self.start_a2(
            db_url=f"sqlite:///{(self.tmp / 'control.db').as_posix()}",
            agent_url=self.a3_base,
            agent_token=self.a3_token,
        )
        health = _wait(
            lambda: (
                _health(self.a2_base)
                if _health(self.a2_base).get("agent_state") == "ok"
                else None
            ),
            self.args.timeout,
            "A2 握手",
        )
        return f"agent_state={health['agent_state']} db_state={health['db_state']}"

    def _threads(self) -> set[str]:
        status, data = _http(
            self.a2_base, "GET", "/api/sessions", token=self.a2_token
        )
        if status != 200:
            raise RuntimeError(f"GET /api/sessions {status}: {data}")
        return {row["thread_id"] for row in data.get("sessions", [])}

    def scenario_sessions(self) -> str:
        status, data = _http(
            self.a2_base,
            "POST",
            "/api/sessions",
            token=self.a2_token,
            body={"thread_id": "smoke-keep"},
        )
        if status != 200:
            raise RuntimeError(f"创建会话 {status}: {data}")
        status, _ = _http(
            self.a2_base,
            "POST",
            "/api/sessions",
            token=self.a2_token,
            body={"thread_id": "smoke-del"},
        )
        if status != 200:
            raise RuntimeError(f"创建 smoke-del {status}")
        threads = self._threads()
        if not {"smoke-keep", "smoke-del"} <= threads:
            raise RuntimeError(f"列表缺少会话：{threads}")
        status, _ = _http(
            self.a2_base,
            "DELETE",
            "/api/sessions/smoke-del",
            token=self.a2_token,
        )
        if status != 200:
            raise RuntimeError(f"删除会话 {status}")
        if "smoke-del" in self._threads():
            raise RuntimeError("tombstone 会话仍在列表")
        return "创建/列表/删除通过"

    def scenario_a3_crash(self) -> str:
        _stop(self.a3)
        _wait(
            lambda: (
                _health(self.a2_base).get("agent_state") == "unavailable"
            ),
            self.args.timeout,
            "A2 感知 A3 下线",
        )
        self.a3 = self.start_a3()
        _wait(
            lambda: _health(self.a2_base).get("agent_state") == "ok",
            self.args.timeout,
            "A2 感知 A3 恢复",
        )
        return "unavailable → ok"

    def scenario_a2_restart(self) -> str:
        _stop(self.a2)
        self.a2, self.a2_base = self.start_a2(
            db_url=f"sqlite:///{(self.tmp / 'control.db').as_posix()}",
            agent_url=self.a3_base,
            agent_token=self.a3_token,
        )
        _wait(
            lambda: _health(self.a2_base).get("status") == "ok",
            self.args.timeout,
            "A2 重启",
        )
        if "smoke-keep" not in self._threads():
            raise RuntimeError("重启后会话丢失")
        return "smoke-keep 仍在"

    def scenario_mysql_degraded(self) -> str:
        port = _free_ports(1)[0]
        proc, base = self.start_a2(
            db_url=(
                "mysql+pymysql://smoke:smoke@127.0.0.1:1/smoke"
                "?connect_timeout=1"
            ),
            port=port,
            log_name="a2-degraded.log",
        )
        try:
            health = _wait(
                lambda: (
                    _health(base)
                    if _health(base).get("db_state") == "degraded"
                    else None
                ),
                self.args.timeout,
                "控制面降级",
            )
            status, _ = _http(
                base,
                "POST",
                "/api/sessions",
                token=self.a2_token,
                body={"thread_id": "degraded"},
            )
            if status != 503:
                raise RuntimeError(f"降级下 POST 应为 503，实际 {status}")
            status, _ = _http(
                base, "GET", "/api/sessions", token=self.a2_token
            )
            if status != 200:
                raise RuntimeError(f"降级下 GET 应为 200，实际 {status}")
            return f"db_state={health['db_state']} POST=503 GET=200"
        finally:
            _stop(proc)

    def scenario_version_mismatch(self) -> str:
        self.fake = _fake_meta_server()
        port = int(self.fake.server_address[1])
        fake_base = f"http://127.0.0.1:{port}"
        a2_port = _free_ports(1)[0]
        proc, base = self.start_a2(
            db_url=f"sqlite:///{(self.tmp / 'incompat.db').as_posix()}",
            agent_url=fake_base,
            agent_token="smoke",
            audit_name="a2-incompat-audit",
            port=a2_port,
            log_name="a2-incompat.log",
        )
        try:
            health = _wait(
                lambda: (
                    _health(base)
                    if _health(base).get("agent_state") == "incompatible"
                    else None
                ),
                self.args.timeout,
                "版本不匹配",
            )
            return f"agent_state={health['agent_state']}"
        finally:
            _stop(proc)
            self.fake.shutdown()
            self.fake.server_close()
            self.fake = None

    def scenario_migration_failure(self) -> str:
        env = _clean_env()
        env.update(
            {
                # 用不存在的父目录让 SQLite 立即失败；不可达 MySQL 在部分
                # Windows 防火墙配置下会等 TCP 超时，拖慢演练。
                "AGENT_CONTROL_DB_URL": (
                    "sqlite:///"
                    f"{(self.tmp / 'missing-dir' / 'control.db').as_posix()}"
                ),
                "AGENT_AUDIT_DIR": str(self.tmp / "migrate-audit"),
            }
        )
        result = subprocess.run(
            [
                self.python,
                "-m",
                "coding_agent.control_server",
                "--migrate-only",
            ],
            env=env,
            cwd=str(self.tmp),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            raise RuntimeError("迁移失败场景却返回 0")
        if "control_db_migrate_failed" not in result.stderr:
            raise RuntimeError(f"stderr 缺少失败事件：{result.stderr[-200:]}")
        return f"exit={result.returncode}"

    # ---------- 主流程 ----------

    def run(self) -> int:
        scenarios = [
            ("1. A3/A2 启动与握手", self.scenario_start_and_handshake),
            ("2. 会话 CRUD", self.scenario_sessions),
            ("3. A3 崩溃与恢复", self.scenario_a3_crash),
            ("4. A2 重启持久化", self.scenario_a2_restart),
            ("5. 数据库降级", self.scenario_mysql_degraded),
            ("6. 版本不匹配", self.scenario_version_mismatch),
            ("7. 迁移失败", self.scenario_migration_failure),
        ]
        try:
            for name, fn in scenarios:
                self.check(name, fn)
        finally:
            for proc in self.procs:
                _stop(proc)
            if self.fake is not None:
                self.fake.shutdown()
                self.fake.server_close()
            if not self.args.keep:
                shutil.rmtree(self.tmp, ignore_errors=True)

        passed = sum(1 for _, ok, _ in self.results if ok)
        print(f"\n结果：{passed}/{len(self.results)} 通过")
        if self.args.keep:
            print(f"临时目录：{self.tmp}")
        failed = [name for name, ok, _ in self.results if not ok]
        if failed:
            for name in failed:
                print(f"失败：{name}")
            for log in sorted(self.tmp.glob("*.log")):
                print(f"\n--- {log.name} ---\n{_tail(log)}")
            return 1
        return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Phase D 故障演练")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args(argv)
    if not Path(args.python).exists():
        print(f"Python 不存在：{args.python}", file=sys.stderr)
        return 2
    return Smoke(args).run()


if __name__ == "__main__":
    raise SystemExit(main())
