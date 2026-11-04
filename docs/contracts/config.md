<!-- 由 scripts/gen_contracts.py 从源码生成，禁止手改。改代码后重跑生成，CI 会校验一致性。 -->

# 配置契约

事实源：`src/ubismart/config.py`。**这份是全量**，README 的配置表只是摘录。
机器可读版本（可直接用来校验有没有捏造配置项）：[`config.json`](config.json)。

优先级：构造函数参数 **>** .env **>** 环境变量 **>** 默认值。

> 刻意偏离「环境变量 > .env」的通用约定：某些工具链会在进程启动时注入用户看不见也删不掉的陈旧值，导致改 .env 不生效。项目目录里的 .env 是显式写下、看得见、可编辑的意图，因此让它优先。

共 **449** 个配置项。

## 通用

| 环境变量 | 字段 | 类型 | 默认值 | 说明 |
|---|---|---|---|---|
| `AGENT_AGENT_DATA_ROOT` | `agent_data_root` | `str` | `""` | Agent Server 的租户数据根；空 = 审计目录旁的 agent/。 |
| `AGENT_ALLOW_GIT_CLONE` | `allow_git_clone` | `bool` | `false` | 本期不实现 git clone；保留开关，未来接入内部 Git 服务时打开。 |
| `AGENT_ALLOW_RULES` | `allow_rules` | `str` | `""` | 持久放行规则，逗号分隔的 `tool:glob`，例如 `shell_exec:git status*,run_tests:*`。只覆盖原本会 ASK 的 L2。 |
| `AGENT_APPROVAL_ACK_TIMEOUT_S` | `approval_ack_timeout_s` | `int` | `300` | 审批卡：超时默认动作（pause / allow / deny）。当前写入卡片契约； 自动 resume 由前端/控制面按下发字段执行。 |
| `AGENT_APPROVAL_DEFAULT_ACTION` | `approval_default_action` | `str` | `"pause"` |  |
| `AGENT_APPROVAL_FOUR_EYES` | `approval_four_eyes` | `bool` | `false` | 四眼审批：L3 默认；0 = 关闭（但仍可用独立开关关闭整个特性）。 |
| `AGENT_APPROVAL_FOUR_EYES_MIN_LEVEL` | `approval_four_eyes_min_level` | `int` | `3` |  |
| `AGENT_APPROVAL_MODE` | `approval_mode` | `str` | `"ask"` | ask（逐条询问）\| approve（自动放行 L2；L3 仍需确认）\| deny（一律拒绝） |
| `AGENT_APPROVAL_TTL_S` | `approval_ttl_s` | `int` | `3600` |  |
| `AGENT_ARCH_IMPACT_MAX_MODULES` | `arch_impact_max_modules` | `int` | `40` |  |
| `AGENT_ARCH_INCREMENTAL_INDEX` | `arch_incremental_index` | `bool` | `true` |  |
| `AGENT_ARCH_INDEX_CACHE_DIR` | `arch_index_cache_dir` | `str` | `""` | 空 = 宿主 ~/.codingagent/index-cache；可指向共享盘实现跨机器复用。 |
| `AGENT_ARCH_INDEX_CACHE_LOCK_STALE_S` | `arch_index_cache_lock_stale_s` | `float` | `60.0` |  |
| `AGENT_ARCH_INDEX_CACHE_LOCK_TIMEOUT_S` | `arch_index_cache_lock_timeout_s` | `float` | `5.0` | 共享缓存跨进程锁：等待上限与 stale 判定（秒）。 |
| `AGENT_ARCH_INDEX_CACHE_MAX_BYTES` | `arch_index_cache_max_bytes` | `int` | `536870912` |  |
| `AGENT_ARCH_INDEX_CACHE_MAX_ENTRIES` | `arch_index_cache_max_entries` | `int` | `20000` |  |
| `AGENT_ARCH_INDEX_CACHE_TTL_DAYS` | `arch_index_cache_ttl_days` | `int` | `30` |  |
| `AGENT_ARCH_LSP_MAX_CALLS` | `arch_lsp_max_calls` | `int` | `50` |  |
| `AGENT_ARCH_LSP_RESOLUTION_ENABLED` | `arch_lsp_resolution_enabled` | `bool` | `false` | 可选：用 LSP definition 解析动态调用（需要 AGENT_LSP_COMMAND）。 |
| `AGENT_ARCH_MAX_FILES` | `arch_max_files` | `int` | `2000` | 架构索引抽样上限与增量缓存。 |
| `AGENT_ARCH_MAX_FILE_BYTES` | `arch_max_file_bytes` | `int` | `1000000` |  |
| `AGENT_ARCH_MAX_TOTAL_BYTES` | `arch_max_total_bytes` | `int` | `32000000` |  |
| `AGENT_ARCH_WORKFLOW_ENABLED` | `arch_workflow_enabled` | `bool` | `false` | 代码架构：从 PythonIndex 生成 ArchIndex/ArchBrief 并注入规划提示。 |
| `AGENT_AUDIT_ASYNC_WRITE` | `audit_async_write` | `bool` | `true` | 控制面不阻塞：审计走有界队列 + 专用写线程；队列满或异步写失败都会 显式抛出 AuditError，绝不静默丢记录。同步模式保留给 CLI/单测。 |
| `AGENT_AUDIT_DIR` | `audit_dir` | `str` | `""` | 留空表示自动用 <当前目录>/.agent/audit |
| `AGENT_AUDIT_ENABLED` | `audit_enabled` | `bool` | `true` |  |
| `AGENT_AUDIT_MAX_MB` | `audit_max_mb` | `int` | `256` | 单个审计文件超过该大小（MB）就轮转；0 表示不轮转 |
| `AGENT_AUDIT_QUEUE_SIZE` | `audit_queue_size` | `int` | `10000` |  |
| `AGENT_AUDIT_SYSLOG_ADDRESS` | `audit_syslog_address` | `str` | `""` | SIEM 导出：host:port（UDP syslog），每条审计 JSON 一行；空 = 不导出。 |
| `AGENT_AUDIT_WEBHOOK_TOKEN` | `audit_webhook_token` | `str` | `""` |  |
| `AGENT_AUDIT_WEBHOOK_URL` | `audit_webhook_url` | `str` | `""` | SIEM webhook：POST 每行审计 JSON；空 = 不导出。 |
| `AGENT_AUTHOR_EVIDENCE_MAX_CHARS` | `author_evidence_max_chars` | `int` | `24000` |  |
| `AGENT_AUTHOR_EVIDENCE_TOP_K` | `author_evidence_top_k` | `int` | `12` | author 的证据上下文：优先取 claim + quote，而不是只取 summary。 |
| `AGENT_BILLING_ENABLED` | `billing_enabled` | `bool` | `false` | 是否在 run 结束时拉取 provider 账单做对账；默认关闭避免额外网络请求。 |
| `AGENT_BILLING_TIMEOUT_S` | `billing_timeout_s` | `float` | `10.0` |  |
| `AGENT_CARD_MAX_SILENCE_S` | `card_max_silence_s` | `float` | `120.0` | 通知面：首卡 T+0、最长静默 heartbeat、进度卡最小间隔。 |
| `AGENT_CARD_MIN_INTERVAL_S` | `card_min_interval_s` | `float` | `10.0` |  |
| `AGENT_CHECKPOINT_KEEP` | `checkpoint_keep` | `int` | `200` | 每个 thread/命名空间保留的 checkpoint 数；0 = 不轮转。 每轮工具调用都会落一个 checkpoint，长期使用需要上限。 |
| `AGENT_CHECKPOINT_PATH` | `checkpoint_path` | `str` | `""` | ---------- 持久化与审计 ---------- 留空表示自动用 <当前目录>/.agent/checkpoints.sqlite；填 :memory: 强制不落盘 |
| `AGENT_CONCURRENCY_LLM_GLOBAL` | `concurrency_llm_global` | `int` | `6` |  |
| `AGENT_CONCURRENCY_LLM_PER_PROVIDER` | `concurrency_llm_per_provider` | `int` | `4` |  |
| `AGENT_CONCURRENCY_LLM_TIMEOUT_S` | `concurrency_llm_timeout_s` | `float` | `30.0` |  |
| `AGENT_CONCURRENCY_MAX_WAITERS` | `concurrency_max_waiters` | `int` | `128` | 异步池等待队列上限；0 = 不限制（仅测试/兼容用）。生产默认有界， 队列满时快速失败，而不是把等待者无限堆进内存。 |
| `AGENT_CONCURRENCY_TOOLS` | `concurrency_tools` | `int` | `0` |  |
| `AGENT_CONCURRENCY_WORKERS` | `concurrency_workers` | `int` | `0` | ---------- 统一并发治理 ---------- 0 = 回退到旧的单项配置（orchestrator_max_concurrent_workers / sandbox_max_concurrent_commands / max_concurrent_tools）。 |
| `AGENT_CONCURRENCY_WORKER_TIMEOUT_S` | `concurrency_worker_timeout_s` | `float` | `60.0` |  |
| `AGENT_CONCURRENCY_WSL` | `concurrency_wsl` | `int` | `0` |  |
| `AGENT_CONCURRENCY_WSL_ADAPTIVE` | `concurrency_wsl_adaptive` | `bool` | `true` | 基于命令 p95 延迟的弹性并发：高延迟降并发，低延迟逐步恢复。 |
| `AGENT_CONCURRENCY_WSL_LATENCY_WINDOW` | `concurrency_wsl_latency_window` | `int` | `20` |  |
| `AGENT_CONCURRENCY_WSL_MAX` | `concurrency_wsl_max` | `int` | `0` |  |
| `AGENT_CONCURRENCY_WSL_MIN` | `concurrency_wsl_min` | `int` | `2` |  |
| `AGENT_CONCURRENCY_WSL_RESERVE` | `concurrency_wsl_reserve` | `int` | `1` | 给健康探针/短探测保留的 WSL 槽位；普通命令不能用。 |
| `AGENT_CONCURRENCY_WSL_TARGET_MS` | `concurrency_wsl_target_ms` | `int` | `1500` |  |
| `AGENT_CONCURRENCY_WSL_TIMEOUT_S` | `concurrency_wsl_timeout_s` | `float` | `30.0` |  |
| `AGENT_CONSENT_TOKEN` | `consent_bridge_token` | `str` | `""` | 桌面端本机授权桥：A2L 把 grant.request 发到 A2，由聊天界面按钮决定。 |
| `AGENT_CONTEXT_KEEP_RECENT` | `context_keep_recent` | `int` | `12` |  |
| `AGENT_CONTEXT_MAX_CHARS` | `context_max_chars` | `int` | `60000` | ---------- 上下文裁剪 ---------- 0 表示不裁剪；这些值作用于发给模型的历史，不改写会话状态 |
| `AGENT_CONTEXT_MAX_TOKENS` | `context_max_tokens` | `int` | `0` |  |
| `AGENT_CONTEXT_TOOL_CHARS` | `context_tool_chars` | `int` | `1500` |  |
| `AGENT_CONTROL_DB_AUTO_CREATE` | `control_db_auto_create` | `bool` | `true` | SQLite/开发环境自动建表；MySQL 生产迁移用 alembic upgrade 或 control_server --migrate（迁移前自动备份）。 |
| `AGENT_CONTROL_DB_URL` | `control_db_url` | `str` | `""` | ---------- A2 控制面（Phase B，可选） ---------- 空 = 继续用 gui-sessions.json / 本地文件（Standalone 与旧部署）。 例：mysql+pymysql://user:pass@127.0.0.1/coding_agent |
| `AGENT_CORS_ALLOWED_ORIGINS` | `web_cors_allowed_origins` | `str` | `""` | 独立源站前端允许的精确 origin（逗号分隔）；空 = 不启用 CORS。 只允许 scheme://host[:port]，不接受路径、通配符或 null。 |
| `AGENT_COST_ALERT_RATIO` | `cost_alert_ratio` | `float` | `0.8` | 达到预算的该比例时发出告警（0~1） |
| `AGENT_COST_CURRENCY` | `cost_currency` | `str` | `"CNY"` | 成本展示与额度计量货币；上游价格仍按 USD 计价。 |
| `AGENT_DEEP_MAX_WRITE_STAGES` | `deep_max_write_stages` | `int` | `4` | deep 模式下允许的写波次上限（仍受总预算/截止时间约束）。 |
| `AGENT_DEEP_WORKFLOWS_ENABLED` | `deep_workflows_enabled` | `bool` | `false` | ---------- 深层工作流（RFC-0009，默认关闭）---------- 总开关：关闭时下列能力全部不生效，行为与既有四模板一致。 |
| `AGENT_DELEGATE_LARGE_CALLS` | `delegate_large_calls` | `int` | `4` |  |
| `AGENT_DELEGATE_LARGE_FILES` | `delegate_large_files` | `int` | `300` |  |
| `AGENT_DELEGATE_MEDIUM_CALLS` | `delegate_medium_calls` | `int` | `2` |  |
| `AGENT_DELEGATE_MEDIUM_FILES` | `delegate_medium_files` | `int` | `100` |  |
| `AGENT_DELEGATE_MIN_FILES` | `delegate_min_files` | `int` | `30` | ---- 成本感知的委派预算（按仓库文件数分级）---- 小于 min_files：不委派，直接读；[min, medium)：medium_calls； [medium, large)：large_calls；>= large：max_delegate_calls。 |
| `AGENT_DEPS_DETECT_TTL_S` | `deps_detect_ttl_s` | `float` | `30.0` | 包管理器探测缓存时间；安装后会主动失效。 |
| `AGENT_DOCUMENT_CACHE` | `document_cache_enabled` | `bool` | `true` |  |
| `AGENT_DOCUMENT_CACHE_MAX_BYTES` | `document_cache_max_bytes` | `int` | `"512 * 1024 * 1024"` |  |
| `AGENT_DOCUMENT_CACHE_TTL_DAYS` | `document_cache_ttl_days` | `int` | `30` |  |
| `AGENT_DOCUMENT_CHUNK_CHARS` | `document_chunk_chars` | `int` | `6000` |  |
| `AGENT_DOCUMENT_EDIT_MAX_OPS` | `document_edit_max_ops` | `int` | `50` | document_edit 单次调用最多执行多少条操作。 |
| `AGENT_DOCUMENT_FULL_COVERAGE` | `document_full_coverage` | `bool` | `false` | 长文档：取消固定 3 次读取；覆盖范围由页数 + document_max_pages 决定， 未读完时报告 partial + 未读区间。 |
| `AGENT_DOCUMENT_IMAGE_MAX_ITEMS` | `document_image_max_items` | `int` | `5` |  |
| `AGENT_DOCUMENT_JUDGE_PASS_SCORE` | `document_judge_pass_score` | `int` | `60` | 文档 judge 的通过阈值；可用 scripts/calibrate_judge.py 在标注集上调。 |
| `AGENT_DOCUMENT_MAP_CONCURRENCY` | `document_map_concurrency` | `int` | `3` |  |
| `AGENT_DOCUMENT_MAP_REDUCE` | `document_map_reduce` | `bool` | `false` | 长文档：按 chunk 分批抽取 claim，再合并；避免整篇塞入单次调用。 |
| `AGENT_DOCUMENT_MATRIX_ENABLED` | `document_matrix_enabled` | `bool` | `false` | 每篇文档提炼固定槽位（问题/方法/算法/实验/局限/创新/结论）， 供跨文档矩阵与 author 使用；默认关闭（每篇多一次结构化调用）， 长文档/报告场景显式开启。 |
| `AGENT_DOCUMENT_MAX_BYTES` | `document_max_bytes` | `int` | `20971520` | 文档解析上限。解析在宿主进程完成，不进入 WSL 沙箱。 |
| `AGENT_DOCUMENT_MAX_CELLS` | `document_max_cells` | `int` | `20000` |  |
| `AGENT_DOCUMENT_MAX_CHARS` | `document_max_chars` | `int` | `2000000` |  |
| `AGENT_DOCUMENT_MAX_PAGES` | `document_max_pages` | `int` | `200` |  |
| `AGENT_DOCUMENT_OCR_ENABLED` | `document_ocr_enabled` | `bool` | `false` |  |
| `AGENT_DOCUMENT_OCR_MAX_PAGES` | `document_ocr_max_pages` | `int` | `5` | 扫描页交给视觉模型的上限。 |
| `AGENT_DOCUMENT_PIPELINE_ENABLED` | `document_pipeline_enabled` | `bool` | `true` | 回滚开关：关闭后 document_pipeline 不再被自动选择。 |
| `AGENT_DOCUMENT_PIPELINE_MAX_DOCS` | `orchestrator_document_max_docs` | `int` | `10` | 单次文档管线的输入上限。 |
| `AGENT_DOCUMENT_PIPELINE_MAX_IMAGES` | `orchestrator_document_max_images` | `int` | `20` |  |
| `AGENT_DOCUMENT_PROCESS_ISOLATION` | `document_process_isolation` | `bool` | `true` | CPU 密集/不可取消的文档解析（PDF/Office/EPUB）默认放进 spawn 子进程； 超时直接杀进程，避免把宿主线程永久挂住。短文本仍走当前线程。 |
| `AGENT_DOCUMENT_PROCESS_THRESHOLD_BYTES` | `document_process_threshold_bytes` | `int` | `2000000` | 超过该大小的任意文档也走进程隔离；0 = 只按格式判断。 |
| `AGENT_DOCUMENT_PROCESS_TIMEOUT_S` | `document_process_timeout_s` | `float` | `60.0` |  |
| `AGENT_DOCUMENT_READ_MAX_CALLS` | `document_read_max_calls` | `int` | `3` |  |
| `AGENT_DOCUMENT_READ_PAGES_PER_CALL` | `document_read_pages_per_call` | `int` | `5` | 长文档分段读取：每次最多多少页、最多几段。 |
| `AGENT_DOCUMENT_TREE_FANOUT` | `document_tree_fanout` | `int` | `5` | 分层归并：每层最多合并多少节点，以及单次归并的输入字符上限。 |
| `AGENT_DOCUMENT_TREE_INPUT_CHARS` | `document_tree_input_chars` | `int` | `16000` |  |
| `AGENT_DRAIN_TIMEOUT_S` | `drain_timeout_s` | `int` | `60` | SIGTERM 优雅退出：A3 停止接收新运行并等待进行中的运行自然结束； 超时后取消，A2 用同一数值约束 uvicorn 在途请求。0 = 不等待。 |
| `AGENT_EVENT_LOOP_LAG_INTERVAL_S` | `event_loop_lag_interval_s` | `float` | `1.0` |  |
| `AGENT_EVENT_LOOP_LAG_THRESHOLD_MS` | `event_loop_lag_threshold_ms` | `int` | `500` | 事件循环 lag watchdog：超过阈值记指标，不阻断运行。 |
| `AGENT_EVENT_TRANSPORT` | `event_transport` | `str` | `"memory"` | ---------- 共享事件传输（Phase B3） ---------- memory = 单实例内存；redis = Redis Streams（多实例 SSE 重放）。 |
| `AGENT_EVENT_TRANSPORT_MAXLEN` | `event_transport_maxlen` | `int` | `10000` |  |
| `AGENT_EVENT_TRANSPORT_PREFIX` | `event_transport_prefix` | `str` | `"coding-agent:events"` |  |
| `AGENT_EVENT_TRANSPORT_TTL_S` | `event_transport_ttl_s` | `int` | `3600` |  |
| `AGENT_EVENT_TRANSPORT_URL` | `event_transport_url` | `str` | `""` |  |
| `AGENT_EXECUTION_BACKEND` | `execution_backend` | `str` | `"auto"` | auto：Windows 用 wsl.exe，Linux/WSL 内直接本地执行 local：强制本地 Linux 执行；wsl：强制 wsl.exe（兼容旧 Host 模式） |
| `AGENT_EXPLORE_ENABLED` | `explore_enabled` | `bool` | `false` | ---------- 子智能体（Phase 1：只读 Explore）---------- **默认关闭**：它会给主模型多一个工具、改变 agent 行为，打开后旧评测基线 不再可比。评测时显式设置 AGENT_EXPLORE_ENABLED=true 做 A/B。 |
| `AGENT_EXPLORE_MAX_REPORT_CHARS` | `explore_max_report_chars` | `int` | `4000` | 返回给主模型的摘要上限（字符） |
| `AGENT_EXPLORE_MAX_ROUNDS` | `explore_max_rounds` | `int` | `8` | 子智能体最多几轮「模型 → 工具」；到顶后强制生成报告，状态记 partial。 定位一个跨文件功能通常需要 6–10 轮只读检索；6 轮实测会频繁 partial。 |
| `AGENT_EXPLORE_TIMEOUT` | `explore_timeout` | `int` | `120` | 整个子智能体的墙钟上限（秒） |
| `AGENT_GRANTS_ENABLED` | `grants_enabled` | `bool` | `false` |  |
| `AGENT_HOST_A2L_ID` | `host_a2l_id` | `str` | `"a2l-local"` |  |
| `AGENT_HOST_BROKER_ACCOUNT` | `host_broker_account` | `str` | `""` |  |
| `AGENT_HOST_BROKER_ALLOW_WRITE` | `host_broker_allow_write` | `bool` | `false` | P1/P2：host 写与受控 exec 默认关闭；安装永远只返回人工指令。 |
| `AGENT_HOST_BROKER_ATTESTATION` | `host_broker_attestation` | `str` | `"off"` |  |
| `AGENT_HOST_BROKER_ATTESTATION_KEY` | `host_broker_attestation_key` | `str` | `""` |  |
| `AGENT_HOST_BROKER_AUTOSTART` | `host_broker_autostart` | `bool` | `true` |  |
| `AGENT_HOST_BROKER_DEVICE_CERT` | `host_broker_device_cert` | `str` | `""` |  |
| `AGENT_HOST_BROKER_DEVICE_KEY` | `host_broker_device_key` | `str` | `""` |  |
| `AGENT_HOST_BROKER_DEVICE_POLICY` | `host_broker_device_policy` | `str` | `"off"` |  |
| `AGENT_HOST_BROKER_DEVICE_REGISTRY` | `host_broker_device_registry` | `str` | `""` | 企业设备台账（A3 本地 JSON）；off = 兼容旧的单设备/自洽校验。 |
| `AGENT_HOST_BROKER_DEVICE_RELOAD_S` | `host_broker_device_reload_s` | `int` | `2` |  |
| `AGENT_HOST_BROKER_ENABLED` | `host_broker_enabled` | `bool` | `false` | ---------- A2L 宿主能力代理（Phase 1，默认关闭） ---------- A2L 绑定客户端生命周期：随客户端启动/退出，不注册系统服务。 |
| `AGENT_HOST_BROKER_EXEC_ENABLED` | `host_broker_exec_enabled` | `bool` | `false` |  |
| `AGENT_HOST_BROKER_HEARTBEAT_S` | `host_broker_heartbeat_s` | `int` | `15` |  |
| `AGENT_HOST_BROKER_LISTEN_HOST` | `host_broker_listen_host` | `str` | `"127.0.0.1"` |  |
| `AGENT_HOST_BROKER_LISTEN_PORT` | `host_broker_listen_port` | `int` | `8791` |  |
| `AGENT_HOST_BROKER_PAUSED` | `host_broker_paused` | `bool` | `false` |  |
| `AGENT_HOST_BROKER_POLICY_FILE` | `host_broker_policy_file` | `str` | `""` | A2L 本地验签的运行策略。 |
| `AGENT_HOST_BROKER_POLICY_PUBKEY` | `host_broker_policy_pubkey` | `str` | `""` |  |
| `AGENT_HOST_BROKER_POLICY_PUBKEY_FILE` | `host_broker_policy_pubkey_file` | `str` | `""` |  |
| `AGENT_HOST_BROKER_REQUIRE_TLS` | `host_broker_require_tls` | `bool` | `false` |  |
| `AGENT_HOST_BROKER_SECURITY_TIER` | `host_broker_security_tier` | `str` | `"lowpriv"` | lowpriv 是 P1 默认；appcontainer 仅受限只读；user-t1 仅 local-dev。 |
| `AGENT_HOST_BROKER_STORE` | `host_broker_store` | `str` | `""` |  |
| `AGENT_HOST_BROKER_TLS_CA` | `host_broker_tls_ca` | `str` | `""` |  |
| `AGENT_HOST_BROKER_TLS_CERT` | `host_broker_tls_cert` | `str` | `""` |  |
| `AGENT_HOST_BROKER_TLS_KEY` | `host_broker_tls_key` | `str` | `""` |  |
| `AGENT_HOST_BROKER_TLS_SERVER_NAME` | `host_broker_tls_server_name` | `str` | `""` |  |
| `AGENT_HOST_BROKER_TOOLCHAINS` | `host_broker_toolchains` | `str` | `""` | JSON 数组；每项 {id,kind,root,commands,output_roots}。root 不下发模型。 |
| `AGENT_HOST_BROKER_URL` | `host_broker_url` | `str` | `""` |  |
| `AGENT_HOST_DENY_ROOTS` | `host_deny_roots` | `str` | `""` |  |
| `AGENT_HOST_ROOT_HANDLE` | `host_root_handle` | `str` | `""` |  |
| `AGENT_HOST_ROOT_POLICY` | `host_root_policy` | `str` | `"local_ntfs_only"` |  |
| `AGENT_HOST_SENSITIVE_ROOTS` | `host_sensitive_roots` | `str` | `""` |  |
| `AGENT_HOST_SNAPSHOT_DIR` | `host_snapshot_dir` | `str` | `""` |  |
| `AGENT_HOST_TOOLCHAIN_ROOTS` | `host_toolchain_roots` | `str` | `""` |  |
| `AGENT_HOST_WORKSPACE_ID` | `host_workspace_id` | `str` | `""` |  |
| `AGENT_IDEMPOTENCY_TTL_S` | `idempotency_ttl_s` | `int` | `600` | 幂等键保留时间。 |
| `AGENT_IMAGE_MAX_BYTES` | `image_max_bytes` | `int` | `10485760` | 图片理解上限；默认 LLM 视为多模态模型。 |
| `AGENT_IMAGE_MAX_DIMENSION` | `image_max_dimension` | `int` | `2048` |  |
| `AGENT_INSTRUCTIONS_ENABLED` | `instructions_enabled` | `bool` | `true` | 是否发现并注入仓库内的 AGENTS.md / CLAUDE.md（分层项目指令）。 |
| `AGENT_INTERFACE_FIRST_WRITES` | `interface_first_writes` | `bool` | `false` | 跨模块变更：按 ChangeContract 自动分波，并在波次之间跑 gate。 |
| `AGENT_INTERNAL_HMAC_KEY` | `agent_internal_hmac_key` | `str` | `""` | 空 = 只依赖内部 Bearer token（兼容旧部署）； 非空 = A2 对 X-Agent-Context 做 HMAC，A3 验签。 |
| `AGENT_INTERNAL_HMAC_REQUIRED` | `agent_internal_hmac_required` | `bool` | `false` | 是否**要求** A2 发来的 X-Agent-Context 带 HMAC 签名（非空 key）。 空 = 兼容未升级的部署，只靠内部 Bearer token；拆分部署应显式设 true。 A3 绑在非 loopback 地址时无论这里怎么配都强制要求（启动时校验）。 |
| `AGENT_INTERNAL_TOKEN` | `agent_internal_token` | `str` | `""` |  |
| `AGENT_INTERNAL_URL` | `agent_internal_url` | `str` | `""` | ---------- A2 → A3 内部连接（Phase B4） ---------- 空 = A2 与 Agent 同进程（默认，兼容旧部署）。 |
| `AGENT_KNOWLEDGE_ARTIFACTS_ENABLED` | `knowledge_artifacts_enabled` | `bool` | `false` |  |
| `AGENT_KNOWLEDGE_EMBEDDING_DIM` | `knowledge_embedding_dim` | `int` | `0` | 0 = 继承 memory_embedding_dim。 |
| `AGENT_KNOWLEDGE_EMBEDDING_PROVIDER` | `knowledge_embedding_provider` | `str` | `""` | 空串 = 继承 memory_embedding_provider；支持 hash/local/remote/none。 remote 仍受 memory_allow_remote_embeddings + base_url/key 双重门禁。 |
| `AGENT_KNOWLEDGE_SEARCH_MODE` | `knowledge_search_mode` | `str` | `"lexical"` | 知识检索模式：lexical（默认，零依赖）\| vector \| hybrid。 |
| `AGENT_LIBRARY_CHUNK_CHARS` | `library_chunk_chars` | `int` | `4000` |  |
| `AGENT_LIBRARY_CHUNK_OVERLAP` | `library_chunk_overlap` | `int` | `200` |  |
| `AGENT_LIBRARY_EMBEDDING_DIM` | `library_embedding_dim` | `int` | `0` | 0 = 继承 memory_embedding_dim。 |
| `AGENT_LIBRARY_EMBEDDING_PROVIDER` | `library_embedding_provider` | `str` | `""` | 空 = 继承 memory_embedding_provider；remote 仍受 memory 门禁。 |
| `AGENT_LIBRARY_ENABLED` | `library_enabled` | `bool` | `false` | ---------- 资料库（每用户跨工作区）---------- 默认关闭；开启后只暴露 library_search 只读工具。 |
| `AGENT_LIBRARY_JOB_STALE_S` | `library_job_stale_s` | `int` | `3600` |  |
| `AGENT_LIBRARY_MAX_CONCURRENT_JOBS` | `library_max_concurrent_jobs` | `int` | `2` | 异步任务：并发上限与崩溃/重启后的 stale 判定（秒）。 |
| `AGENT_LIBRARY_MAX_ITEMS` | `library_max_items` | `int` | `100` |  |
| `AGENT_LIBRARY_MAX_ITEM_BYTES` | `library_max_item_bytes` | `int` | `20971520` |  |
| `AGENT_LIBRARY_MAX_TOTAL_BYTES` | `library_max_total_bytes` | `int` | `268435456` |  |
| `AGENT_LIBRARY_MMR_LAMBDA` | `library_mmr_lambda` | `float` | `0.7` | MMR 多样性权重：1 = 纯相关度，越小越去重。 |
| `AGENT_LIBRARY_RERANK_BOOST` | `library_rerank_boost` | `float` | `0.25` |  |
| `AGENT_LIBRARY_RERANK_ENABLED` | `library_rerank_enabled` | `bool` | `true` | 确定性 rerank：查询短语/标题命中的小幅加权。 |
| `AGENT_LIBRARY_ROOT` | `library_root` | `str` | `""` | WSL 内绝对路径；空则按沙箱 $HOME/.agent-library 推导。 Web 多用户由 TenancyManager 覆盖成 per-user 目录。 |
| `AGENT_LIBRARY_SEARCH_MAX_CHARS` | `library_search_max_chars` | `int` | `8000` |  |
| `AGENT_LIBRARY_SEARCH_MODE` | `library_search_mode` | `str` | `"lexical"` | lexical（默认，零依赖）\| vector \| hybrid；vector/hybrid 失败回退词法。 |
| `AGENT_LIBRARY_SEARCH_TOP_K` | `library_search_top_k` | `int` | `8` |  |
| `AGENT_LIBRARY_VECTOR_BACKEND` | `library_vector_backend` | `str` | `"linear"` | linear（默认）\| hnswlib；hnswlib 未安装/失败时自动回退 linear。 |
| `AGENT_LIBRARY_VECTOR_CANDIDATES` | `library_vector_candidates` | `int` | `50` |  |
| `AGENT_LLM_AIMD_MAX` | `llm_aimd_max` | `int` | `8` |  |
| `AGENT_LLM_AIMD_MIN` | `llm_aimd_min` | `int` | `1` | LLM 每个 provider 的自适应并发范围。 |
| `AGENT_LLM_FALLBACK_ON_ERROR` | `llm_fallback_on_error` | `bool` | `true` | 非 429 错误是否也尝试下一个 provider。 |
| `AGENT_LLM_PROVIDERS` | `llm_providers` | `str` | `""` | 可选多 provider JSON；空 = 只用 DEEPSEEK_* 单 provider。 [{"name":"deepseek","base_url":"...","api_key":"...","model":"...", "priority":0}, {"name":"backup","api_key_env":"BACKUP_API_KEY",...}] |
| `AGENT_LLM_PROVIDER_MAX_WAIT_S` | `llm_provider_max_wait_s` | `float` | `30.0` | 所有 provider 都在冷却时，单次调用最多等待多久再放弃。 |
| `AGENT_LLM_ROUTING_STRATEGY` | `llm_routing_strategy` | `str` | `"cost"` | cost（按 provider 单价排序）\| priority（按配置顺序） |
| `AGENT_LLM_TEMPERATURE` | `llm_temperature` | `float` | `0.0` |  |
| `AGENT_LLM_TIMEOUT` | `llm_timeout` | `int` | `120` |  |
| `AGENT_LOG_FORMAT` | `log_format` | `str` | `"text"` | text \| json；容器/日志采集建议 json。 |
| `AGENT_LSP_COMMAND` | `lsp_command` | `str` | `""` | 可选 language server 命令；空 = 不启用（symbol_search 仍用 AST）。 |
| `AGENT_LSP_DIAGNOSTICS_WAIT_S` | `lsp_diagnostics_wait_s` | `float` | `2.0` | pull 诊断不可用时，等待 publishDiagnostics 的最长时间。 |
| `AGENT_LSP_TIMEOUT` | `lsp_timeout` | `float` | `10.0` |  |
| `AGENT_MAX_CAPTURE_BYTES` | `max_capture_bytes` | `int` | `1000000` | 宿主从沙箱捕获单条命令输出的字节上限（stdout/stderr 各自）。 `max_output_chars` 只控制回灌给模型的文本；捕获阶段若不设上限， 一条 `yes`/`cat big.log` 会在截断前先把宿主内存打满。 |
| `AGENT_MAX_CONCURRENT_AGENTS` | `max_concurrent_agents` | `int` | `4` | 同一条消息里的多个 delegate 调用最多同时跑几个。 子智能体只读，所以并发不会产生写冲突；限制主要是控制成本与 wsl.exe 压力。 |
| `AGENT_MAX_CONCURRENT_TOOLS` | `max_concurrent_tools` | `int` | `4` | 同一条消息里的多个只读工具最多同时跑几个；写工具始终串行。 |
| `AGENT_MAX_DELEGATE_CALLS` | `max_delegate_calls` | `int` | `6` | 单个任务最多委派几次探索 —— 与主 agent 的工具轮次预算相互独立。 默认给到 6：提示词鼓励把互不依赖的区域拆成多个并发 delegate， 预算若只有 2 会与这条建议直接冲突（实测 5 个里 3 个被预算拒绝）。 |
| `AGENT_MAX_FILE_READ_BYTES` | `max_file_read_bytes` | `int` | `2000000` |  |
| `AGENT_MAX_OUTPUT_CHARS` | `max_output_chars` | `int` | `20000` |  |
| `AGENT_MAX_PLAN_STEPS` | `max_plan_steps` | `int` | `5` |  |
| `AGENT_MAX_REPAIR_ROUNDS` | `max_repair_rounds` | `int` | `3` | ---------- Agent 循环 ---------- |
| `AGENT_MAX_REPLANS` | `max_replans` | `int` | `2` | 执行中「重建剩余计划」的次数上限。计划可以修正，但不能无限改 —— 边做边改没有收敛点，而每次重规划都要多一次模型调用 |
| `AGENT_MAX_RESUMES` | `max_resumes` | `int` | `50` | 同一会话内「挂起→恢复」的总次数上限，防前端反复 resume 绕开 recursion_limit |
| `AGENT_MAX_SAME_FAILURES` | `max_same_failures` | `int` | `2` | 连续出现同一失败签名多少次后停止 repair、改走 replan。 24 次相同失败验证的教训：没有这道闸，repair 会把同一类修改重复到额度耗尽。 |
| `AGENT_MAX_TOOL_ROUNDS` | `max_tool_rounds` | `int` | `12` |  |
| `AGENT_MEMORY_ALLOW_REMOTE_EMBEDDINGS` | `memory_allow_remote_embeddings` | `bool` | `false` |  |
| `AGENT_MEMORY_EMBEDDING_API_KEY` | `memory_embedding_api_key` | `str` | `""` |  |
| `AGENT_MEMORY_EMBEDDING_BASE_URL` | `memory_embedding_base_url` | `str` | `""` | 远程 OpenAI-compatible embedding（默认关闭，需双重显式开启）。 |
| `AGENT_MEMORY_EMBEDDING_BATCH_SIZE` | `memory_embedding_batch_size` | `int` | `64` |  |
| `AGENT_MEMORY_EMBEDDING_CACHE_DIR` | `memory_embedding_cache_dir` | `str` | `""` |  |
| `AGENT_MEMORY_EMBEDDING_DIM` | `memory_embedding_dim` | `int` | `512` |  |
| `AGENT_MEMORY_EMBEDDING_MODEL` | `memory_embedding_model` | `str` | `"BAAI/bge-small-zh-v1.5"` |  |
| `AGENT_MEMORY_EMBEDDING_PROVIDER` | `memory_embedding_provider` | `str` | `"local"` |  |
| `AGENT_MEMORY_EMBEDDING_TIMEOUT_S` | `memory_embedding_timeout_s` | `float` | `10.0` |  |
| `AGENT_MEMORY_FUSION` | `memory_fusion` | `str` | `"rrf"` |  |
| `AGENT_MEMORY_GLOBAL_ROOT` | `memory_global_root` | `str` | `""` | 跨工作区全局记忆根；空 = 沙箱 $HOME/.agent-memory/global。 |
| `AGENT_MEMORY_ROOT` | `memory_root` | `str` | `""` | 长期记忆根目录（WSL 绝对路径）；空则按 trust_repo_memory 推导。 |
| `AGENT_MEMORY_TOP_K` | `memory_top_k` | `int` | `8` | 每次运行注入系统提示的长期记忆条数（按与请求的相关度取前 K 条）。 0 表示不限制，整份注入 —— 记忆上限 50 条，但每条都会进入每一轮 system prompt。 |
| `AGENT_MEMORY_VECTOR_ENABLED` | `memory_vector_enabled` | `bool` | `false` | 可选向量检索：默认关闭，开启后仍以 BM25 为回退。 |
| `AGENT_MEMORY_VECTOR_TOP_N` | `memory_vector_top_n` | `int` | `20` |  |
| `AGENT_METRICS_TOKEN` | `metrics_token` | `str` | `""` | A2 /metrics 的抓取 token；空 = 复用普通 Web 认证。 |
| `AGENT_MFA_CHALLENGE_TTL_S` | `mfa_challenge_ttl_s` | `int` | `300` |  |
| `AGENT_MFA_ENABLED` | `mfa_enabled` | `bool` | `false` |  |
| `AGENT_MFA_ISSUER` | `mfa_issuer` | `str` | `"CodingAgent"` |  |
| `AGENT_MFA_MAX_ATTEMPTS` | `mfa_max_attempts` | `int` | `5` |  |
| `AGENT_MFA_WINDOW_STEPS` | `mfa_window_steps` | `int` | `1` |  |
| `AGENT_NUDGE_EMPTY_STEPS` | `nudge_empty_steps` | `bool` | `true` | 「一步没有任何改动」时是否给它一次重做的机会。  背景：`dirty=False` 时 verify 与 review 都会短路，控制流直接 advance —— 于是「这一步只读了文件」与「这一步做完了」在图上**无法区分**。实测过一整批 长程任务因此把计划"读"完就算完成：10 次运行里 5 次一次写都没尝试过。  默认开启，但**必须可关**：它会改变所有任务的行为，现有基线因此不再可比 （评测的 baseline 里记了这个开关）。 |
| `AGENT_OIDC_ALLOW_INSECURE_HTTP` | `oidc_allow_insecure_http` | `bool` | `false` | 仅本地开发允许 http:// issuer；生产必须 HTTPS。 |
| `AGENT_OIDC_AUTO_CREATE_USERS` | `oidc_auto_create_users` | `bool` | `true` |  |
| `AGENT_OIDC_CLIENT_ID` | `oidc_client_id` | `str` | `""` |  |
| `AGENT_OIDC_CLIENT_SECRET` | `oidc_client_secret` | `str` | `""` |  |
| `AGENT_OIDC_DEFAULT_ROLE` | `oidc_default_role` | `str` | `"member"` |  |
| `AGENT_OIDC_EMAIL_CLAIM` | `oidc_email_claim` | `str` | `"email"` |  |
| `AGENT_OIDC_ENABLED` | `oidc_enabled` | `bool` | `false` | --- 企业身份（RFC-0004）--- |
| `AGENT_OIDC_HTTP_TIMEOUT_S` | `oidc_http_timeout_s` | `float` | `10.0` |  |
| `AGENT_OIDC_ISSUER` | `oidc_issuer` | `str` | `""` |  |
| `AGENT_OIDC_REDIRECT_URI` | `oidc_redirect_uri` | `str` | `""` |  |
| `AGENT_OIDC_ROLE_CLAIM` | `oidc_role_claim` | `str` | `"groups"` |  |
| `AGENT_OIDC_ROLE_MAP` | `oidc_role_map` | `str` | `""` | JSON: {"idp-group": "admin", ...}；未命中用 default_role。 |
| `AGENT_OIDC_SCOPES` | `oidc_scopes` | `str` | `"openid profile email"` |  |
| `AGENT_OIDC_STATE_TTL_S` | `oidc_state_ttl_s` | `int` | `600` |  |
| `AGENT_OIDC_USERNAME_CLAIM` | `oidc_username_claim` | `str` | `"preferred_username"` |  |
| `AGENT_ORCHESTRATION_CONTROL_CHECKPOINTS` | `orchestration_control_checkpoints` | `bool` | `true` | 阶段门/轻量控制检查点：只存契约/计划/产物引用/预算，不存全量消息。 |
| `AGENT_ORCHESTRATOR_ADD_EXPLORE_TESTS` | `orchestrator_add_explore_tests` | `bool` | `false` | 只读模式下模型只给一个任务时，是否强行补一个「找测试」explore 任务。 默认关闭：这会增加一次 worker 成本，只有演示并发/确实需要测试画像时才值得。 |
| `AGENT_ORCHESTRATOR_ALLOW_OVERLAP` | `orchestrator_allow_overlap` | `bool` | `false` | 允许多个 implement 修改同一文件；合并时用 git apply --3way。 有真实冲突时只回滚本分量，不会把冲突标记落进主工作区。 |
| `AGENT_ORCHESTRATOR_AUTHOR_CONCURRENCY` | `orchestrator_author_concurrency` | `int` | `1` |  |
| `AGENT_ORCHESTRATOR_AUTHOR_MODEL` | `orchestrator_author_model` | `str` | `""` |  |
| `AGENT_ORCHESTRATOR_AUTHOR_TOKEN_BUDGET` | `orchestrator_author_token_budget` | `int` | `60000` |  |
| `AGENT_ORCHESTRATOR_BUDGET_WARNING_RATIO` | `orchestrator_budget_warning_ratio` | `float` | `0.7` | 70% 预告警；100% 停止派发并输出已完成部分/卡点/建议。 |
| `AGENT_ORCHESTRATOR_CHECKPOINT_EVERY` | `orchestrator_checkpoint_every` | `int` | `4` | scheduler 每完成多少个任务返回一次父图，形成 checkpoint/干预边界。 |
| `AGENT_ORCHESTRATOR_CONTEXT_CHARS` | `orchestrator_context_chars` | `int` | `8000` | 交给主 agent 的 worker 汇总上限（字符）。 |
| `AGENT_ORCHESTRATOR_DEPENDENCY_CONTEXT_CHARS` | `orchestrator_dependency_context_chars` | `int` | `12000` | 依赖任务注入下游 worker 的上下文总字符上限。 |
| `AGENT_ORCHESTRATOR_DOCUMENT_CHECKPOINT` | `orchestrator_document_checkpoint` | `bool` | `true` | 每个 document 任务完成后触发一次 steering/checkpoint 边界。 |
| `AGENT_ORCHESTRATOR_DOCUMENT_CONCURRENCY` | `orchestrator_document_concurrency` | `int` | `2` | 文档管线角色级并发；author 固定为 1（报告写入全局单实例）。 |
| `AGENT_ORCHESTRATOR_DOCUMENT_MODEL` | `orchestrator_document_model` | `str` | `""` |  |
| `AGENT_ORCHESTRATOR_DOCUMENT_TOKEN_BUDGET` | `orchestrator_document_token_budget` | `int` | `120000` | 角色级累计 token 硬预算；0 = 不限制。文档/报告是单次成本最高的角色， 给保守默认值，避免一份大文档或一轮报告无限消耗；超限只阻塞该角色。 |
| `AGENT_ORCHESTRATOR_ENABLED` | `orchestrator_enabled` | `bool` | `true` | ---------- Orchestrator（Phase 3A）---------- 新架构默认启用：满足规模阈值的仓库走 harness；小仓库仍走 direct 快速路径。 |
| `AGENT_ORCHESTRATOR_ESTIMATION_ENABLED` | `orchestrator_estimation_enabled` | `bool` | `true` | 执行前预估（S5）：规则先验 + 历史分桶（task_type × role × model）。 |
| `AGENT_ORCHESTRATOR_ESTIMATION_MIN_SAMPLES` | `orchestrator_estimation_min_samples` | `int` | `5` |  |
| `AGENT_ORCHESTRATOR_EXPLORE_MODEL` | `orchestrator_explore_model` | `str` | `"deepseek-flash"` | 角色级模型覆盖；空串表示沿用 orchestrator_worker_model（再退到主模型）。 explore/review 默认用便宜模型，失败重试时自动升级到主模型； 网关不支持 deepseek-flash 时，把这两个变量置空即可回退。 |
| `AGENT_ORCHESTRATOR_FANIN_REDUCER` | `orchestrator_fanin_reducer` | `bool` | `false` | fan-in 超预算/有冲突时才调用一次 reducer LLM；失败回退确定性汇总。 |
| `AGENT_ORCHESTRATOR_FANOUT_ARTIFACTS` | `orchestrator_fanout_artifacts` | `bool` | `true` | fan-in 是否落 artifact store 并输出结构化结果。 |
| `AGENT_ORCHESTRATOR_FIX_ROUNDS` | `orchestrator_fix_rounds` | `int` | `0` | verify 失败后允许重入 orchestrator 修复的最大轮数；0 = 关闭。 |
| `AGENT_ORCHESTRATOR_GATE_TIMEOUT_S` | `orchestrator_gate_timeout_s` | `float` | `0.0` | 波次 gate 验证的墙钟上限；0 = 自动推导（2 × verify_timeout + 30s， 为“先跑选中用例、通过后再补全量”留出两次命令的空间）。 |
| `AGENT_ORCHESTRATOR_IMPLEMENT_MODEL` | `orchestrator_implement_model` | `str` | `""` |  |
| `AGENT_ORCHESTRATOR_INCREMENTAL_MERGE` | `orchestrator_incremental_merge` | `bool` | `true` | 已完成的 implement 补丁若与在飞任务无文件交集，立即按连通分量合并， 不必等整批结束。默认开启；若有兼容性问题可设为 false 回退整批合并。 |
| `AGENT_ORCHESTRATOR_LONG_TASK_THRESHOLD_S` | `orchestrator_long_task_threshold_s` | `int` | `120` |  |
| `AGENT_ORCHESTRATOR_MAX_CONCURRENT_WORKERS` | `orchestrator_max_concurrent_workers` | `int` | `4` | 同一批 ready 任务最多并发几个只读 worker。 |
| `AGENT_ORCHESTRATOR_MAX_DEPTH` | `orchestrator_max_depth` | `int` | `1` |  |
| `AGENT_ORCHESTRATOR_MAX_FANOUT` | `orchestrator_max_fanout` | `int` | `2` |  |
| `AGENT_ORCHESTRATOR_MAX_PENDING_MERGES` | `orchestrator_max_pending_merges` | `int` | `8` | 在飞 implement + 待合并补丁的上限；超过后暂停启动新的 implement， 先让合并队列排空，避免补丁/快照堆积。 |
| `AGENT_ORCHESTRATOR_MAX_TASKS` | `orchestrator_max_tasks` | `int` | `6` |  |
| `AGENT_ORCHESTRATOR_MAX_WRITE_STAGES` | `orchestrator_max_write_stages` | `int` | `2` | 灰度期最多允许的写波次数（stage 上限）。 |
| `AGENT_ORCHESTRATOR_MIN_FILES` | `orchestrator_min_files` | `int` | `30` | 小于该文件数不做编排，直接单 agent。 |
| `AGENT_ORCHESTRATOR_MIN_PARALLEL_GAIN` | `orchestrator_min_parallel_gain` | `float` | `1.2` | 并行收益比阈值：task_count / critical_path 低于该值时，只读并行计划 会退回 direct（链式任务并行只多付调度/上下文成本）。0 = 关闭预估。 |
| `AGENT_ORCHESTRATOR_NESTED_ENABLED` | `orchestrator_nested_enabled` | `bool` | `false` | 嵌套子编排器：默认关闭；开启后 MAX_DEPTH=1、单层扇出有限。 只有子任务确实需要独立契约/检查点时才应由 planner 选用。 |
| `AGENT_ORCHESTRATOR_PATCH_REVIEW` | `orchestrator_patch_review` | `bool` | `true` | 补丁合并前的轻量审查：只对高风险 patch（改测试/配置/CI/安全路径、 或 diff 超过阈值）调用一次 review 模型。审查拒绝会让任务带 findings 重试； 它只是质量加固，不替代 write_scope 与审批这些安全边界。 |
| `AGENT_ORCHESTRATOR_PATCH_REVIEW_CONCURRENCY` | `orchestrator_patch_review_concurrency` | `int` | `2` | 同时进行的补丁审查数；审查是 LLM 调用，串行会把合并尾部拉长。 |
| `AGENT_ORCHESTRATOR_PATCH_REVIEW_MAX_CHARS` | `orchestrator_patch_review_max_chars` | `int` | `60000` |  |
| `AGENT_ORCHESTRATOR_PATCH_REVIEW_MAX_LINES` | `orchestrator_patch_review_max_lines` | `int` | `400` |  |
| `AGENT_ORCHESTRATOR_PATCH_REVIEW_TIMEOUT_S` | `orchestrator_patch_review_timeout_s` | `float` | `0.0` | 单次补丁审查的墙钟上限；0 = 自动推导（review 角色的 hard timeout）。 超时按“审查未完成”放行合并，但用户取消/deadline 会停止整批合并。 |
| `AGENT_ORCHESTRATOR_PLANNER_MODEL` | `orchestrator_planner_model` | `str` | `""` | 工作流选择/任务图生成专用模型；空串表示继承主模型。 |
| `AGENT_ORCHESTRATOR_PLAN_REPAIR` | `orchestrator_plan_repair` | `bool` | `true` | 任务图校验失败时，先带错误反馈让模型修一次，再决定是否整图降级。 默认开启；若要回到「校验失败立即降级」的旧行为，设为 false。 |
| `AGENT_ORCHESTRATOR_PLAN_REVIEW` | `orchestrator_plan_review` | `bool` | `true` | 独立计划评审（S4）：契约映射、不可验证步骤、重复步骤。 |
| `AGENT_ORCHESTRATOR_RATE_LIMIT_BACKOFF_S` | `orchestrator_rate_limit_backoff_s` | `float` | `2.0` | 遇到 429/限流时 scheduler 的首次退避秒数；之后指数增长，上限 30s。 |
| `AGENT_ORCHESTRATOR_REPEAT_ACTION_LIMIT` | `orchestrator_repeat_action_limit` | `int` | `3` | 同一 (tool, args) 在单个编排内重复出现达到该次数即熔断（F2 重试黑洞）。 |
| `AGENT_ORCHESTRATOR_REPO_MAP_LAZY` | `orchestrator_repo_map_lazy` | `bool` | `false` | survey 先只收集廉价画像；repo_map 等到确认要编排后再构建。 |
| `AGENT_ORCHESTRATOR_REVIEW_MODEL` | `orchestrator_review_model` | `str` | `"deepseek-flash"` |  |
| `AGENT_ORCHESTRATOR_RUN_DEADLINE_S` | `orchestrator_run_deadline_s` | `int` | `900` | 整个编排阶段（含所有 worker）的墙钟上限，0 表示不限制。 |
| `AGENT_ORCHESTRATOR_SEED_MAX_BYTES` | `orchestrator_seed_max_bytes` | `int` | `5000000` |  |
| `AGENT_ORCHESTRATOR_SEED_MAX_FILES` | `orchestrator_seed_max_files` | `int` | `200` | 注入新 worktree 的基线补丁上限；超限 fail closed。 |
| `AGENT_ORCHESTRATOR_SEQUENTIAL_WRITES` | `orchestrator_sequential_writes` | `bool` | `false` | 顺序写波次：允许后一波 implement 基于前一波已合并结果继续修改。 默认关闭；关闭时 implement 之间仍禁止互相依赖。 |
| `AGENT_ORCHESTRATOR_SHORT_TASK_THRESHOLD_S` | `orchestrator_short_task_threshold_s` | `int` | `60` |  |
| `AGENT_ORCHESTRATOR_SMART_ENTRY` | `orchestrator_smart_entry` | `bool` | `true` | 在规模阈值之上再加一层请求级快速判定：明显的单文件/只读问答/拼写修复 直接走 single agent，不付 survey + 工作流选择的固定成本。 |
| `AGENT_ORCHESTRATOR_TASK_RETRIES` | `orchestrator_task_retries` | `int` | `1` | 单个任务失败后最多重试几次（0 = 不重试）。重试仍失败才把依赖方标 blocked。 |
| `AGENT_ORCHESTRATOR_TASK_TOKENS` | `orchestrator_task_tokens` | `int` | `0` | 单任务 token 上限，0 表示不限制（由 provider 回报的 usage 累计判断）。 |
| `AGENT_ORCHESTRATOR_TOTAL_STEPS` | `orchestrator_total_steps` | `int` | `100` | 四维预算的另外两维：完成的任务尝试数与 worker 工具调用总数。 0 = 不限制；默认给失控循环一个硬边界，正常任务远低于该值。 |
| `AGENT_ORCHESTRATOR_TOTAL_TOKENS` | `orchestrator_total_tokens` | `int` | `500000` | 整个编排阶段的 token 上限，0 = 不限制。scheduler 在派发前按角色预留， 预留 + 已用超过上限时停止派发；只读在飞任务会被取消，implement 继续跑完 并合并。默认 500k，足以覆盖正常多 worker 任务，同时给失控循环止损。 |
| `AGENT_ORCHESTRATOR_TOTAL_TOOL_CALLS` | `orchestrator_total_tool_calls` | `int` | `1000` |  |
| `AGENT_ORCHESTRATOR_VISION_CONCURRENCY` | `orchestrator_vision_concurrency` | `int` | `2` |  |
| `AGENT_ORCHESTRATOR_VISION_TOKEN_BUDGET` | `orchestrator_vision_token_budget` | `int` | `60000` |  |
| `AGENT_ORCHESTRATOR_WORKER_HARD_TIMEOUT_S` | `orchestrator_worker_hard_timeout_s` | `float` | `0.0` | worker 外层兜底超时（秒），独立于各 runner 的内部超时。 0 = 自动推导：implement 用 worker_timeout，explore/review 用 explore_timeout， 其余角色取 max(600, worker_timeout)；都会再加 30s 余量。 |
| `AGENT_ORCHESTRATOR_WORKER_MODEL` | `orchestrator_worker_model` | `str` | `""` | worker 专用模型；空串表示继承 DEEPSEEK_MODEL。用于 explore/review 降本。 |
| `AGENT_ORCHESTRATOR_WORKER_REPAIRS` | `orchestrator_worker_repairs` | `int` | `2` |  |
| `AGENT_ORCHESTRATOR_WORKER_ROUNDS` | `orchestrator_worker_rounds` | `int` | `12` |  |
| `AGENT_ORCHESTRATOR_WORKER_TIMEOUT` | `orchestrator_worker_timeout` | `int` | `240` |  |
| `AGENT_ORCHESTRATOR_WRITE_ENABLED` | `orchestrator_write_enabled` | `bool` | `false` | ---- Phase 3C：并发写 worker（默认关闭）---- 打开后，orchestrator 才可能生成 implement 任务；还要求 --write、工作区是 git 仓库且干净。worker 在独立 git worktree 中修改，完成后由宿主合并补丁。 |
| `AGENT_ORCHESTRATOR_WRITE_PLAN_APPROVAL` | `orchestrator_write_plan_approval` | `bool` | `false` | 派发 implement 前一次性展示 write_scope 并等待确认（并发区不弹审批）。 |
| `AGENT_ORCHESTRATOR_WRITE_SCOPE_ENFORCE` | `orchestrator_write_scope_enforce` | `bool` | `true` | 写 worker 的 write_scope 在 I/O 前强制校验（三重防线之一）。 |
| `AGENT_OTEL_ENDPOINT` | `otel_endpoint` | `str` | `""` | OTLP HTTP trace；空 = 关闭。需要 `pip install -e ".[otel]"`。 |
| `AGENT_OTEL_HEADERS` | `otel_headers` | `str` | `""` | `k=v,k2=v2` 形式的 collector headers。 |
| `AGENT_OTEL_SERVICE_NAME` | `otel_service_name` | `str` | `"coding-agent"` |  |
| `AGENT_PDF_EXTRACT_ANNOTATIONS` | `pdf_extract_annotations` | `bool` | `true` |  |
| `AGENT_PDF_EXTRACT_ATTACHMENTS` | `pdf_extract_attachments` | `bool` | `true` |  |
| `AGENT_PDF_EXTRACT_FORMS` | `pdf_extract_forms` | `bool` | `true` |  |
| `AGENT_PDF_EXTRACT_METADATA` | `pdf_extract_metadata` | `bool` | `true` |  |
| `AGENT_PDF_EXTRACT_OUTLINE` | `pdf_extract_outline` | `bool` | `true` |  |
| `AGENT_PDF_EXTRACT_TABLES` | `pdf_extract_tables` | `bool` | `true` |  |
| `AGENT_PDF_TEXT_ONLY` | `pdf_text_only` | `bool` | `true` | 默认只处理文本型 PDF；扫描页不自动 OCR。 |
| `AGENT_PERSISTENT_SHELL` | `persistent_shell` | `bool` | `true` | 常驻 WSL bash 会话池。默认开启：实测读/检索从 ~170ms 降到 ~20ms。 会话异常时会自动退回逐命令 wsl.exe，因此可安全地作为默认值。 |
| `AGENT_PERSISTENT_SHELL_POOL` | `persistent_shell_pool` | `int` | `4` |  |
| `AGENT_PROCESS_RUNNER_MAX_CONCURRENT` | `process_runner_max_concurrent` | `int` | `2` |  |
| `AGENT_PROVIDER_PROBE_ALLOWED_HOSTS` | `provider_probe_allowed_hosts` | `str` | `""` | 目标主机后缀白名单（逗号分隔，如 `deepseek.com,openai.com`）； 空 = 不额外限制，仍需过 IP 判定。企业部署建议填上，可以顺带收敛 DNS rebinding 的残余风险。 |
| `AGENT_PROVIDER_PROBE_ALLOW_PRIVATE_NETWORK` | `provider_probe_allow_private_network` | `bool` | `false` | ---------- 服务端外连守卫（SSRF） ---------- provider 连通性探测会带上真实 API Key，目标必须过 security/netguard.py 的解析后 IP 判定。默认拒绝回环 / 内网 / 保留地址； 链路本地（169.254.169.254 等云元数据）**永远**拒绝，不受此开关影响。 本机或内网 OpenAI-compatible 端点显式打开它。 |
| `AGENT_QUOTA_RESERVATION_TTL_S` | `quota_reservation_ttl_s` | `int` | `3600` | 配额预留的存活时间；进程崩溃后由其他实例/下次启动清理。 |
| `AGENT_RATE_LIMIT_BACKEND` | `rate_limit_backend` | `str` | `"memory"` | memory = 单进程内存；redis = 多实例共享固定窗口。 |
| `AGENT_RATE_LIMIT_REDIS_URL` | `rate_limit_redis_url` | `str` | `""` |  |
| `AGENT_RATE_LIMIT_STATE_PATH` | `rate_limit_state_path` | `str` | `""` | 跨进程限流状态文件；留空 = <当前目录>/.agent/rate_limits.json；:memory: 关闭 |
| `AGENT_READ_CACHE_MAX_BYTES` | `read_cache_max_bytes` | `int` | `"64 * 1024 * 1024"` | run 级文件内容缓存的字节上限（LRU）。0 = 关闭缓存。 |
| `AGENT_READ_DENY_GLOBS` | `read_deny_globs` | `str` | `"DEFAULT_READ_DENY_GLOBS"` | 秘密文件读保护 glob（逗号分隔）；.env.example 等模板自动放行。 |
| `AGENT_REGRESSION_CAPTURE` | `regression_capture_enabled` | `bool` | `true` | 跑偏轨迹回归集：只记结构化摘要/指标，不记提示词或工具原文。 |
| `AGENT_REPORT_ALLOW_ANY_PATH` | `report_allow_any_path` | `bool` | `false` |  |
| `AGENT_REPORT_FONT_PATH` | `report_font_path` | `str` | `""` |  |
| `AGENT_REPORT_LLM_REVIEW` | `report_llm_review` | `bool` | `false` |  |
| `AGENT_REPORT_OUTPUT_DIR` | `report_output_dir` | `str` | `".agent/artifacts/reports"` | 报告输出：默认只允许写到报告目录；显式路径由 planner 解析后仍受此约束。 |
| `AGENT_REPO_MAP_CHARS` | `repo_map_chars` | `int` | `4000` | repo map 注入 planner 的字符预算（0 = 关闭）。 |
| `AGENT_RETRY_ATTEMPTS` | `retry_attempts` | `int` | `3` | 节点级瞬时错误重试（超时/连接重置/5xx/429/环境抖动）。 attempts 是**总尝试次数**；1 表示不重试。退避为指数 + 抖动， 且整次重试累计等待不超过 max_total_wait，避免长时间卡住。 |
| `AGENT_RETRY_BASE_DELAY_S` | `retry_base_delay_s` | `float` | `1.0` |  |
| `AGENT_RETRY_MAX_DELAY_S` | `retry_max_delay_s` | `float` | `30.0` |  |
| `AGENT_RETRY_MAX_TOTAL_WAIT_S` | `retry_max_total_wait_s` | `float` | `60.0` |  |
| `AGENT_REVIEW_ENABLED` | `review_enabled` | `bool` | `true` | ---------- 代码审查 ---------- 一步做完、验证通过后，对本次改动跑一遍确定性静态检查（`tools/review.py`）。 与自动 verify 同一口径：不过审批，所以只跑宿主写死的检查，不读工作区里的 任何"配置"。关掉它等于去掉「测试通过 ≠ 代码正确」那第二道关。 |
| `AGENT_REVIEW_LINT_BLOCKING` | `review_lint_blocking` | `bool` | `true` | 探测到的 linter/formatter 对**本次改动文件**的发现是否阻断。 只把改动文件提升为 blocking：仓库里已有的历史问题不该每一步都卡死。 |
| `AGENT_RUN_COST_BUDGET_USD` | `run_cost_budget_usd` | `float` | `0.0` | 单次 run 的成本预算（USD）；0 = 不告警 |
| `AGENT_RUN_DEADLINE_S` | `run_deadline_s` | `int` | `1800` | 一次完整运行（含审批/暂停后的 resume）的总墙钟上限，0 表示不限制。 与 recursion_limit 互补：后者管步数，这里管时间（例如模型长时间卡住）。 |
| `AGENT_RUN_LEASE_TTL_S` | `run_lease_ttl_s` | `int` | `120` | ---------- 一致性与内部签名（M4） ---------- 会话级运行租约 TTL；A2 崩溃后到期自动释放。 |
| `AGENT_SANDBOX_MAX_CONCURRENT_COMMANDS` | `sandbox_max_concurrent_commands` | `int` | `8` | 同时启动的 wsl.exe 进程上限。并发 worker 会各自调用沙箱，没有这层 背压时 4 个 worker 的一次工具轮次就能同时拉起十几个 WSL 进程。 |
| `AGENT_SECRET_MASTER_KEY` | `secret_master_key` | `str` | `""` | 上游 provider Key 的 AES-GCM 主密钥（base64 32B）或文件。 |
| `AGENT_SECRET_MASTER_KEY_FILE` | `secret_master_key_file` | `str` | `""` |  |
| `AGENT_SECRET_MASTER_KEY_VERSION` | `secret_master_key_version` | `int` | `1` | 主密钥代次；轮换脚本递增，写入每条密文的 key_version。 |
| `AGENT_SECRET_PROVIDER` | `secret_provider` | `str` | `"auto"` | auto \| keyring \| systemd；服务器部署用 systemd-creds，Vault/KMS 走同一 Protocol。 |
| `AGENT_SECURITY_PROFILE` | `security_profile` | `str` | `"local"` | ---------- 安全 profile 与不可信仓库 ---------- local（默认，面向可信仓库）\| untrusted（bwrap + 禁网 + 强制 ask + 不信任仓库指令/记忆）。 |
| `AGENT_SESSION_BRIEF_CHARS` | `session_brief_chars` | `int` | `4000` | 编排子图/worker 的会话约束摘要上限（字符）。独立子图不复制 messages， 这里从 checkpoint 抽最近若干轮压缩后注入；0 表示关闭。 |
| `AGENT_SESSION_COST_BUDGET_USD` | `session_cost_budget_usd` | `float` | `0.0` | 整个会话（同一 thread，多轮 run 累计）的成本预算（USD）；0 = 不限制。 全新 run 在开始前检查；resume 不拦截，避免挂起的审批被预算锁死。 |
| `AGENT_SHELL_CPU_SECONDS` | `shell_cpu_seconds` | `int` | `600` | 沙箱资源上限，0 表示不限制 |
| `AGENT_SHELL_ENV_ALLOWLIST` | `shell_env_allowlist` | `str` | `""` | WSL 命令默认 `env -i` 清空宿主环境。这里按名称显式放行少量构建变量 （如 JAVA_HOME/GOPATH/VIRTUAL_ENV）。名称含 KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/AUTH/COOKIE/SESSION 的一律忽略。 |
| `AGENT_SHELL_MAX_FILE_MB` | `shell_max_file_mb` | `int` | `512` |  |
| `AGENT_SHELL_MAX_PROCESSES` | `shell_max_processes` | `int` | `1024` |  |
| `AGENT_SHELL_MEMORY_MB` | `shell_memory_mb` | `int` | `0` |  |
| `AGENT_SHELL_NETWORK` | `shell_network` | `bool` | `true` | 仅 bwrap 模式生效：false 时加 --unshare-net，命令完全无网络。 off 模式没有内核级隔离，词法策略无法禁止 curl/wget 出网。 |
| `AGENT_SHELL_SANDBOX` | `shell_sandbox` | `str` | `"off"` | shell 隔离后端：off（默认，仅工作区 cwd 约束）\| bwrap（挂载命名空间隔离） bwrap 模式下若沙箱内不可用，会**拒绝执行**（fail closed），不静默降级 |
| `AGENT_SHELL_TIMEOUT` | `shell_timeout` | `int` | `60` |  |
| `AGENT_SMTP_FROM` | `smtp_from` | `str` | `""` |  |
| `AGENT_SMTP_HOST` | `smtp_host` | `str` | `""` | 自助密码重置邮件（SMTP）；host 为空 = 不发送。 |
| `AGENT_SMTP_PASSWORD` | `smtp_password` | `str` | `""` |  |
| `AGENT_SMTP_PORT` | `smtp_port` | `int` | `587` |  |
| `AGENT_SMTP_STARTTLS` | `smtp_starttls` | `bool` | `true` |  |
| `AGENT_SMTP_USER` | `smtp_user` | `str` | `""` |  |
| `AGENT_SNAPSHOT_KEEP_COUNT` | `snapshot_keep_count` | `int` | `200` | 快照保留：每次运行开始清理，保留最新 N 份且不超过 M 天。0 = 不按该维度清理。 |
| `AGENT_SNAPSHOT_KEEP_DAYS` | `snapshot_keep_days` | `int` | `30` |  |
| `AGENT_SNAPSHOT_MANIFEST_DIR` | `snapshot_manifest_dir` | `str` | `""` | 快照校验清单根目录；留空 = 用户主目录下的 .codingagent/snapshots。 放在工作区外，shell 级篡改快照内容也能在恢复时被发现。 |
| `AGENT_STEERING_DIR` | `steering_dir` | `str` | `""` | 未消费 steering 指令的持久化目录；留空 = <当前目录>/.agent/steering |
| `AGENT_SYMBOL_INDEX_TTL_S` | `symbol_index_ttl_s` | `float` | `10.0` | AST 符号索引缓存时间；写操作不会主动失效，过期后自动重建。 |
| `AGENT_TOOL_JOURNAL_ENABLED` | `tool_journal_enabled` | `bool` | `true` | 有副作用工具的执行日志：崩溃后同一 call_id 不自动重放。 |
| `AGENT_TRUST_REPO_INSTRUCTIONS` | `trust_repo_instructions` | `bool` | `true` | 仓库内的 AGENTS.md / CLAUDE.md 是否作为可信指令注入。 |
| `AGENT_TRUST_REPO_MEMORY` | `trust_repo_memory` | `bool` | `true` | 是否读取仓库内 `.agent/memory.md`；false 时记忆改存 memory_root。 |
| `AGENT_USD_TO_CNY` | `usd_to_cny` | `float` | `7.2` |  |
| `AGENT_VERIFY_COMMAND` | `verify_command` | `str` | `""` | 留空表示按项目清单自动识别（pytest / cargo / go / npm / make） |
| `AGENT_VERIFY_ENABLED` | `verify_enabled` | `bool` | `true` | ---------- 验证 ---------- |
| `AGENT_VERIFY_ONLY_FINAL` | `verify_only_final` | `bool` | `true` | 多步计划里，非最终的 implement 步不跑全量测试：早期步骤只完成了一部分， 全量套件必然因为后面还没做的步骤而失败，会把 agent 卡死在这一步。 只在 kind=verify 或最后一步验证；单步任务不受影响。 |
| `AGENT_VERIFY_TIMEOUT` | `verify_timeout` | `int` | `300` | 自动验证的独立超时。测试/构建通常比普通 shell 命令长，不能共用 60s； 0 表示回退到 shell_timeout。 |
| `AGENT_WEBPUSH_PRIVATE_KEY` | `webpush_private_key` | `str` | `""` |  |
| `AGENT_WEBPUSH_PUBLIC_KEY` | `webpush_public_key` | `str` | `""` | 浏览器 Web Push（VAPID）。公钥/私钥都配置且安装 pywebpush 才启用； 私钥只放服务端，绝不下发。 |
| `AGENT_WEBPUSH_SUBJECT` | `webpush_subject` | `str` | `"mailto:admin@localhost"` |  |
| `AGENT_WEBPUSH_TTL` | `webpush_ttl` | `int` | `3600` |  |
| `AGENT_WEB_ACCOUNTS_ENABLED` | `web_accounts_enabled` | `bool` | `false` | 企业账号：打开后使用 control_users / session Cookie 登录。 |
| `AGENT_WEB_ACCOUNTS_REQUIRE_MYSQL` | `web_accounts_require_mysql` | `bool` | `true` | 企业账号的生产约束：只允许 MySQL；本地测试可显式关闭。 |
| `AGENT_WEB_ACCOUNT_TENANT` | `web_account_tenant` | `str` | `"default"` |  |
| `AGENT_WEB_ALLOW_ANONYMOUS_LOCAL` | `web_allow_anonymous_local` | `bool` | `true` | 未配置 token 时是否允许 loopback 匿名访问（单用户本地模式）。 |
| `AGENT_WEB_AUTH_TOKENS` | `web_auth_tokens` | `str` | `""` | ---------- Web 多用户 ---------- JSON：{"token": "user"}；与 AGENT_WEB_AUTH_TOKENS_FILE 二选一。 |
| `AGENT_WEB_AUTH_TOKENS_FILE` | `web_auth_tokens_file` | `str` | `""` |  |
| `AGENT_WEB_COOKIE_SECURE` | `web_cookie_secure` | `bool` | `false` | 跨源部署时 Cookie 必须 Secure；同源反代可关。 |
| `AGENT_WEB_CSP_REPORT_RATE_LIMIT_PER_MIN` | `web_csp_report_rate_limit_per_min` | `int` | `300` | 匿名 CSP 违规上报按来源 IP 限流；防止被当作写日志放大器。 |
| `AGENT_WEB_DATA_ROOT` | `web_data_root` | `str` | `""` | per-user 宿主侧数据根目录（审计/checkpoint/steering）； 空 = <当前目录>/.agent/web/users。 |
| `AGENT_WEB_FRONTEND_DIR` | `web_frontend_dir` | `str` | `""` | 前端发布包根目录（含 index.html / sw.js / manifest.webmanifest）。 空 = 包内 web/static（桌面与开发回退）。 |
| `AGENT_WEB_LIBRARY_ROOT` | `web_library_root` | `str` | `""` | per-user 资料库根目录；空 = 沙箱 $HOME/agent-library。 每个用户一份，跨工作区保留。 |
| `AGENT_WEB_LOGIN_RATE_LIMIT_PER_MIN` | `web_login_rate_limit_per_min` | `int` | `10` | 登录按来源 IP 的固定窗口限流。 |
| `AGENT_WEB_MAX_BODY_BYTES` | `web_max_body_bytes` | `int` | `"256 * 1024"` |  |
| `AGENT_WEB_MAX_PROMPT_CHARS` | `web_max_prompt_chars` | `int` | `100000` |  |
| `AGENT_WEB_MAX_RUNTIMES` | `web_max_runtimes` | `int` | `8` |  |
| `AGENT_WEB_MAX_RUNTIMES_PER_USER` | `web_max_runtimes_per_user` | `int` | `2` |  |
| `AGENT_WEB_MAX_UPLOAD_BYTES` | `web_max_upload_bytes` | `int` | `20971520` | 单次上传的单文件上限；上传走单独授权，写入 .agent/uploads。 |
| `AGENT_WEB_MAX_UPLOAD_FILES` | `web_max_upload_files` | `int` | `20` | 单次请求最多上传几个文件。 |
| `AGENT_WEB_MAX_UPLOAD_TOTAL_BYTES` | `web_max_upload_total_bytes` | `int` | `209715200` | 每个会话累计上传上限，防止反复上传占满工作区。 |
| `AGENT_WEB_MEMORY_ROOT` | `web_memory_root` | `str` | `""` | per-user WSL 侧记忆根目录；空 = 沙箱 $HOME/agent-memory。 记忆在沙箱内、仓库之外，避免不可信仓库预置记忆。 |
| `AGENT_WEB_OPENAPI` | `web_openapi` | `bool` | `false` | /openapi.json 与 /docs；默认关闭，避免把完整路由表暴露给公网。 |
| `AGENT_WEB_PASSWORD_RESET_TTL_S` | `web_password_reset_ttl_s` | `int` | `1800` |  |
| `AGENT_WEB_PUBLIC_URL` | `web_public_url` | `str` | `""` | 对外访问根，用于邮件里的重置链接；空 = 邮件只带 token。 |
| `AGENT_WEB_QUOTA_RESERVE_TOKENS` | `web_quota_reserve_tokens` | `int` | `20000` | 运行前为 token 限额预留的量；0 = 只做事后限额。默认预留一段， 让并发/长 run 在真正超额前被硬上限拦住。 |
| `AGENT_WEB_RATE_LIMIT_PER_MIN` | `web_rate_limit_per_min` | `int` | `60` |  |
| `AGENT_WEB_RUNTIME_TTL_S` | `web_runtime_ttl_s` | `int` | `1800` | 挂起 runtime 的空闲保留时间；超时后释放，resume 时从 checkpoint 重建。 |
| `AGENT_WEB_SERVE_FRONTEND` | `web_serve_frontend` | `bool` | `true` | 是否由 A2 托管前端页面。独立静态站/CDN 部署时关闭，只保留 API； 桌面模式（desktop_mode）始终强制开启，否则没有页面可加载。 |
| `AGENT_WEB_SESSION_IDLE_TTL_S` | `web_session_idle_ttl_s` | `int` | `0` | 空闲超时：超过该时长没有请求则失效；仍受上面的绝对 TTL 限制。 0 = 关闭（只使用绝对 TTL）。 |
| `AGENT_WEB_SESSION_TTL_S` | `web_session_ttl_s` | `int` | `"12 * 3600"` |  |
| `AGENT_WEB_STATIC_DIR` | `web_static_dir` | `str` | `""` | /static 挂载目录；空 = 与 web_frontend_dir 相同。 发布包通常为 <frontend_dir>/static，包内则两者同目录。 |
| `AGENT_WEB_WORKSPACES_ROOT` | `web_workspaces_root` | `str` | `""` | per-user 工作区根目录；空 = 沙箱 $HOME/agent-ws/users。 |
| `AGENT_WORKER_JOURNAL_ENABLED` | `worker_journal_enabled` | `bool` | `true` | worker 结果 journal：崩溃/resume 时复用已完成的只读结果，避免重跑。 只复用 success（可选 partial）；implement 永不复用（补丁可能已过期）。 |
| `AGENT_WORKER_JOURNAL_MAX_BYTES` | `worker_journal_max_bytes` | `int` | `10485760` |  |
| `AGENT_WORKER_JOURNAL_REQUIRE_CLEAN_GIT` | `worker_journal_require_clean_git` | `bool` | `true` | 只有「git 仓库 + 干净工作区 + 能拿到 HEAD」时才复用：HEAD 相同 且没有未提交改动，输入与代码状态才足够一致。 |
| `AGENT_WORKER_JOURNAL_REUSE_PARTIAL` | `worker_journal_reuse_partial` | `bool` | `false` |  |
| `AGENT_WORKER_JOURNAL_ROLES` | `worker_journal_roles` | `str` | `"explore,review"` | 允许复用/落盘的角色，逗号分隔。test 默认排除：结果依赖当时环境。 |
| `AGENT_WORKSPACE_ARCHIVE_ROOT` | `workspace_archive_root` | `str` | `""` | revision 归档根；空 = 关闭归档（仍可运行，但不能灾难恢复）。 |
| `AGENT_WORKSPACE_EXTRACT_MAX_RATIO` | `workspace_extract_max_ratio` | `int` | `200` | 解压比上限：解压后总字节 / 归档字节，防止 zip bomb。 |
| `AGENT_WORKSPACE_IMPORT_MODE` | `workspace_import_mode` | `str` | `"copy"` | copy = 桌面默认复制到 ext4；mount = 显式降级，需二次确认。 |
| `AGENT_WORKSPACE_JOB_RETENTION_DAYS` | `workspace_job_retention_days` | `int` | `7` | A3 导入/上传任务状态保留天数；超期的终态文件在下次启动时清理。 |
| `AGENT_WORKSPACE_KIND_DEFAULT` | `workspace_kind_default` | `str` | `"sandbox"` | 默认 sandbox 保持旧行为；桌面启动器显式设为 host。 |
| `AGENT_WORKSPACE_LOCK` | `workspace_lock_enabled` | `bool` | `true` | 同一工作区只允许一个 runtime 运行；锁文件在审计目录下。 |
| `AGENT_WORKSPACE_MOUNT_ROOTS` | `workspace_mount_roots` | `str` | `""` | 允许的 /mnt 挂载根，逗号分隔；空 = 禁止任何挂载工作区。 |
| `AGENT_WORKSPACE_REGISTRY` | `workspace_registry` | `bool` | `false` | ---------- 工作区注册表（RFC-0001，默认关闭） ---------- false = 继续使用 control_sessions.workspace 裸路径，行为与旧部署一致。 |
| `AGENT_WORKSPACE_REVISIONS_KEEP` | `workspace_revisions_keep` | `int` | `10` | 每个工作区保留的 revision 数；超出后清理最旧的非 pinned 归档。 |
| `AGENT_WORKSPACE_ROOT` | `workspace_root` | `str` | `""` | 工作区根；空 = 沿用租户 workspaces_root（TenantManager）。 必须是 A3/A4 视角的绝对 POSIX 路径；host 工作区也可以写成 Windows 路径（`D:\ws`），校验时两侧都会归一化成 POSIX 再比较。 |
| `AGENT_WORKSPACE_SCAN_PROVIDER` | `workspace_scan_provider` | `str` | `"none"` | none \| clamav；企业部署应在 M3 设为 clamav 并配合 fail-closed。 |
| `AGENT_WORKSPACE_SCAN_TIMEOUT_S` | `workspace_scan_timeout_s` | `int` | `300` | ClamAV 单次扫描超时。 |
| `AGENT_WORKSPACE_UPLOAD_MAX_BYTES` | `workspace_upload_max_bytes` | `int` | `209715200` | 项目归档上传上限（单包、需 Content-Length）；默认 200 MiB。 |
| `AGENT_WORKSPACE_UPLOAD_MAX_FILES` | `workspace_upload_max_files` | `int` | `200000` | 解包后的文件数上限。 |
| `AGENT_WRITE_DENY_GLOBS` | `write_deny_globs` | `str` | `""` | 追加的内容级写保护 glob（逗号分隔）；默认规则始终生效。 |
| `AGENT_WSL_BREAKER_COOLDOWN_S` | `wsl_breaker_cooldown_s` | `float` | `15.0` |  |
| `AGENT_WSL_BREAKER_FAILURES` | `wsl_breaker_failures` | `int` | `3` |  |
| `AGENT_WSL_BREAKER_MAX_COOLDOWN_S` | `wsl_breaker_max_cooldown_s` | `float` | `60.0` |  |
| `AGENT_WSL_DISTRO` | `wsl_distro` | `str` | `"Ubuntu"` | ---------- WSL2 沙箱 ---------- |
| `AGENT_WSL_HEALTH_CHECK` | `wsl_health_check_enabled` | `bool` | `true` | WSL 健康检查与熔断。连续失败达到阈值后，短时间内直接 fail fast， 避免每个工具调用都去拉起一个注定失败的 wsl.exe；冷却后放一个探测请求。 |
| `AGENT_WSL_HEALTH_TIMEOUT_S` | `wsl_health_timeout_s` | `int` | `10` |  |
| `AGENT_WSL_HEALTH_TTL_S` | `wsl_health_ttl_s` | `float` | `30.0` |  |
| `AGENT_WSL_WORKSPACE` | `wsl_workspace` | `str` | `""` | 留空表示自动使用沙箱内 $HOME/agent-ws，避免与真实用户名不匹配 |
| `DEEPSEEK_API_KEY` | `deepseek_api_key` | `str` | `""` | ---------- DeepSeek ---------- |
| `DEEPSEEK_BASE_URL` | `deepseek_base_url` | `str` | `"https://api.deepseek.com"` |  |
| `DEEPSEEK_MODEL` | `deepseek_model` | `str` | `"deepseek-chat"` |  |
| `LANGSMITH_API_KEY` | `langsmith_api_key` | `str` | `""` |  |
| `LANGSMITH_PROJECT` | `langsmith_project` | `str` | `"coding-agent"` |  |
| `LANGSMITH_TRACING` | `langsmith_tracing` | `bool` | `false` | ---------- LangSmith ---------- |

