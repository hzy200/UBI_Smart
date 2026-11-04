---
layer: L2
doc_id: observability
title: 可观测性：指标、日志与告警
owner: 项目作者
status: active
last_reviewed: 2026-11-04
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
| `agent_invalid_step_ratio` | gauge | — | 无效步占比（回退/失败/取消步数 / 总步数） |
| `agent_tokens_per_success` | gauge | — | 每成功任务 token |
| `agent_estimation_deviation_ratio` | gauge | — | `(actual-P50)/P50` |
| `agent_aborts_total` | counter | `phase` | 熔断次数：before_execution/execution/verification |
| `agent_budget_warnings_total` / `agent_budget_exhausted_total` | counter | `dimension` | 四维预算预警/熔断 |
| `agent_event_loop_lag_seconds` / `_events_total` | gauge / counter | — | 控制面事件循环 lag |
| `agent_process_runs_total` / `agent_process_active` | counter / gauge | `outcome` | 进程兜底任务 |
| `agent_lease_waiters_rejected_total` | counter | `pool` | 有界池等待队列满的快速失败 |
| `agent_library_search_total` / `agent_library_search_hits_total` | counter | `mode`, `outcome` / `mode`, `hit` | 检索量、失败与命中率 |
| `agent_library_search_p95_ms` | gauge | `mode` | 进程内最近 512 次检索的 p95 |
| `agent_library_ingest_total` / `agent_library_failures_total` | counter | `action`, `outcome` / `stage` | 摄取/重建完成与失败 |
| `agent_library_ingest_p95_ms` | gauge | `action` | 进程内最近 512 次摄取/重建耗时的 p95 |
| `agent_library_jobs_active` / `agent_library_jobs_pending` | gauge | — | 在飞与排队任务数 |
| `agent_library_vector_index_size` | gauge | `backend` | 当前进程内向量索引条目数 |

> 资料库的 p95 是**进程内、最近 512 次**的近似值，不是跨实例聚合；
> 多实例部署时按实例观察。

建议告警（按实例）：

- `agent_library_failures_total` 5 分钟增量 > 0；
- `agent_library_jobs_pending` 持续高于 `AGENT_LIBRARY_MAX_CONCURRENT_JOBS`；
- `agent_library_search_p95_ms{mode="vector|hybrid"}` 明显高于 lexical；
- `agent_library_search_total{outcome="error"}` 增长。

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

创新点评测 `papers/innovation` 额外输出：`candidates`、`supported_candidates`、
`evidence_docs`、`innovation_issues`。`supported` 来自 novelty critic，
不是外部新颖性证明。

### 编排控制面的结构化审计

`kind=orchestration` 记录新增字段（用于估算校准、熔断定位与回归）：

| 字段 | 含义 |
|---|---|
| `est_steps` / `est_tool_calls` / `est_tokens_p50/p90` / `est_wall_ms_p50/p90` | Estimation Card |
| `actual_steps` / `actual_tool_calls` / `actual_tokens` / `actual_wall_ms` | 实际用量 |
| `invalid_steps` / `invalid_step_ratio` | 失败/阻塞/取消的步骤与占比 |
| `state_from` / `state_to` | `RUNNING` → `DONE` / `ABORTED` |
| `checkpoint_id` / `rollback_level` | 最近控制检查点与 L2 回退 |
| `concurrency_unit` / `isolation_level` | 协程/进程档位与隔离档位 |
| `approval_ack_ms` | 审批请求到 resume 的耗时 |

跑偏回归集写在审计目录 `regression.jsonl`，`kind` 包括
`abort:<phase>`、`verify_fail`、`loop_detected`、`invalid_steps`；只记结构化
摘要与指标，不记提示词/工具原文。

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
- `agent_invalid_step_ratio` 持续 > 0.3 或 `agent_estimation_deviation_ratio` 的 P90 明显偏离 0（需要按任务类型分桶看）；
- `agent_event_loop_lag_events_total` 5 分钟增量 > 0；
- `agent_process_runs_total{outcome="timeout"}` 增量 > 0；
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
