"""可观测性：指标注册表与 `/metrics` `/readyz` 端点。

`/healthz` 只说明"进程活着"。依赖（执行后端、控制面库）挂了而进程还在，
是**最容易被反代/编排继续放进流量**的状态 —— `/readyz` 就是为它存在的。
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from coding_agent import web
from coding_agent.platform import observability
from coding_agent.platform.request_context import reset_request_id, set_request_id


def test_counter_increments_and_renders() -> None:
    observability.inc("agent_runs_total", {"outcome": "ok"}, 2)
    observability.inc("agent_runs_total", {"outcome": "ok"})
    text = observability.render()
    assert "# TYPE agent_runs_total counter" in text
    assert 'agent_runs_total{outcome="ok"} 3' in text


def test_unknown_metric_is_rejected() -> None:
    """拼错指标名等于指标永远不出现：必须立刻炸，不能静默丢弃。"""
    with pytest.raises(KeyError):
        observability.inc("agent_runs_totl")
    with pytest.raises(KeyError):
        observability.set_gauge("agent_active_run", 1)


def test_silent_failures_are_counted() -> None:
    """best-effort 吞掉异常可以，不留痕不行。"""
    before = observability.snapshot().get("agent_silent_failures_total", 0)
    observability.note_silent_failure("quota_settle")
    after = observability.snapshot().get("agent_silent_failures_total", 0)
    assert after == before + 1
    assert 'site="quota_settle"' in observability.render()


def test_label_values_are_escaped() -> None:
    """标签值来自运行时（异常类型名之类），引号与换行必须转义。"""
    observability.inc("agent_silent_failures_total", {"site": 'a"b\nc'})
    text = observability.render()
    assert 'site="a\\"b\\nc"' in text


def test_gauge_is_settable() -> None:
    observability.set_gauge("agent_active_runs", 3)
    assert observability.snapshot()["agent_active_runs"] == 3


def test_a2_exposes_metrics(settings) -> None:
    response = TestClient(web.create_app(settings)).get("/metrics")
    assert response.status_code == 200
    assert "agent_silent_failures_total" in response.text


def test_a2_readyz_is_200_when_nothing_is_broken(settings) -> None:
    app = web.create_app(settings)
    data = TestClient(app).get("/readyz").json()
    assert data["status"] == "ready"
    assert data["checks"]["control_db"] == "disabled"


def test_a2_readyz_is_503_when_agent_is_unavailable(settings) -> None:
    """A3 不可用时 A2 必须从流量里摘出去，而不是继续接客。"""
    app = web.create_app(settings)
    app.state.agent_status = {"state": "unavailable"}
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_a2_readyz_is_503_when_control_db_is_unreachable(settings) -> None:
    app = web.create_app(settings)

    class _BrokenStore:
        class engine:  # noqa: N801 - 故意模仿 sqlalchemy Engine 的接口形状
            @staticmethod
            def connect():  # noqa: ANN205
                raise OSError("database is locked")

    app.state.control_store = _BrokenStore()
    response = TestClient(app).get("/readyz")
    assert response.status_code == 503
    assert "unreachable" in response.json()["checks"]["control_db"]


def test_json_formatter_has_stable_fields_and_request_id() -> None:
    token = set_request_id("req-123")
    try:
        record = logging.LogRecord(
            "coding_agent.test",
            logging.WARNING,
            __file__,
            1,
            "hello %s",
            ("world",),
            None,
        )
        payload = json.loads(observability.JsonFormatter().format(record))
    finally:
        reset_request_id(token)
    assert payload["level"] == "WARNING"
    assert payload["message"] == "hello world"
    assert payload["request_id"] == "req-123"
    assert "ts" in payload


def test_gauge_can_be_incremented() -> None:
    before = observability.snapshot().get("agent_sse_connections", 0)
    observability.inc_gauge("agent_sse_connections", 1)
    observability.inc_gauge("agent_sse_connections", -1)
    after = observability.snapshot().get("agent_sse_connections", 0)
    assert after == before


def test_a2_http_requests_are_counted(settings) -> None:
    client = TestClient(web.create_app(settings))
    assert client.get("/healthz").status_code == 200
    text = client.get("/metrics").text
    assert "agent_http_requests_total" in text
    assert 'route="/healthz"' in text
    assert 'status="2xx"' in text


def test_a2_metrics_accepts_dedicated_token(settings) -> None:
    settings = settings.model_copy(
        update={"metrics_token": "metrics-secret"}
    )
    client = TestClient(web.create_app(settings))
    assert client.get("/metrics").status_code == 401
    ok = client.get(
        "/metrics",
        headers={"Authorization": "Bearer metrics-secret"},
    )
    assert ok.status_code == 200
    assert "agent_http_requests_total" in ok.text


def test_a3_metrics_requires_internal_token(tmp_path) -> None:
    """指标含运行量与安全事件计数：不能匿名可读。"""
    from coding_agent import agent_server
    from coding_agent.config import Settings

    settings = Settings(
        _env_file=None,
        wsl_workspace="/tmp/ws",
        web_data_root=str(tmp_path / "host-data"),
    )
    app = agent_server.create_agent_app(settings, token="tok")
    client = TestClient(app)
    assert client.get("/metrics").status_code == 401
    ok = client.get("/metrics", headers={"Authorization": "Bearer tok"})
    assert ok.status_code == 200
    assert "agent_silent_failures_total" in ok.text


class _Span:
    def __init__(self, name: str) -> None:
        self.name = name
        self.attributes: dict[str, object] = {}
        self.status = None

    def set_attribute(self, name: str, value: object) -> None:
        self.attributes[name] = value

    def set_status(self, status: object) -> None:
        self.status = status


class _Tracer:
    def __init__(self) -> None:
        self.spans: list[_Span] = []

    def start_as_current_span(self, name: str):
        span = _Span(name)
        self.spans.append(span)
        tracer = self

        class _Ctx:
            def __enter__(self):
                return span

            def __exit__(self, *args) -> bool:
                return False

        del tracer
        return _Ctx()


def test_tracing_is_off_without_endpoint() -> None:
    from coding_agent.config import Settings

    assert observability.configure_tracing(Settings(_env_file=None)) is None


def test_tracing_middleware_records_route_and_status() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from coding_agent.platform.tracing import TracingMiddleware

    tracer = _Tracer()
    app = FastAPI()
    app.add_middleware(TracingMiddleware, tracer=tracer)

    @app.get("/items/{item_id}")
    async def item(item_id: str) -> dict:
        return {"id": item_id}

    @app.get("/boom")
    async def boom() -> dict:
        raise RuntimeError("boom")

    client = TestClient(app, raise_server_exceptions=False)
    assert client.get("/items/1").status_code == 200
    assert client.get("/boom").status_code == 500
    assert [span.name for span in tracer.spans] == [
        "GET /items/1",
        "GET /boom",
    ]
    assert tracer.spans[0].attributes["http.route"] == "/items/{item_id}"
    assert tracer.spans[0].attributes["http.status_code"] == 200
    assert tracer.spans[1].attributes["http.status_code"] == 500
    assert tracer.spans[1].status is not None


def test_tracing_headers_parsing() -> None:
    assert observability._parse_otel_headers(
        "Authorization=Bearer x, x-team=a"
    ) == {"Authorization": "Bearer x", "x-team": "a"}
    assert observability._parse_otel_headers("bad") == {}


def test_configure_tracing_with_memory_exporter() -> None:
    pytest.importorskip("opentelemetry.sdk")
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
        InMemorySpanExporter,
    )

    from coding_agent.config import Settings

    memory = InMemorySpanExporter()
    tracer = observability.configure_tracing(
        Settings(
            _env_file=None,
            otel_endpoint="http://collector:4318/v1/traces",
            otel_service_name="coding-agent-test",
        ),
        exporter=memory,
    )
    assert tracer is not None
    assert observability.tracer() is tracer
