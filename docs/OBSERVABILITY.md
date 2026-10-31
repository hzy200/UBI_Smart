---
layer: L2
doc_id: observability
title: 可观测性：指标、日志与告警
owner: 项目作者
status: active
last_reviewed: 2026-10-31
review_cadence: 指标/日志/告警或抓取方式变动时
---

# 可观测性

## 1. 指标

A2 与 A3 都暴露 Prometheus text exposition：

| 服务 | 端点 | 认证 |
|---|---|---|
| A2（control_server / web） | `GET /metrics` | `AGENT_METRICS_TOKEN` Bearer；空则复用普通 Web 认证（Cookie/Bearer/loopback） |
| A3（agent_server） | `GET /metrics` | `AGENT_INTERNAL_TOKEN` Bearer；非 loopback 仍需内部 HMAC |

指标定义在 `src/ubismart/platform/observability.py`。标签只取有界枚举，
`thread_id` / 文件路径 / workspace 不允许作为标签值。

常用指标：

| 指标 | 类型 | 标签 | 用途 |
|---|---|---|---|
| `agent_http_requests_total` | counter | `route`, `status` | A2 请求量/错误率（状态类是 `2xx/4xx/5xx`） |
| `agent_active_runs` | gauge | — | 当前在飞运行数 |
| `agent_sse_connections` | gauge | — | 当前打开的 SSE 连接数 |
| `agent_auth_events_total` | counter | `kind` | 登录/MFA/OIDC 事件 |
| `agent_approvals_total` | counter | `decision` | 四眼审批决定 |
| `agent_approvals_pending` | gauge | `tenant` | 待管理员决定的审批数（抓取时刷新） |
| `agent_csp_reports_total` | counter | `disposition` | 浏览器 CSP 违规上报（enforce/report） |
| `agent_silent_failures_total` | counter | `site` | best-effort 吞掉的异常 |
| `agent_audit_write_failures_total` | counter | — | 审计写入失败（承重不变量） |
| `agent_runs_total` / `agent_tool_calls_total` | counter | 有界枚举 | A3 运行/工具调用 |
| `agent_sandbox_commands_total` / `agent_llm_errors_total` | counter | `status` / `kind` | 执行与模型错误 |

### 文档 worker 的结构化字段

不出 Prometheus 指标，但随 `WorkerResult` 写入事件/审计与评测 JSON：

| 字段 | 含义 |
|---|---|
| `coverage` | 每篇文档的 pages/read_ranges/unread/status |
| `sources[].quote_verified` / `quote_span` | quote 是否在原文定位、页码与字符跨度 |
| `sources[].anchor_valid` | 锚点是否属于文档真实锚点集合 |
| `matrix` | 每篇固定槽位（问题/方法/算法/实验/局限/创新/结论） |
| `attribution_issues` | 提到 A 却引用 B 的句子级错配 |
| `verification.matrix_entries` / `attribution_issues` | 上述两类的计数摘要 |

评测侧另输出 `quote_verified_ratio`、`matrix_slot_ratio`、`attribution_issues`
与 `report_chars`，用于回归对比；这些是**过程指标**，不等于语义正确率。

## 2. 日志

服务入口支持 `AGENT_LOG_FORMAT=text|json`（`--log-level` 控制级别）：

- `text`：开发默认，人类可读；
- `json`：容器/日志采集，固定字段 `ts/level/logger/message`，有 `request_id`
  时附加该字段；异常附加 `exception`。
- JSON 日志不记录请求体、提示词、文件内容或密钥；脱敏仍由审计/工具输出层负责。

请求日志由 `RequestIdMiddleware` 输出，包含 method/path/status/request_id/耗时。
建议采集侧按 `request_id` 关联 A2→A3。

## 3. 告警

基线规则在 [`packaging/prometheus/ubismart-alerts.yml`](../packaging/prometheus/ubismart-alerts.yml)：

- 审计写入失败（critical，任何一次都告警）；
- A2 5xx > 5%（10 分钟）；
- 四眼审批积压 > 10 条（15 分钟）；
- 静默失败 10 分钟 > 20 次；
- 在飞运行 > 20（30 分钟）或 SSE 连接 > 100（10 分钟）。

抓取与告警部署不在本仓库内；按企业实际 Prometheus/Alertmanager 配置加载该文件。
`/metrics` 不要暴露到公网；A2 建议只在内网/抓取网络监听，A3 只绑 loopback。

## 4. 已知边界

- 指标是进程内的，A2/A3 多实例各自暴露；聚合由 Prometheus 负责。
- 没有直方图/分位数；SLO 需要 p95 时先用请求日志/外部 APM 补。
- 审批待办 gauge 在抓取时查询控制面库，库不可达时保留上一次值。
- 审计 JSONL 仍是本地哈希链；SIEM 转发/外部锚定不在本轮范围。
