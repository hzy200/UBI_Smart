"""Web 最小验证界面。

用 FastAPI 的 TestClient 打真实端点，只把 AgentRuntime 换成假的 ——
LLM 与沙箱不参与，测的是「事件流 → SSE 帧」这条链路和端点形状。
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from coding_agent import web
from coding_agent.config import Settings
from coding_agent.events import (
    ApprovalRequested,
    AssistantToken,
    PlanCreated,
    RunFinished,
    RunStarted,
    SteeringApplied,
    ToolCallFinished,
    ToolCallStarted,
)
from coding_agent.runtime import HistoryMessage
from coding_agent.web import local_runtime

SCRIPT = [
    RunStarted(thread_id="t"),
    PlanCreated(steps=["看目录"]),
    AssistantToken(node="act", text="先看看"),
    ToolCallStarted(name="shell_exec", args={}, summary="ls", level="L0 只读"),
    ToolCallFinished(name="shell_exec", ok=True, exit_code=0, duration_ms=10),
    AssistantToken(node="respond", text="目录是空的"),
    RunFinished(thread_id="t", answer="目录是空的"),
]


class FakeRuntime:
    """按脚本回放事件；可切换成「抛异常」来验证错误也走事件流。"""

    def __init__(self, *, events=None, error: Exception | None = None,
                 history=None, workspace: str = "/mnt/d/proj") -> None:
        self._events = events if events is not None else SCRIPT
        self._error = error
        self._history = history or []
        self.workspace = workspace
        self.ran: list[tuple[str, str]] = []
        self.steered: list[tuple[str, str]] = []
        self.resumed: list[tuple[str, dict[str, bool]]] = []
        self.steering_resumed = 0
        self.paused = False
        self.closed = False

    async def run(self, prompt: str, *, thread_id: str):
        self.ran.append((prompt, thread_id))
        if self._error is not None:
            raise self._error
        for event in self._events:
            if isinstance(event, SteeringApplied) and event.action == "pause":
                self.paused = True
            yield event

    def steer(self, thread_id: str, action: str, **kwargs) -> None:
        self.steered.append((thread_id, action))
        if action == "pause":
            self.paused = True

    def is_paused(self, thread_id: str) -> bool:
        return self.paused

    async def resume(self, thread_id: str, decisions: dict[str, bool]):
        self.resumed.append((thread_id, decisions))
        self.paused = False
        yield RunFinished(thread_id=thread_id, answer="resumed")

    async def resume_steering(self, thread_id: str):
        self.steering_resumed += 1
        self.paused = False
        yield RunFinished(thread_id=thread_id, answer="resumed")

    async def history(
        self,
        thread_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ):
        return self._history

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        _env_file=None,
        audit_dir=str(tmp_path / "audit"),
        checkpoint_path=":memory:",
        deepseek_api_key="sk-test",
        # 显式 workspace 避免本地匿名模式去探测 WSL $HOME。
        wsl_workspace="/tmp/ws",
        web_data_root=str(tmp_path / "web-data"),
    )


def _client(monkeypatch, settings: Settings, runtime: object | None = None) -> TestClient:
    """路由里是现建 AgentRuntime 的，这里把工厂整体换掉。

    用 monkeypatch 而不是直接赋值模块全局 —— 后者会跨用例泄漏。
    """
    runtime = runtime or FakeRuntime()
    monkeypatch.setattr(local_runtime, "AgentRuntime", lambda *a, **k: runtime)
    return TestClient(web.create_app(settings))


def _frames(text: str) -> list[dict]:
    out = []
    for frame in text.split("\n\n"):
        payload = frame.removeprefix("data: ").strip()
        if payload:
            out.append(json.loads(payload))
    return out


# ---------------- 页面与联通测试 ----------------

def test_index_page_is_served(settings) -> None:
    resp = TestClient(web.create_app(settings)).get("/")
    assert resp.status_code == 200
    assert "CodingAgent" in resp.text
    assert "text/event-stream" not in resp.text  # 是页面不是流


def test_index_has_no_build_chain(settings) -> None:
    """刻意不引前端构建链：静态 JS/CSS 直接由应用提供，没有打包步骤。"""
    client = TestClient(web.create_app(settings))
    response = client.get("/")
    html = response.text
    assert "/static/app.js" in html
    assert "/static/style.css" in html
    assert "webpack" not in html.lower()
    assert "vite" not in html.lower()
    assert "/static/vendor/alpine-csp.min.js" in html
    assert "unsafe-eval" not in response.headers.get(
        "content-security-policy", ""
    )
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/vendor/alpine-csp.min.js").status_code == 200


def test_desktop_endpoints_reject_browser_mode(settings) -> None:
    client = TestClient(web.create_app(settings))
    for path in ("/api/desktop/cancel_all", "/api/desktop/shutdown"):
        response = client.post(path)
        assert response.status_code == 403
        assert response.json()["code"] == "desktop.unavailable"


def test_runtime_config_is_public_and_same_origin(settings) -> None:
    response = TestClient(web.create_app(settings)).get("/config.json")
    assert response.status_code == 200
    data = response.json()
    assert data["schemaVersion"] == 1
    assert data["apiBaseUrl"] == ""
    assert data["apiVersion"] == "v0"
    assert data["authMode"] == "bearer"
    assert data["sse"]["heartbeatMs"] == 15000
    assert data["sse"]["idleTimeoutMs"] == 45000
    assert data["requestIdHeader"] == "X-Request-ID"
    assert response.headers["cache-control"] == "no-cache"
    # 配置公开可读，但不得包含任何凭证或密钥。
    assert "token" not in json.dumps(data).lower()
    assert "key" not in json.dumps(data).lower()


def test_runtime_config_marks_desktop_mode(settings) -> None:
    client = TestClient(web.create_app(settings, desktop_mode=True))
    data = client.get("/config.json").json()
    assert data["authMode"] == "desktop-token"
    assert data["apiBaseUrl"] == ""


def test_runtime_config_stays_public_when_auth_is_enabled(settings) -> None:
    locked = settings.model_copy(
        update={"web_auth_tokens": '{"secret-token": "alice"}'}
    )
    client = TestClient(web.create_app(locked))
    assert client.get("/config.json").status_code == 200
    assert client.get("/api/capabilities").status_code == 401


def test_healthz_reports_frontend_version_contract(settings) -> None:
    client = TestClient(web.create_app(settings))
    data = client.get("/healthz").json()
    assert data["api_version"] == "v0"
    assert data["min_frontend_version"] == "0.1.0"
    assert data["capabilities_version"] == 1
    assert data["version"] == "0.1.0"
    # 版本握手必须先于认证，否则前端无法在登录前阻断。
    locked = settings.model_copy(
        update={"web_auth_tokens": '{"secret-token": "alice"}'}
    )
    assert TestClient(web.create_app(locked)).get("/healthz").status_code == 200


def test_capabilities_reports_capabilities_version(settings) -> None:
    data = TestClient(web.create_app(settings)).get("/api/capabilities").json()
    assert data["capabilities_version"] == 1


def test_health_reports_configuration(settings) -> None:
    data = TestClient(web.create_app(settings)).get("/api/health").json()
    assert data["api_key"] is True
    assert data["reachable"] is True
    assert data["model"] == settings.deepseek_model
    assert "tracing" in data


def test_health_without_api_key(settings) -> None:
    bare = settings.model_copy(update={"deepseek_api_key": ""})
    data = TestClient(web.create_app(bare)).get("/api/health").json()
    assert data["reachable"] is False
    assert "DEEPSEEK_API_KEY" in data["error"]


def test_health_survives_broken_sandbox(monkeypatch, settings) -> None:
    """沙箱不可用时也要给出可读的错误，而不是 500。"""
    class _Broken:
        def __init__(self, *a, **k) -> None:
            pass

        @property
        def workspace(self) -> str:
            raise RuntimeError("WSL 不可用")

        async def aclose(self) -> None:
            pass

    def _broken(_self, _user):
        raise RuntimeError("WSL 不可用")

    monkeypatch.setattr(web.app.TenantManager, "for_user", _broken)
    data = TestClient(web.create_app(settings)).get("/api/health").json()
    assert data["reachable"] is True
    assert "WSL 不可用" in data["error"]
    assert data["workspace"] is None


# ---------------- 会话列表 ----------------

def test_sessions_are_listed(settings, tmp_path) -> None:
    from coding_agent.audit import AuditLogger, AuditRecord

    logger = AuditLogger(settings.resolved_audit_dir)
    logger.write(AuditRecord(ts="2026-10-03T10:00:00+00:00", kind="run_start",
                             thread_id="abc", detail="看看目录"))
    logger.write(AuditRecord(ts="2026-10-03T10:00:01+00:00", kind="tool_call",
                             thread_id="abc", tool="shell_exec", decision="auto", ok=True))

    data = TestClient(web.create_app(settings)).get("/api/sessions").json()
    assert len(data["sessions"]) == 1
    row = data["sessions"][0]
    assert row["thread_id"] == "abc"
    assert row["title"] == "看看目录"
    assert row["prompts"] == 1
    assert row["tool_calls"] == 1


def test_sessions_empty(settings) -> None:
    assert TestClient(web.create_app(settings)).get("/api/sessions").json() == {"sessions": []}


def test_sessions_respects_limit(settings, monkeypatch) -> None:
    from coding_agent.audit import AuditLogger, AuditRecord

    logger = AuditLogger(settings.resolved_audit_dir)
    for i in range(5):
        logger.write(AuditRecord(ts=f"2026-10-0{i + 1}T10:00:00+00:00", kind="run_start",
                                 thread_id=f"t{i}", detail=f"会话 {i}"))

    data = TestClient(web.create_app(settings)).get("/api/sessions?limit=2").json()
    assert [s["thread_id"] for s in data["sessions"]] == ["t4", "t3"]


# ---------------- 流式执行 ----------------

def test_run_streams_domain_events(monkeypatch, settings) -> None:
    client = _client(monkeypatch, settings)
    resp = client.post("/api/run", json={"prompt": "看看目录"})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    kinds = [f["type"] for f in _frames(resp.text)]
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_finished"
    assert {"plan_created", "assistant_token", "tool_call_started"} <= set(kinds)


def test_run_assigns_a_thread_id_when_missing(monkeypatch, settings) -> None:
    client = _client(monkeypatch, settings)
    resp = client.post("/api/run", json={"prompt": "hi"})
    assert resp.headers["x-thread-id"]
    assert 8 <= len(resp.headers["x-thread-id"]) <= 64


def test_run_reuses_given_thread_id(monkeypatch, settings) -> None:
    runtime = FakeRuntime()
    monkeypatch.setattr(local_runtime, "AgentRuntime", lambda *a, **k: runtime)

    TestClient(web.create_app(settings)).post(
        "/api/run", json={"prompt": "接着聊", "thread_id": "t-9"}
    )
    assert runtime.ran == [("接着聊", "t-9")]


def test_run_errors_become_events_not_500(monkeypatch, settings) -> None:
    """运行期异常要以 run_failed 事件送到前端，而不是断掉连接。"""
    client = _client(monkeypatch, settings, FakeRuntime(error=RuntimeError("模型炸了")))
    resp = client.post("/api/run", json={"prompt": "hi"})

    assert resp.status_code == 200
    kinds = [f["type"] for f in _frames(resp.text)]
    assert "run_failed" in kinds
    assert "模型炸了" in next(f for f in _frames(resp.text) if f["type"] == "run_failed")["message"]


def test_run_closes_the_runtime(monkeypatch, settings) -> None:
    """每次请求用完就释放 sqlite 连接，否则跑久了会攒住句柄。"""
    runtime = FakeRuntime()
    monkeypatch.setattr(local_runtime, "AgentRuntime", lambda *a, **k: runtime)

    TestClient(web.create_app(settings)).post("/api/run", json={"prompt": "hi"})
    assert runtime.closed is True


def test_run_rejects_empty_prompt(monkeypatch, settings) -> None:
    client = _client(monkeypatch, settings)
    response = client.post("/api/run", json={"prompt": ""})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "request.invalid"
    assert body["request_id"] == response.headers["x-request-id"]
    assert body["details"]["errors"]


def test_http_errors_use_problem_json_and_request_id(settings) -> None:
    locked = settings.model_copy(
        update={"web_auth_tokens": '{"secret-token": "alice"}'}
    )
    client = TestClient(web.create_app(locked))

    response = client.get("/api/capabilities")
    assert response.status_code == 401
    assert response.headers["content-type"].startswith(
        "application/problem+json"
    )
    body = response.json()
    assert body["type"] == "urn:codingagent:error:auth.required"
    assert body["code"] == "auth.required"
    assert body["status"] == 401
    assert body["retryable"] is False
    assert body["instance"] == "/api/capabilities"
    assert body["request_id"] == response.headers["x-request-id"]
    assert response.headers["www-authenticate"] == "Bearer"

    missing = client.get("/api/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["code"] == "request.not_found"
    assert missing.json()["request_id"] == missing.headers["x-request-id"]


def test_old_or_invalid_frontend_version_is_rejected(settings) -> None:
    client = TestClient(web.create_app(settings))

    ok = client.get(
        "/api/capabilities", headers={"X-Frontend-Version": "0.1.0"}
    )
    assert ok.status_code == 200
    missing = client.get("/api/capabilities")
    assert missing.status_code == 200

    old = client.get(
        "/api/capabilities", headers={"X-Frontend-Version": "0.0.9"}
    )
    assert old.status_code == 426
    body = old.json()
    assert body["code"] == "version.frontend_too_old"
    assert body["details"]["minimum"] == "0.1.0"
    assert body["details"]["received"] == "0.0.9"
    assert body["request_id"] == old.headers["x-request-id"]

    invalid = client.get(
        "/api/capabilities", headers={"X-Frontend-Version": "not-a-version"}
    )
    assert invalid.status_code == 426
    assert invalid.json()["code"] == "version.frontend_too_old"

    # 版本握手端点本身不能被版本中间件拦住。
    assert client.get(
        "/healthz", headers={"X-Frontend-Version": "0.0.9"}
    ).status_code == 200


def test_request_id_is_echoed_only_when_valid(settings) -> None:
    client = TestClient(web.create_app(settings))
    valid = client.get(
        "/healthz", headers={"X-Request-ID": "client-request-123456"}
    )
    assert valid.headers["x-request-id"] == "client-request-123456"

    invalid = client.get(
        "/healthz", headers={"X-Request-ID": "bad id with spaces"}
    )
    assert invalid.headers["x-request-id"] != "bad id with spaces"
    assert len(invalid.headers["x-request-id"]) == 32


def test_security_headers_are_present(monkeypatch, settings) -> None:
    client = _client(monkeypatch, settings)
    response = client.get("/")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]


def test_cors_headers_are_present_on_sse(monkeypatch, settings) -> None:
    settings = settings.model_copy(
        update={"web_cors_allowed_origins": "https://app.example.com"}
    )
    client = _client(monkeypatch, settings)
    with client.stream(
        "POST",
        "/api/run",
        json={"prompt": "hi"},
        headers={"Origin": "https://app.example.com"},
    ) as response:
        assert response.status_code == 200
        assert (
            response.headers["access-control-allow-origin"]
            == "https://app.example.com"
        )
        assert response.headers["content-type"].startswith(
            "text/event-stream"
        )
        assert "no-transform" in response.headers["cache-control"]
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["connection"] == "keep-alive"
        next(response.iter_lines(), None)


def test_chunked_body_is_rejected(settings) -> None:
    client = TestClient(web.create_app(settings))
    response = client.post(
        "/api/run",
        content=iter([b"{}"]),
        headers={
            "Content-Type": "application/json",
            "Transfer-Encoding": "chunked",
        },
    )
    assert response.status_code == 411
    assert response.json()["code"] == "request.length_required"
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_oversized_body_is_rejected(monkeypatch, settings) -> None:
    settings = settings.model_copy(update={"web_max_body_bytes": 32})
    client = _client(monkeypatch, settings)

    response = client.post(
        "/api/run",
        content=b"x" * 64,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["code"] == "request.too_large"
    assert response.json()["details"]["limit_bytes"] == 32
    assert response.json()["details"]["scope"] == "body"


def test_rate_limit_is_per_user(monkeypatch, settings) -> None:
    settings = settings.model_copy(update={"web_rate_limit_per_min": 1})
    client = _client(monkeypatch, settings)

    first = client.post("/api/run", json={"prompt": "hi"})
    second = client.post("/api/run", json={"prompt": "again"})

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"]
    assert second.json()["code"] == "rate.limited"
    assert second.json()["retryable"] is True
    assert second.json()["request_id"] == second.headers["x-request-id"]


def test_run_uses_ask_mode_so_approvals_can_resume(monkeypatch, settings) -> None:
    seen: list[str] = []

    def _capture(*args, **kwargs):
        seen.append(kwargs.get("approval_mode", ""))
        return FakeRuntime()

    monkeypatch.setattr(local_runtime, "AgentRuntime", _capture)
    TestClient(web.create_app(settings)).post("/api/run", json={"prompt": "hi"})

    assert seen == [web.app.WEB_APPROVAL_MODE]
    assert web.app.WEB_APPROVAL_MODE == "ask"


def test_steering_and_resume_share_the_suspended_runtime(
    monkeypatch, settings
) -> None:
    runtime = FakeRuntime(
        events=[
            RunStarted(thread_id="t"),
            SteeringApplied(action="pause", detail="paused"),
        ]
    )
    monkeypatch.setattr(local_runtime, "AgentRuntime", lambda *a, **k: runtime)
    client = TestClient(web.create_app(settings))

    resp = client.post("/api/run", json={"prompt": "hi", "thread_id": "t"})
    assert [f["type"] for f in _frames(resp.text)][-1] == "steering_applied"
    assert runtime.closed is False

    steer = client.post(
        "/api/steer/t", json={"action": "add", "text": "检查边界", "role": "review"}
    )
    assert steer.status_code == 200
    assert runtime.steered == [("t", "add")]

    status = client.get("/api/status/t").json()
    assert status["exists"] is True
    assert status["paused"] is True

    resumed = client.post("/api/resume_steering/t")
    assert [f["type"] for f in _frames(resumed.text)][-1] == "run_finished"
    assert runtime.steering_resumed == 1
    assert runtime.closed is True


def test_resume_approval_rejects_a_steering_pause(
    monkeypatch, settings
) -> None:
    runtime = FakeRuntime(
        events=[
            RunStarted(thread_id="t"),
            SteeringApplied(action="pause", detail="paused"),
        ]
    )
    monkeypatch.setattr(local_runtime, "AgentRuntime", lambda *a, **k: runtime)
    client = TestClient(web.create_app(settings))
    client.post("/api/run", json={"prompt": "hi", "thread_id": "t"})

    response = client.post("/api/resume/t", json={"decisions": {}})
    assert response.status_code == 409
    assert runtime.resumed == []


def test_resume_steering_rejects_a_non_paused_session(
    monkeypatch, settings
) -> None:
    """审批挂起不能误用 steering 恢复：payload 不同，会 fail-closed 拒绝工具。"""
    runtime = FakeRuntime(
        events=[
            RunStarted(thread_id="t"),
            RunFinished(thread_id="t", answer="done"),
        ]
    )
    monkeypatch.setattr(local_runtime, "AgentRuntime", lambda *a, **k: runtime)
    client = TestClient(web.create_app(settings))

    client.post("/api/run", json={"prompt": "hi", "thread_id": "t"})
    resp = client.post("/api/resume_steering/t")

    assert resp.status_code == 409
    assert runtime.steering_resumed == 0


def test_approval_resume_endpoint(monkeypatch, settings) -> None:
    runtime = FakeRuntime(
        events=[
            RunStarted(thread_id="t"),
            ApprovalRequested(
                request_id="c1", tool="shell_exec", command="pip install x"
            ),
        ]
    )
    monkeypatch.setattr(local_runtime, "AgentRuntime", lambda *a, **k: runtime)
    client = TestClient(web.create_app(settings))

    client.post("/api/run", json={"prompt": "hi", "thread_id": "t"})
    assert runtime.closed is False

    resumed = client.post(
        "/api/resume/t", json={"decisions": {"c1": True}}
    )
    assert [f["type"] for f in _frames(resumed.text)][-1] == "run_finished"
    assert runtime.resumed == [("t", {"c1": True})]
    assert runtime.closed is True


# ---------------- 历史 ----------------

def test_thread_history(monkeypatch, settings) -> None:
    runtime = FakeRuntime(history=[
        HistoryMessage("user", "之前问的"),
        HistoryMessage("assistant", "之前的回答"),
    ])
    monkeypatch.setattr(local_runtime, "AgentRuntime", lambda *a, **k: runtime)

    data = TestClient(web.create_app(settings)).get("/api/thread/abc").json()
    assert data["messages"] == [
        {"role": "user", "text": "之前问的"},
        {"role": "assistant", "text": "之前的回答"},
    ]


def test_client_error_report_is_appended(settings) -> None:
    from pathlib import Path

    client = TestClient(web.create_app(settings))
    response = client.post(
        "/api/client-errors",
        json={"kind": "error", "message": "boom", "stack": "stack"},
    )
    assert response.status_code == 200
    path = Path(settings.resolved_audit_dir) / "client-errors.jsonl"
    text = path.read_text(encoding="utf-8")
    assert '"message": "boom"' in text
    assert '"kind": "error"' in text

    bad = client.post("/api/client-errors", json={"message": ""})
    assert bad.status_code == 422


def test_thread_history_empty(monkeypatch, settings) -> None:
    client = _client(monkeypatch, settings)
    assert client.get("/api/thread/none").json() == {
        "messages": [],
        "transcript": [],
        "transcript_has_more": False,
    }


def test_transcript_paginates_from_newest(monkeypatch, settings) -> None:
    """长轨迹按最新一页往前翻，offset 表示已加载的最新条目数。"""
    client = _client(monkeypatch, settings)
    messages = [
        {"role": "user", "text": f"m{index}"} for index in range(5)
    ]
    saved = client.post(
        "/api/thread/t1/transcript", json={"messages": messages}
    )
    assert saved.status_code == 200

    first = client.get("/api/thread/t1?limit=2&offset=0").json()
    assert [item["text"] for item in first["transcript"]] == ["m3", "m4"]
    assert first["transcript_has_more"] is True

    second = client.get("/api/thread/t1?limit=2&offset=2").json()
    assert [item["text"] for item in second["transcript"]] == ["m1", "m2"]
    assert second["transcript_has_more"] is True

    last = client.get("/api/thread/t1?limit=2&offset=4").json()
    assert [item["text"] for item in last["transcript"]] == ["m0"]
    assert last["transcript_has_more"] is False


def test_thread_history_reports_unavailable(monkeypatch, settings) -> None:
    """历史拿不到要给 503 与原因，而不是空列表让前端以为没记录。"""

    class _Broken(FakeRuntime):
        async def history(
            self,
            thread_id: str,
            *,
            limit: int | None = None,
            offset: int = 0,
        ):
            raise RuntimeError("checkpoint 打不开")

    monkeypatch.setattr(local_runtime, "AgentRuntime", lambda *a, **k: _Broken())

    resp = TestClient(web.create_app(settings)).get("/api/thread/abc")
    assert resp.status_code == 503
    assert "checkpoint 打不开" in resp.json()["detail"]


def test_transcript_roundtrip_preserves_tool_cards(settings) -> None:
    """桌面轨迹要保存工具卡片/阶段，重新打开会话才能看到工作痕迹。"""
    from pathlib import Path

    client = TestClient(web.create_app(settings))
    messages = [
        {"role": "user", "phase": "user", "text": "看看目录"},
        {
            "role": "工具",
            "phase": "tool",
            "kind": "tool",
            "name": "file_list",
            "summary": "ls .",
            "preview": "a.txt",
            "state": "完成",
            "ok": True,
        },
        {"role": "assistant", "phase": "final", "text": "目录里只有 a.txt"},
    ]
    response = client.post(
        "/api/thread/t1/transcript", json={"messages": messages}
    )
    assert response.status_code == 200
    assert response.json()["count"] == 3

    path = (
        Path(settings.resolved_audit_dir)
        / "transcripts"
        / "t1.json"
    )
    import json as _json

    saved = _json.loads(path.read_text(encoding="utf-8"))
    assert saved["messages"][1]["kind"] == "tool"
    assert saved["messages"][1]["preview"] == "a.txt"
    assert saved["messages"][2]["phase"] == "final"


def test_session_import_restores_exported_json(monkeypatch, settings) -> None:
    """导出 JSON 应可原样导入到新会话，并保留工具卡片。"""
    client = _client(monkeypatch, settings)
    created = client.post(
        "/api/sessions", json={"thread_id": "t1", "workspace": "/tmp/ws"}
    )
    assert created.status_code == 200
    messages = [
        {"role": "user", "text": "看看目录"},
        {
            "role": "工具",
            "kind": "tool",
            "name": "file_list",
            "summary": "ls",
            "preview": "a.txt",
            "ok": True,
        },
        {"role": "assistant", "text": "只有 a.txt"},
    ]
    saved = client.post(
        "/api/thread/t1/transcript", json={"messages": messages}
    )
    assert saved.status_code == 200

    exported = client.get("/api/sessions/t1/export?format=json").json()
    assert exported["transcript"][1]["kind"] == "tool"

    imported = client.post(
        "/api/sessions/import",
        json={
            "title": exported["title"],
            "messages": exported["messages"],
            "transcript": exported["transcript"],
            "workspace": "/tmp/ws",
        },
    )
    assert imported.status_code == 200
    body = imported.json()
    assert body["imported"] == 3
    assert body["thread_id"] != "t1"

    history = client.get(f"/api/thread/{body['thread_id']}").json()
    assert [item["text"] for item in history["transcript"]] == [
        "看看目录",
        "",
        "只有 a.txt",
    ]
    assert history["transcript"][1]["kind"] == "tool"


def test_transcript_updates_session_preview_and_unread(settings) -> None:
    """保存轨迹时顺带更新列表摘要与未读，不必再读整份 transcript。"""
    client = TestClient(web.create_app(settings))
    created = client.post(
        "/api/sessions", json={"thread_id": "t1", "workspace": "/tmp/ws"}
    )
    assert created.status_code == 200
    response = client.post(
        "/api/thread/t1/transcript",
        json={
            "messages": [
                {"role": "user", "text": "看看目录"},
                {"role": "工具", "kind": "tool", "text": "a.txt"},
                {"role": "assistant", "text": "目录里只有 a.txt"},
            ],
            "unread": True,
        },
    )
    assert response.status_code == 200

    row = client.get("/api/sessions").json()["sessions"][0]
    assert row["last_message"] == "目录里只有 a.txt"
    assert row["unread"] is True


def test_importing_the_module_does_not_build_an_app() -> None:
    """A10：`web.app` 不应有模块级 `app = create_app()`。

    import 就建 app 并读 settings 是副作用 —— `cli web` 导入 `create_app` 时
    会凭空多造一个默认 app（那个 app 永远不会被用到，却已经读了配置）。
    """
    import coding_agent.web.app as module

    assert not hasattr(module, "app"), "模块级不该有 app 对象（用 create_app 自己建）"
