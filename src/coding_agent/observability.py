"""进程内指标：最小注册表 + Prometheus 文本导出。

为什么自己写而不引 `prometheus-client`：一旦引进来，它就从"可观测性依赖"
变成"安全边界的一部分"（它自带解析器与 HTTP 层）。这里需要的只是计数器与
文本导出，几十行足够，且输出与 Prometheus 的 text exposition 格式兼容。

刻意**不做**：

- 直方图/分位数：需要本地 bucket 与聚合，留到真的有 SLO 再说；
- 推送网关与远端写入：指标只在本机 `/metrics` 暴露，由部署侧采集；
- 高基数标签：`thread_id`、文件路径、workspace 一律**不许**当标签值 ——
  那会把监控系统打爆，也让指标退化成日志。标签只取有界枚举。
"""

from __future__ import annotations

import json
import logging
import sys
import threading
from datetime import UTC, datetime
from typing import Any

from coding_agent.request_context import current_request_id

_LOCK = threading.Lock()
_COUNTERS: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
_GAUGES: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
_HELP: dict[str, str] = {}
_KIND: dict[str, str] = {}
_LABELS: dict[str, tuple[str, ...]] = {}

# 有界枚举：新增标签值前先确认它是有限的。
_METRIC_SPEC: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    # (kind, help, 单位, labels)
    "agent_runs_total": (
        "counter",
        "已完成的运行次数",
        "",
        ("outcome",),
    ),
    "agent_tool_calls_total": (
        "counter",
        "宿主代执行的工具调用次数",
        "",
        ("tool", "level", "decision"),
    ),
    "agent_approvals_total": (
        "counter",
        "审批决定次数",
        "",
        ("decision",),
    ),
    "agent_audit_write_failures_total": (
        "counter",
        "审计写入失败次数（审计是承重不变量，非 0 必须告警）",
        "",
        (),
    ),
    "agent_silent_failures_total": (
        "counter",
        "被 best-effort 吞掉的异常次数；按 site 分组",
        "",
        ("site",),
    ),
    "agent_sandbox_commands_total": (
        "counter",
        "沙箱命令执行次数",
        "",
        ("status",),
    ),
    "agent_llm_errors_total": (
        "counter",
        "LLM 调用失败次数",
        "",
        ("kind",),
    ),
    "agent_http_requests_total": (
        "counter",
        "HTTP 请求次数",
        "",
        ("route", "status"),
    ),
    "agent_active_runs": (
        "gauge",
        "当前在飞的运行数",
        "",
        (),
    ),
    "agent_sse_connections": (
        "gauge",
        "当前打开的 SSE 连接数",
        "",
        (),
    ),
    "agent_auth_events_total": (
        "counter",
        "认证/登录事件次数",
        "",
        ("kind",),
    ),
    "agent_approvals_pending": (
        "gauge",
        "待管理员决定的四眼审批数",
        "",
        ("tenant",),
    ),
}

for _name, (_kind, _help, _unit, _labels) in _METRIC_SPEC.items():
    _KIND[_name] = _kind
    _HELP[_name] = _help
    _LABELS[_name] = _labels
    if _kind == "counter":
        _COUNTERS.setdefault(_name, {})
    else:
        _GAUGES.setdefault(_name, {})


def _clean(value: Any) -> str:
    text = str(value)
    return (
        text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


def _key(name: str, labels: dict[str, Any] | None) -> tuple[tuple[str, str], ...]:
    spec = _LABELS.get(name)
    if not spec:
        return ()
    given = labels or {}
    return tuple((key, _clean(given.get(key, ""))) for key in spec)


def inc(name: str, labels: dict[str, Any] | None = None, value: float = 1) -> None:
    """计数器 +1；未知指标名直接抛错（拼错名字等于指标永远不出现）。"""
    if name not in _COUNTERS:
        raise KeyError(f"未登记的计数器：{name}")
    key = _key(name, labels)
    with _LOCK:
        _COUNTERS[name][key] = _COUNTERS[name].get(key, 0.0) + value


def set_gauge(name: str, value: float, labels: dict[str, Any] | None = None) -> None:
    if name not in _GAUGES:
        raise KeyError(f"未登记的 gauge：{name}")
    with _LOCK:
        _GAUGES[name][_key(name, labels)] = float(value)


def inc_gauge(
    name: str, delta: float, labels: dict[str, Any] | None = None
) -> None:
    """gauge 的增量更新（连接数/在飞任务）；未登记的 gauge 直接抛错。"""
    if name not in _GAUGES:
        raise KeyError(f"未登记的 gauge：{name}")
    key = _key(name, labels)
    with _LOCK:
        _GAUGES[name][key] = _GAUGES[name].get(key, 0.0) + float(delta)


def note_silent_failure(site: str) -> None:
    """best-effort 分支吞掉异常时调用：静默失败必须**可被计数**。"""
    inc("agent_silent_failures_total", {"site": site})


_TRACER: Any = None


def _parse_otel_headers(raw: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    for item in str(raw or "").split(","):
        name, sep, value = item.partition("=")
        if sep and name.strip():
            headers[name.strip()] = value.strip()
    return headers


def configure_tracing(settings: Any, *, exporter: Any = None) -> Any:
    """按 `AGENT_OTEL_ENDPOINT` 初始化 OTLP HTTP trace；空 = 关闭。

    返回 tracer（无 endpoint 返回 None）。未安装 `.[otel]` 时直接抛错，
    避免“配了 endpoint 但其实没采”。
    """
    global _TRACER
    endpoint = str(getattr(settings, "otel_endpoint", "") or "").strip()
    if not endpoint:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except ImportError as exc:  # pragma: no cover - 依赖缺失时启动即失败
        raise RuntimeError(
            "AGENT_OTEL_ENDPOINT 已配置，但未安装 OpenTelemetry："
            'pip install -e ".[otel]"'
        ) from exc
    if exporter is None:
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                OTLPSpanExporter,
            )
        except ImportError as exc:  # pragma: no cover - 依赖缺失
            raise RuntimeError(
                '缺少 OTLP HTTP exporter：pip install -e ".[otel]"'
            ) from exc
        exporter = OTLPSpanExporter(
            endpoint=endpoint,
            headers=_parse_otel_headers(
                getattr(settings, "otel_headers", "")
            ),
        )
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": str(
                    getattr(settings, "otel_service_name", "")
                    or "coding-agent"
                )
            }
        )
    )
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _TRACER = trace.get_tracer("coding-agent")
    return _TRACER


def tracer() -> Any:
    return _TRACER


class JsonFormatter(logging.Formatter):
    """稳定字段的 JSON 日志；不记录请求体、提示词或密钥。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = current_request_id()
        if request_id:
            payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(*, level: str = "INFO", fmt: str = "text") -> None:
    """配置根 logger；`json` 供容器/日志采集，默认保持人类可读。"""
    handler = logging.StreamHandler(sys.stderr)
    if str(fmt or "").strip().lower() == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s %(levelname)s %(name)s: %(message)s"
            )
        )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(str(level or "INFO").upper())


def render() -> str:
    """导出 Prometheus text exposition 格式。"""
    lines: list[str] = []
    with _LOCK:
        for name in sorted(_KIND):
            lines.append(f"# HELP {name} {_HELP[name]}")
            lines.append(f"# TYPE {name} {_KIND[name]}")
            table = (
                _COUNTERS.get(name) or {}
                if _KIND[name] == "counter"
                else _GAUGES.get(name) or {}
            )
            for key in sorted(table):
                if key:
                    rendered = ",".join(
                        f'{label}="{value}"' for label, value in key
                    )
                    lines.append(f"{name}{{{rendered}}} {table[key]}")
                else:
                    lines.append(f"{name} {table[key]}")
    return "\n".join(lines) + "\n"


def snapshot() -> dict[str, float]:
    """给测试与 `/healthz` 用的扁平视图（去掉标签维度）。"""
    flat: dict[str, float] = {}
    with _LOCK:
        for name, table in _COUNTERS.items():
            flat[name] = sum(table.values())
        for name, table in _GAUGES.items():
            flat[name] = sum(table.values())
    return flat
