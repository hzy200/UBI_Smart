"""全局配置。

约定：环境变量名与业界惯例保持一致（如 ``DEEPSEEK_API_KEY``），
Agent 自有配置统一加 ``AGENT_`` 前缀。

**优先级：构造函数参数 > `.env` > 环境变量 > 默认值。**

通用约定是「环境变量 > .env」，这里刻意反过来。原因是本机踩到的实际故障：
harness 在进程启动时把陈旧的值注入进程环境，用户既看不到来源（不在注册表、
也不在任何配置文件里）也删不掉，于是改 `.env` 完全不生效 ——
表现为「明明更新了 API key 却一直 403」。

项目目录里的 `.env` 是**显式写下、看得见、可编辑**的意图，让它优先；
需要临时覆盖时用命令行前缀或环境变量改 `.env` 本身即可。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

DEFAULT_READ_DENY_GLOBS = (
    ".env,.env.*,*.pem,*.key,*.p12,*.pfx,"
    "id_rsa*,id_ed25519*,id_ecdsa*,.ssh/**,"
    # 常见凭据文件：不能靠“扩展名是 .env”来判断敏感与否
    ".netrc,.npmrc,.pypirc,.git-credentials,"
    "credentials,**/credentials,credentials.json,**/credentials.json,"
    "*service-account*.json,*service_account*.json,"
    ".aws/**,**/.aws/**,.kube/**,**/.kube/**,"
    ".config/gcloud/**,**/.config/gcloud/**,"
    ".docker/config.json,**/.docker/config.json,"
    "*.tfstate,*.tfstate.backup,*.jks,*.keystore,*.gpg"
)

# 这些 profile 面向生产/只读审计：命令变更一律拒绝，写能力也被强制关闭。
PRODUCTION_PROFILES = frozenset({"production", "ops"})


class _NonEmptyDotEnvSource(PydanticBaseSettingsSource):
    """给 `.env` 源加一层空值过滤：`KEY=` 这种占位不算「配置过」。

    否则从 `.env.example` 复制来的空行会盖掉环境变量里真正可用的值 ——
    那正是这套优先级最容易被吐槽的地方。

    **委托**而不是重建：`Settings(_env_file=None)` 这类显式覆盖必须继续生效，
    重建一个源会把它无视掉，测试里大量依赖这一点。
    """

    def __init__(self, inner: PydanticBaseSettingsSource) -> None:
        super().__init__(inner.settings_cls)
        self._inner = inner

    def get_field_value(self, field: Any, field_name: str) -> tuple[Any, str, bool]:
        return None, field_name, False  # 不使用：取值全部委托给 _inner

    def __call__(self) -> dict[str, Any]:
        data = self._inner()
        return {k: v for k, v in data.items() if v not in ("", None)}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: Any,
        env_settings: Any,
        dotenv_settings: Any,
        file_secret_settings: Any,
    ) -> tuple[Any, ...]:
        # 构造函数参数仍然最高优先 —— 测试与嵌入式用法依赖这一点
        return (
            init_settings,
            _NonEmptyDotEnvSource(dotenv_settings),
            env_settings,
            file_secret_settings,
        )

    # ---------- DeepSeek ----------
    deepseek_api_key: str = Field(default="", alias="DEEPSEEK_API_KEY")
    deepseek_base_url: str = Field(default="https://api.deepseek.com", alias="DEEPSEEK_BASE_URL")
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")
    # 可选多 provider JSON；空 = 只用 DEEPSEEK_* 单 provider。
    # [{"name":"deepseek","base_url":"...","api_key":"...","model":"...",
    #   "priority":0}, {"name":"backup","api_key_env":"BACKUP_API_KEY",...}]
    llm_providers: str = Field(default="", alias="AGENT_LLM_PROVIDERS")
    # 非 429 错误是否也尝试下一个 provider。
    llm_fallback_on_error: bool = Field(
        default=True, alias="AGENT_LLM_FALLBACK_ON_ERROR"
    )
    # 所有 provider 都在冷却时，单次调用最多等待多久再放弃。
    llm_provider_max_wait_s: float = Field(
        default=30.0, alias="AGENT_LLM_PROVIDER_MAX_WAIT_S"
    )
    # 节点级瞬时错误重试（超时/连接重置/5xx/429/环境抖动）。
    # attempts 是**总尝试次数**；1 表示不重试。退避为指数 + 抖动，
    # 且整次重试累计等待不超过 max_total_wait，避免长时间卡住。
    retry_attempts: int = Field(default=3, alias="AGENT_RETRY_ATTEMPTS")
    retry_base_delay_s: float = Field(
        default=1.0, alias="AGENT_RETRY_BASE_DELAY_S"
    )
    retry_max_delay_s: float = Field(
        default=30.0, alias="AGENT_RETRY_MAX_DELAY_S"
    )
    retry_max_total_wait_s: float = Field(
        default=60.0, alias="AGENT_RETRY_MAX_TOTAL_WAIT_S"
    )
    # cost（按 provider 单价排序）| priority（按配置顺序）
    llm_routing_strategy: str = Field(
        default="cost", alias="AGENT_LLM_ROUTING_STRATEGY"
    )
    # 单次 run 的成本预算（USD）；0 = 不告警
    run_cost_budget_usd: float = Field(
        default=0.0, alias="AGENT_RUN_COST_BUDGET_USD"
    )
    # 整个会话（同一 thread，多轮 run 累计）的成本预算（USD）；0 = 不限制。
    # 全新 run 在开始前检查；resume 不拦截，避免挂起的审批被预算锁死。
    session_cost_budget_usd: float = Field(
        default=0.0, alias="AGENT_SESSION_COST_BUDGET_USD"
    )
    # 达到预算的该比例时发出告警（0~1）
    cost_alert_ratio: float = Field(
        default=0.8, alias="AGENT_COST_ALERT_RATIO"
    )
    # 是否在 run 结束时拉取 provider 账单做对账；默认关闭避免额外网络请求。
    billing_enabled: bool = Field(default=False, alias="AGENT_BILLING_ENABLED")
    billing_timeout_s: float = Field(
        default=10.0, alias="AGENT_BILLING_TIMEOUT_S"
    )

    # ---------- LangSmith ----------
    langsmith_tracing: bool = Field(default=False, alias="LANGSMITH_TRACING")
    langsmith_api_key: str = Field(default="", alias="LANGSMITH_API_KEY")
    langsmith_project: str = Field(default="coding-agent", alias="LANGSMITH_PROJECT")

    # ---------- WSL2 沙箱 ----------
    wsl_distro: str = Field(default="Ubuntu", alias="AGENT_WSL_DISTRO")
    # 留空表示自动使用沙箱内 $HOME/agent-ws，避免与真实用户名不匹配
    wsl_workspace: str = Field(default="", alias="AGENT_WSL_WORKSPACE")
    # auto：Windows 用 wsl.exe，Linux/WSL 内直接本地执行
    # local：强制本地 Linux 执行；wsl：强制 wsl.exe（兼容旧 Host 模式）
    execution_backend: str = Field(
        default="auto", alias="AGENT_EXECUTION_BACKEND"
    )

    # ---------- A2L 宿主能力代理（Phase 1，默认关闭） ----------
    # A2L 绑定客户端生命周期：随客户端启动/退出，不注册系统服务。
    host_broker_enabled: bool = Field(
        default=False, alias="AGENT_HOST_BROKER_ENABLED"
    )
    host_broker_url: str = Field(default="", alias="AGENT_HOST_BROKER_URL")
    host_broker_listen_host: str = Field(
        default="127.0.0.1", alias="AGENT_HOST_BROKER_LISTEN_HOST"
    )
    host_broker_listen_port: int = Field(
        default=8791, alias="AGENT_HOST_BROKER_LISTEN_PORT"
    )
    host_workspace_id: str = Field(
        default="", alias="AGENT_HOST_WORKSPACE_ID"
    )
    host_root_handle: str = Field(
        default="", alias="AGENT_HOST_ROOT_HANDLE"
    )
    host_a2l_id: str = Field(
        default="a2l-local", alias="AGENT_HOST_A2L_ID"
    )
    # lowpriv 是 P1 默认；appcontainer 仅受限只读；user-t1 仅 local-dev。
    host_broker_security_tier: str = Field(
        default="lowpriv", alias="AGENT_HOST_BROKER_SECURITY_TIER"
    )
    host_broker_account: str = Field(
        default="", alias="AGENT_HOST_BROKER_ACCOUNT"
    )
    host_broker_device_cert: str = Field(
        default="", alias="AGENT_HOST_BROKER_DEVICE_CERT"
    )
    host_broker_device_key: str = Field(
        default="", alias="AGENT_HOST_BROKER_DEVICE_KEY"
    )
    # 企业设备台账（A3 本地 JSON）；off = 兼容旧的单设备/自洽校验。
    host_broker_device_registry: str = Field(
        default="", alias="AGENT_HOST_BROKER_DEVICE_REGISTRY"
    )
    host_broker_device_policy: str = Field(
        default="off", alias="AGENT_HOST_BROKER_DEVICE_POLICY"
    )
    host_broker_device_reload_s: int = Field(
        default=2, alias="AGENT_HOST_BROKER_DEVICE_RELOAD_S"
    )
    # A2L 本地验签的运行策略。
    host_broker_policy_file: str = Field(
        default="", alias="AGENT_HOST_BROKER_POLICY_FILE"
    )
    host_broker_policy_pubkey: str = Field(
        default="", alias="AGENT_HOST_BROKER_POLICY_PUBKEY"
    )
    host_broker_policy_pubkey_file: str = Field(
        default="", alias="AGENT_HOST_BROKER_POLICY_PUBKEY_FILE"
    )
    host_broker_attestation: str = Field(
        default="off", alias="AGENT_HOST_BROKER_ATTESTATION"
    )
    host_broker_attestation_key: str = Field(
        default="", alias="AGENT_HOST_BROKER_ATTESTATION_KEY"
    )
    host_broker_tls_ca: str = Field(
        default="", alias="AGENT_HOST_BROKER_TLS_CA"
    )
    host_broker_tls_cert: str = Field(
        default="", alias="AGENT_HOST_BROKER_TLS_CERT"
    )
    host_broker_tls_key: str = Field(
        default="", alias="AGENT_HOST_BROKER_TLS_KEY"
    )
    host_broker_tls_server_name: str = Field(
        default="", alias="AGENT_HOST_BROKER_TLS_SERVER_NAME"
    )
    host_broker_require_tls: bool = Field(
        default=False, alias="AGENT_HOST_BROKER_REQUIRE_TLS"
    )
    host_broker_heartbeat_s: int = Field(
        default=15, alias="AGENT_HOST_BROKER_HEARTBEAT_S"
    )
    host_broker_autostart: bool = Field(
        default=True, alias="AGENT_HOST_BROKER_AUTOSTART"
    )
    host_broker_paused: bool = Field(
        default=False, alias="AGENT_HOST_BROKER_PAUSED"
    )
    host_broker_store: str = Field(
        default="", alias="AGENT_HOST_BROKER_STORE"
    )
    # 桌面端本机授权桥：A2L 把 grant.request 发到 A2，由聊天界面按钮决定。
    consent_bridge_token: str = Field(
        default="", alias="AGENT_CONSENT_TOKEN"
    )
    # 默认 sandbox 保持旧行为；桌面启动器显式设为 host。
    workspace_kind_default: str = Field(
        default="sandbox", alias="AGENT_WORKSPACE_KIND_DEFAULT"
    )
    grants_enabled: bool = Field(
        default=False, alias="AGENT_GRANTS_ENABLED"
    )
    host_deny_roots: str = Field(default="", alias="AGENT_HOST_DENY_ROOTS")
    host_root_policy: str = Field(
        default="local_ntfs_only", alias="AGENT_HOST_ROOT_POLICY"
    )
    host_sensitive_roots: str = Field(
        default="", alias="AGENT_HOST_SENSITIVE_ROOTS"
    )
    host_toolchain_roots: str = Field(
        default="", alias="AGENT_HOST_TOOLCHAIN_ROOTS"
    )
    # P1/P2：host 写与受控 exec 默认关闭；安装永远只返回人工指令。
    host_broker_allow_write: bool = Field(
        default=False, alias="AGENT_HOST_BROKER_ALLOW_WRITE"
    )
    host_broker_exec_enabled: bool = Field(
        default=False, alias="AGENT_HOST_BROKER_EXEC_ENABLED"
    )
    # JSON 数组；每项 {id,kind,root,commands,output_roots}。root 不下发模型。
    host_broker_toolchains: str = Field(
        default="", alias="AGENT_HOST_BROKER_TOOLCHAINS"
    )
    host_snapshot_dir: str = Field(
        default="", alias="AGENT_HOST_SNAPSHOT_DIR"
    )
    # shell 隔离后端：off（默认，仅工作区 cwd 约束）| bwrap（挂载命名空间隔离）
    # bwrap 模式下若沙箱内不可用，会**拒绝执行**（fail closed），不静默降级
    shell_sandbox: str = Field(default="off", alias="AGENT_SHELL_SANDBOX")
    # WSL 命令默认 `env -i` 清空宿主环境。这里按名称显式放行少量构建变量
    # （如 JAVA_HOME/GOPATH/VIRTUAL_ENV）。名称含
    # KEY/TOKEN/SECRET/PASSWORD/CREDENTIAL/AUTH/COOKIE/SESSION 的一律忽略。
    shell_env_allowlist: str = Field(
        default="", alias="AGENT_SHELL_ENV_ALLOWLIST"
    )
    # 仅 bwrap 模式生效：false 时加 --unshare-net，命令完全无网络。
    # off 模式没有内核级隔离，词法策略无法禁止 curl/wget 出网。
    shell_network: bool = Field(default=True, alias="AGENT_SHELL_NETWORK")
    # 常驻 WSL bash 会话池。默认开启：实测读/检索从 ~170ms 降到 ~20ms。
    # 会话异常时会自动退回逐命令 wsl.exe，因此可安全地作为默认值。
    persistent_shell: bool = Field(default=True, alias="AGENT_PERSISTENT_SHELL")
    persistent_shell_pool: int = Field(
        default=4, alias="AGENT_PERSISTENT_SHELL_POOL"
    )
    shell_timeout: int = Field(default=60, alias="AGENT_SHELL_TIMEOUT")
    # WSL 健康检查与熔断。连续失败达到阈值后，短时间内直接 fail fast，
    # 避免每个工具调用都去拉起一个注定失败的 wsl.exe；冷却后放一个探测请求。
    wsl_health_check_enabled: bool = Field(
        default=True, alias="AGENT_WSL_HEALTH_CHECK"
    )
    wsl_health_ttl_s: float = Field(
        default=30.0, alias="AGENT_WSL_HEALTH_TTL_S"
    )
    wsl_health_timeout_s: int = Field(
        default=10, alias="AGENT_WSL_HEALTH_TIMEOUT_S"
    )
    wsl_breaker_failures: int = Field(
        default=3, alias="AGENT_WSL_BREAKER_FAILURES"
    )
    wsl_breaker_cooldown_s: float = Field(
        default=15.0, alias="AGENT_WSL_BREAKER_COOLDOWN_S"
    )
    wsl_breaker_max_cooldown_s: float = Field(
        default=60.0, alias="AGENT_WSL_BREAKER_MAX_COOLDOWN_S"
    )
    # 自动验证的独立超时。测试/构建通常比普通 shell 命令长，不能共用 60s；
    # 0 表示回退到 shell_timeout。
    verify_timeout: int = Field(default=300, alias="AGENT_VERIFY_TIMEOUT")
    # 追加的内容级写保护 glob（逗号分隔）；默认规则始终生效。
    write_deny_globs: str = Field(default="", alias="AGENT_WRITE_DENY_GLOBS")
    # repo map 注入 planner 的字符预算（0 = 关闭）。
    repo_map_chars: int = Field(default=4_000, alias="AGENT_REPO_MAP_CHARS")
    # 可选 language server 命令；空 = 不启用（symbol_search 仍用 AST）。
    lsp_command: str = Field(default="", alias="AGENT_LSP_COMMAND")
    lsp_timeout: float = Field(default=10.0, alias="AGENT_LSP_TIMEOUT")
    # pull 诊断不可用时，等待 publishDiagnostics 的最长时间。
    lsp_diagnostics_wait_s: float = Field(
        default=2.0, alias="AGENT_LSP_DIAGNOSTICS_WAIT_S"
    )
    # 沙箱资源上限，0 表示不限制
    shell_cpu_seconds: int = Field(default=600, alias="AGENT_SHELL_CPU_SECONDS")
    shell_memory_mb: int = Field(default=0, alias="AGENT_SHELL_MEMORY_MB")
    shell_max_file_mb: int = Field(default=512, alias="AGENT_SHELL_MAX_FILE_MB")
    shell_max_processes: int = Field(default=1024, alias="AGENT_SHELL_MAX_PROCESSES")
    max_output_chars: int = Field(default=20_000, alias="AGENT_MAX_OUTPUT_CHARS")
    # 宿主从沙箱捕获单条命令输出的字节上限（stdout/stderr 各自）。
    # `max_output_chars` 只控制回灌给模型的文本；捕获阶段若不设上限，
    # 一条 `yes`/`cat big.log` 会在截断前先把宿主内存打满。
    max_capture_bytes: int = Field(
        default=1_000_000, alias="AGENT_MAX_CAPTURE_BYTES"
    )
    max_file_read_bytes: int = Field(default=2_000_000, alias="AGENT_MAX_FILE_READ_BYTES")
    # 文档解析上限。解析在宿主进程完成，不进入 WSL 沙箱。
    document_max_bytes: int = Field(
        default=20_971_520, alias="AGENT_DOCUMENT_MAX_BYTES"
    )
    document_max_pages: int = Field(
        default=200, alias="AGENT_DOCUMENT_MAX_PAGES"
    )
    document_max_cells: int = Field(
        default=20_000, alias="AGENT_DOCUMENT_MAX_CELLS"
    )
    document_max_chars: int = Field(
        default=2_000_000, alias="AGENT_DOCUMENT_MAX_CHARS"
    )
    # 回滚开关：关闭后 document_pipeline 不再被自动选择。
    document_pipeline_enabled: bool = Field(
        default=True, alias="AGENT_DOCUMENT_PIPELINE_ENABLED"
    )
    document_cache_enabled: bool = Field(
        default=True, alias="AGENT_DOCUMENT_CACHE"
    )
    document_cache_max_bytes: int = Field(
        default=512 * 1024 * 1024, alias="AGENT_DOCUMENT_CACHE_MAX_BYTES"
    )
    document_cache_ttl_days: int = Field(
        default=30, alias="AGENT_DOCUMENT_CACHE_TTL_DAYS"
    )
    # CPU 密集/不可取消的文档解析（PDF/Office/EPUB）默认放进 spawn 子进程；
    # 超时直接杀进程，避免把宿主线程永久挂住。短文本仍走当前线程。
    document_process_isolation: bool = Field(
        default=True, alias="AGENT_DOCUMENT_PROCESS_ISOLATION"
    )
    document_process_timeout_s: float = Field(
        default=60.0, alias="AGENT_DOCUMENT_PROCESS_TIMEOUT_S"
    )
    # 超过该大小的任意文档也走进程隔离；0 = 只按格式判断。
    document_process_threshold_bytes: int = Field(
        default=2_000_000, alias="AGENT_DOCUMENT_PROCESS_THRESHOLD_BYTES"
    )
    process_runner_max_concurrent: int = Field(
        default=2, alias="AGENT_PROCESS_RUNNER_MAX_CONCURRENT"
    )
    # 文档管线角色级并发；author 固定为 1（报告写入全局单实例）。
    orchestrator_document_concurrency: int = Field(
        default=2, alias="AGENT_ORCHESTRATOR_DOCUMENT_CONCURRENCY"
    )
    orchestrator_vision_concurrency: int = Field(
        default=2, alias="AGENT_ORCHESTRATOR_VISION_CONCURRENCY"
    )
    orchestrator_author_concurrency: int = Field(
        default=1, alias="AGENT_ORCHESTRATOR_AUTHOR_CONCURRENCY"
    )
    # 单次文档管线的输入上限。
    orchestrator_document_max_docs: int = Field(
        default=10, alias="AGENT_DOCUMENT_PIPELINE_MAX_DOCS"
    )
    orchestrator_document_max_images: int = Field(
        default=20, alias="AGENT_DOCUMENT_PIPELINE_MAX_IMAGES"
    )
    # 每个 document 任务完成后触发一次 steering/checkpoint 边界。
    orchestrator_document_checkpoint: bool = Field(
        default=True, alias="AGENT_ORCHESTRATOR_DOCUMENT_CHECKPOINT"
    )
    # 长文档分段读取：每次最多多少页、最多几段。
    document_read_pages_per_call: int = Field(
        default=5, alias="AGENT_DOCUMENT_READ_PAGES_PER_CALL"
    )
    document_read_max_calls: int = Field(
        default=3, alias="AGENT_DOCUMENT_READ_MAX_CALLS"
    )
    # document_edit 单次调用最多执行多少条操作。
    document_edit_max_ops: int = Field(
        default=50, alias="AGENT_DOCUMENT_EDIT_MAX_OPS"
    )
    # 依赖任务注入下游 worker 的上下文总字符上限。
    orchestrator_dependency_context_chars: int = Field(
        default=12_000, alias="AGENT_ORCHESTRATOR_DEPENDENCY_CONTEXT_CHARS"
    )
    # 默认只处理文本型 PDF；扫描页不自动 OCR。
    pdf_text_only: bool = Field(default=True, alias="AGENT_PDF_TEXT_ONLY")
    document_ocr_enabled: bool = Field(
        default=False, alias="AGENT_DOCUMENT_OCR_ENABLED"
    )
    pdf_extract_metadata: bool = Field(
        default=True, alias="AGENT_PDF_EXTRACT_METADATA"
    )
    pdf_extract_outline: bool = Field(
        default=True, alias="AGENT_PDF_EXTRACT_OUTLINE"
    )
    pdf_extract_annotations: bool = Field(
        default=True, alias="AGENT_PDF_EXTRACT_ANNOTATIONS"
    )
    pdf_extract_forms: bool = Field(
        default=True, alias="AGENT_PDF_EXTRACT_FORMS"
    )
    pdf_extract_attachments: bool = Field(
        default=True, alias="AGENT_PDF_EXTRACT_ATTACHMENTS"
    )
    pdf_extract_tables: bool = Field(
        default=True, alias="AGENT_PDF_EXTRACT_TABLES"
    )
    # 扫描页交给视觉模型的上限。
    document_ocr_max_pages: int = Field(
        default=5, alias="AGENT_DOCUMENT_OCR_MAX_PAGES"
    )
    document_image_max_items: int = Field(
        default=5, alias="AGENT_DOCUMENT_IMAGE_MAX_ITEMS"
    )
    # 角色级累计 token 硬预算；0 = 不限制。文档/报告是单次成本最高的角色，
    # 给保守默认值，避免一份大文档或一轮报告无限消耗；超限只阻塞该角色。
    orchestrator_document_token_budget: int = Field(
        default=120_000, alias="AGENT_ORCHESTRATOR_DOCUMENT_TOKEN_BUDGET"
    )
    orchestrator_vision_token_budget: int = Field(
        default=60_000, alias="AGENT_ORCHESTRATOR_VISION_TOKEN_BUDGET"
    )
    orchestrator_author_token_budget: int = Field(
        default=60_000, alias="AGENT_ORCHESTRATOR_AUTHOR_TOKEN_BUDGET"
    )
    # 图片理解上限；默认 LLM 视为多模态模型。
    image_max_bytes: int = Field(
        default=10_485_760, alias="AGENT_IMAGE_MAX_BYTES"
    )
    image_max_dimension: int = Field(
        default=2048, alias="AGENT_IMAGE_MAX_DIMENSION"
    )
    # 报告输出：默认只允许写到报告目录；显式路径由 planner 解析后仍受此约束。
    report_output_dir: str = Field(
        default=".agent/artifacts/reports", alias="AGENT_REPORT_OUTPUT_DIR"
    )
    report_font_path: str = Field(
        default="", alias="AGENT_REPORT_FONT_PATH"
    )
    report_allow_any_path: bool = Field(
        default=False, alias="AGENT_REPORT_ALLOW_ANY_PATH"
    )
    report_llm_review: bool = Field(
        default=False, alias="AGENT_REPORT_LLM_REVIEW"
    )
    # 同时启动的 wsl.exe 进程上限。并发 worker 会各自调用沙箱，没有这层
    # 背压时 4 个 worker 的一次工具轮次就能同时拉起十几个 WSL 进程。
    sandbox_max_concurrent_commands: int = Field(
        default=8, alias="AGENT_SANDBOX_MAX_CONCURRENT_COMMANDS"
    )

    # ---------- 统一并发治理 ----------
    # 0 = 回退到旧的单项配置（orchestrator_max_concurrent_workers /
    # sandbox_max_concurrent_commands / max_concurrent_tools）。
    concurrency_workers: int = Field(
        default=0, alias="AGENT_CONCURRENCY_WORKERS"
    )
    concurrency_tools: int = Field(
        default=0, alias="AGENT_CONCURRENCY_TOOLS"
    )
    concurrency_wsl: int = Field(default=0, alias="AGENT_CONCURRENCY_WSL")
    # 给健康探针/短探测保留的 WSL 槽位；普通命令不能用。
    concurrency_wsl_reserve: int = Field(
        default=1, alias="AGENT_CONCURRENCY_WSL_RESERVE"
    )
    # 基于命令 p95 延迟的弹性并发：高延迟降并发，低延迟逐步恢复。
    concurrency_wsl_adaptive: bool = Field(
        default=True, alias="AGENT_CONCURRENCY_WSL_ADAPTIVE"
    )
    concurrency_wsl_min: int = Field(
        default=2, alias="AGENT_CONCURRENCY_WSL_MIN"
    )
    concurrency_wsl_max: int = Field(
        default=0, alias="AGENT_CONCURRENCY_WSL_MAX"
    )
    concurrency_wsl_target_ms: int = Field(
        default=1500, alias="AGENT_CONCURRENCY_WSL_TARGET_MS"
    )
    concurrency_wsl_latency_window: int = Field(
        default=20, alias="AGENT_CONCURRENCY_WSL_LATENCY_WINDOW"
    )
    concurrency_llm_per_provider: int = Field(
        default=4, alias="AGENT_CONCURRENCY_LLM_PER_PROVIDER"
    )
    concurrency_llm_global: int = Field(
        default=6, alias="AGENT_CONCURRENCY_LLM_GLOBAL"
    )
    # 异步池等待队列上限；0 = 不限制（仅测试/兼容用）。生产默认有界，
    # 队列满时快速失败，而不是把等待者无限堆进内存。
    concurrency_max_waiters: int = Field(
        default=128, alias="AGENT_CONCURRENCY_MAX_WAITERS"
    )
    concurrency_worker_timeout_s: float = Field(
        default=60.0, alias="AGENT_CONCURRENCY_WORKER_TIMEOUT_S"
    )
    concurrency_llm_timeout_s: float = Field(
        default=30.0, alias="AGENT_CONCURRENCY_LLM_TIMEOUT_S"
    )
    concurrency_wsl_timeout_s: float = Field(
        default=30.0, alias="AGENT_CONCURRENCY_WSL_TIMEOUT_S"
    )
    # LLM 每个 provider 的自适应并发范围。
    llm_aimd_min: int = Field(default=1, alias="AGENT_LLM_AIMD_MIN")
    llm_aimd_max: int = Field(default=8, alias="AGENT_LLM_AIMD_MAX")

    # ---------- 上下文裁剪 ----------
    # 0 表示不裁剪；这些值作用于发给模型的历史，不改写会话状态
    context_max_chars: int = Field(default=60_000, alias="AGENT_CONTEXT_MAX_CHARS")
    context_max_tokens: int = Field(default=0, alias="AGENT_CONTEXT_MAX_TOKENS")
    context_keep_recent: int = Field(default=12, alias="AGENT_CONTEXT_KEEP_RECENT")
    context_tool_chars: int = Field(default=1_500, alias="AGENT_CONTEXT_TOOL_CHARS")
    # run 级文件内容缓存的字节上限（LRU）。0 = 关闭缓存。
    read_cache_max_bytes: int = Field(
        default=64 * 1024 * 1024, alias="AGENT_READ_CACHE_MAX_BYTES"
    )
    # AST 符号索引缓存时间；写操作不会主动失效，过期后自动重建。
    symbol_index_ttl_s: float = Field(
        default=10.0, alias="AGENT_SYMBOL_INDEX_TTL_S"
    )
    # 包管理器探测缓存时间；安装后会主动失效。
    deps_detect_ttl_s: float = Field(
        default=30.0, alias="AGENT_DEPS_DETECT_TTL_S"
    )
    # 每次运行注入系统提示的长期记忆条数（按与请求的相关度取前 K 条）。
    # 0 表示不限制，整份注入 —— 记忆上限 50 条，但每条都会进入每一轮 system prompt。
    memory_top_k: int = Field(default=8, alias="AGENT_MEMORY_TOP_K")
    # 是否发现并注入仓库内的 AGENTS.md / CLAUDE.md（分层项目指令）。
    instructions_enabled: bool = Field(
        default=True, alias="AGENT_INSTRUCTIONS_ENABLED"
    )
    # 编排子图/worker 的会话约束摘要上限（字符）。独立子图不复制 messages，
    # 这里从 checkpoint 抽最近若干轮压缩后注入；0 表示关闭。
    session_brief_chars: int = Field(
        default=4_000, alias="AGENT_SESSION_BRIEF_CHARS"
    )

    # ---------- 验证 ----------
    verify_enabled: bool = Field(default=True, alias="AGENT_VERIFY_ENABLED")
    # 多步计划里，非最终的 implement 步不跑全量测试：早期步骤只完成了一部分，
    # 全量套件必然因为后面还没做的步骤而失败，会把 agent 卡死在这一步。
    # 只在 kind=verify 或最后一步验证；单步任务不受影响。
    verify_only_final: bool = Field(default=True, alias="AGENT_VERIFY_ONLY_FINAL")
    # 留空表示按项目清单自动识别（pytest / cargo / go / npm / make）
    verify_command: str = Field(default="", alias="AGENT_VERIFY_COMMAND")

    # 「一步没有任何改动」时是否给它一次重做的机会。
    #
    # 背景：`dirty=False` 时 verify 与 review 都会短路，控制流直接 advance ——
    # 于是「这一步只读了文件」与「这一步做完了」在图上**无法区分**。实测过一整批
    # 长程任务因此把计划"读"完就算完成：10 次运行里 5 次一次写都没尝试过。
    #
    # 默认开启，但**必须可关**：它会改变所有任务的行为，现有基线因此不再可比
    # （评测的 baseline 里记了这个开关）。
    nudge_empty_steps: bool = Field(default=True, alias="AGENT_NUDGE_EMPTY_STEPS")

    # ---------- 代码审查 ----------
    # 一步做完、验证通过后，对本次改动跑一遍确定性静态检查（`tools/review.py`）。
    # 与自动 verify 同一口径：不过审批，所以只跑宿主写死的检查，不读工作区里的
    # 任何"配置"。关掉它等于去掉「测试通过 ≠ 代码正确」那第二道关。
    review_enabled: bool = Field(default=True, alias="AGENT_REVIEW_ENABLED")
    # 探测到的 linter/formatter 对**本次改动文件**的发现是否阻断。
    # 只把改动文件提升为 blocking：仓库里已有的历史问题不该每一步都卡死。
    review_lint_blocking: bool = Field(
        default=True, alias="AGENT_REVIEW_LINT_BLOCKING"
    )

    # ---------- 子智能体（Phase 1：只读 Explore）----------
    # **默认关闭**：它会给主模型多一个工具、改变 agent 行为，打开后旧评测基线
    # 不再可比。评测时显式设置 AGENT_EXPLORE_ENABLED=true 做 A/B。
    explore_enabled: bool = Field(default=False, alias="AGENT_EXPLORE_ENABLED")
    # 子智能体最多几轮「模型 → 工具」；到顶后强制生成报告，状态记 partial。
    # 定位一个跨文件功能通常需要 6–10 轮只读检索；6 轮实测会频繁 partial。
    explore_max_rounds: int = Field(default=8, alias="AGENT_EXPLORE_MAX_ROUNDS")
    # 整个子智能体的墙钟上限（秒）
    explore_timeout: int = Field(default=120, alias="AGENT_EXPLORE_TIMEOUT")
    # 返回给主模型的摘要上限（字符）
    explore_max_report_chars: int = Field(default=4_000, alias="AGENT_EXPLORE_MAX_REPORT_CHARS")
    # 单个任务最多委派几次探索 —— 与主 agent 的工具轮次预算相互独立。
    # 默认给到 6：提示词鼓励把互不依赖的区域拆成多个并发 delegate，
    # 预算若只有 2 会与这条建议直接冲突（实测 5 个里 3 个被预算拒绝）。
    max_delegate_calls: int = Field(default=6, alias="AGENT_MAX_DELEGATE_CALLS")
    # 同一条消息里的多个 delegate 调用最多同时跑几个。
    # 子智能体只读，所以并发不会产生写冲突；限制主要是控制成本与 wsl.exe 压力。
    max_concurrent_agents: int = Field(default=4, alias="AGENT_MAX_CONCURRENT_AGENTS")
    # 同一条消息里的多个只读工具最多同时跑几个；写工具始终串行。
    max_concurrent_tools: int = Field(
        default=4, alias="AGENT_MAX_CONCURRENT_TOOLS"
    )
    # ---- 成本感知的委派预算（按仓库文件数分级）----
    # 小于 min_files：不委派，直接读；[min, medium)：medium_calls；
    # [medium, large)：large_calls；>= large：max_delegate_calls。
    delegate_min_files: int = Field(default=30, alias="AGENT_DELEGATE_MIN_FILES")
    delegate_medium_files: int = Field(default=100, alias="AGENT_DELEGATE_MEDIUM_FILES")
    delegate_large_files: int = Field(default=300, alias="AGENT_DELEGATE_LARGE_FILES")
    delegate_medium_calls: int = Field(default=2, alias="AGENT_DELEGATE_MEDIUM_CALLS")
    delegate_large_calls: int = Field(default=4, alias="AGENT_DELEGATE_LARGE_CALLS")

    # ---------- 深层工作流（RFC-0009，默认关闭）----------
    # 总开关：关闭时下列能力全部不生效，行为与既有四模板一致。
    deep_workflows_enabled: bool = Field(
        default=False, alias="AGENT_DEEP_WORKFLOWS_ENABLED"
    )
    knowledge_artifacts_enabled: bool = Field(
        default=False, alias="AGENT_KNOWLEDGE_ARTIFACTS_ENABLED"
    )
    # 长文档：取消固定 3 次读取；覆盖范围由页数 + document_max_pages 决定，
    # 未读完时报告 partial + 未读区间。
    document_full_coverage: bool = Field(
        default=False, alias="AGENT_DOCUMENT_FULL_COVERAGE"
    )
    # 长文档：按 chunk 分批抽取 claim，再合并；避免整篇塞入单次调用。
    document_map_reduce: bool = Field(
        default=False, alias="AGENT_DOCUMENT_MAP_REDUCE"
    )
    document_chunk_chars: int = Field(
        default=6_000, alias="AGENT_DOCUMENT_CHUNK_CHARS"
    )
    document_map_concurrency: int = Field(
        default=3, alias="AGENT_DOCUMENT_MAP_CONCURRENCY"
    )
    # 每篇文档提炼固定槽位（问题/方法/算法/实验/局限/创新/结论），
    # 供跨文档矩阵与 author 使用；默认关闭（每篇多一次结构化调用），
    # 长文档/报告场景显式开启。
    document_matrix_enabled: bool = Field(
        default=False, alias="AGENT_DOCUMENT_MATRIX_ENABLED"
    )
    # 分层归并：每层最多合并多少节点，以及单次归并的输入字符上限。
    document_tree_fanout: int = Field(
        default=5, ge=2, le=20, alias="AGENT_DOCUMENT_TREE_FANOUT"
    )
    document_tree_input_chars: int = Field(
        default=16_000, ge=2_000, alias="AGENT_DOCUMENT_TREE_INPUT_CHARS"
    )
    # author 的证据上下文：优先取 claim + quote，而不是只取 summary。
    author_evidence_top_k: int = Field(
        default=12, alias="AGENT_AUTHOR_EVIDENCE_TOP_K"
    )
    author_evidence_max_chars: int = Field(
        default=24_000, alias="AGENT_AUTHOR_EVIDENCE_MAX_CHARS"
    )
    # 代码架构：从 PythonIndex 生成 ArchIndex/ArchBrief 并注入规划提示。
    arch_workflow_enabled: bool = Field(
        default=False, alias="AGENT_ARCH_WORKFLOW_ENABLED"
    )
    arch_impact_max_modules: int = Field(
        default=40, alias="AGENT_ARCH_IMPACT_MAX_MODULES"
    )
    # 架构索引抽样上限与增量缓存。
    arch_max_files: int = Field(
        default=2_000, alias="AGENT_ARCH_MAX_FILES"
    )
    arch_max_file_bytes: int = Field(
        default=1_000_000, alias="AGENT_ARCH_MAX_FILE_BYTES"
    )
    arch_max_total_bytes: int = Field(
        default=32_000_000, alias="AGENT_ARCH_MAX_TOTAL_BYTES"
    )
    arch_incremental_index: bool = Field(
        default=True, alias="AGENT_ARCH_INCREMENTAL_INDEX"
    )
    # 空 = 宿主 ~/.codingagent/index-cache；可指向共享盘实现跨机器复用。
    arch_index_cache_dir: str = Field(
        default="", alias="AGENT_ARCH_INDEX_CACHE_DIR"
    )
    arch_index_cache_max_entries: int = Field(
        default=20_000, alias="AGENT_ARCH_INDEX_CACHE_MAX_ENTRIES"
    )
    arch_index_cache_max_bytes: int = Field(
        default=536_870_912, alias="AGENT_ARCH_INDEX_CACHE_MAX_BYTES"
    )
    arch_index_cache_ttl_days: int = Field(
        default=30, alias="AGENT_ARCH_INDEX_CACHE_TTL_DAYS"
    )
    # 共享缓存跨进程锁：等待上限与 stale 判定（秒）。
    arch_index_cache_lock_timeout_s: float = Field(
        default=5.0, alias="AGENT_ARCH_INDEX_CACHE_LOCK_TIMEOUT_S"
    )
    arch_index_cache_lock_stale_s: float = Field(
        default=60.0, alias="AGENT_ARCH_INDEX_CACHE_LOCK_STALE_S"
    )
    # 可选：用 LSP definition 解析动态调用（需要 AGENT_LSP_COMMAND）。
    arch_lsp_resolution_enabled: bool = Field(
        default=False, alias="AGENT_ARCH_LSP_RESOLUTION_ENABLED"
    )
    arch_lsp_max_calls: int = Field(
        default=50, alias="AGENT_ARCH_LSP_MAX_CALLS"
    )
    # 文档 judge 的通过阈值；可用 scripts/calibrate_judge.py 在标注集上调。
    document_judge_pass_score: int = Field(
        default=60, alias="AGENT_DOCUMENT_JUDGE_PASS_SCORE"
    )
    # 知识检索模式：lexical（默认，零依赖）| vector | hybrid。
    knowledge_search_mode: str = Field(
        default="lexical", alias="AGENT_KNOWLEDGE_SEARCH_MODE"
    )
    # 空串 = 继承 memory_embedding_provider；支持 hash/local/remote/none。
    # remote 仍受 memory_allow_remote_embeddings + base_url/key 双重门禁。
    knowledge_embedding_provider: str = Field(
        default="", alias="AGENT_KNOWLEDGE_EMBEDDING_PROVIDER"
    )
    # 0 = 继承 memory_embedding_dim。
    knowledge_embedding_dim: int = Field(
        default=0, alias="AGENT_KNOWLEDGE_EMBEDDING_DIM"
    )

    # ---------- 资料库（每用户跨工作区）----------
    # 默认关闭；开启后只暴露 library_search 只读工具。
    library_enabled: bool = Field(
        default=False, alias="AGENT_LIBRARY_ENABLED"
    )
    # WSL 内绝对路径；空则按沙箱 $HOME/.agent-library 推导。
    # Web 多用户由 TenancyManager 覆盖成 per-user 目录。
    library_root: str = Field(default="", alias="AGENT_LIBRARY_ROOT")
    library_max_items: int = Field(
        default=100, alias="AGENT_LIBRARY_MAX_ITEMS"
    )
    library_max_item_bytes: int = Field(
        default=20_971_520, alias="AGENT_LIBRARY_MAX_ITEM_BYTES"
    )
    library_max_total_bytes: int = Field(
        default=268_435_456, alias="AGENT_LIBRARY_MAX_TOTAL_BYTES"
    )
    library_chunk_chars: int = Field(
        default=4_000, alias="AGENT_LIBRARY_CHUNK_CHARS"
    )
    library_chunk_overlap: int = Field(
        default=200, alias="AGENT_LIBRARY_CHUNK_OVERLAP"
    )
    library_search_top_k: int = Field(
        default=8, alias="AGENT_LIBRARY_SEARCH_TOP_K"
    )
    library_search_max_chars: int = Field(
        default=8_000, alias="AGENT_LIBRARY_SEARCH_MAX_CHARS"
    )
    # MMR 多样性权重：1 = 纯相关度，越小越去重。
    library_mmr_lambda: float = Field(
        default=0.7, alias="AGENT_LIBRARY_MMR_LAMBDA"
    )
    # 确定性 rerank：查询短语/标题命中的小幅加权。
    library_rerank_enabled: bool = Field(
        default=True, alias="AGENT_LIBRARY_RERANK_ENABLED"
    )
    library_rerank_boost: float = Field(
        default=0.25, alias="AGENT_LIBRARY_RERANK_BOOST"
    )
    # linear（默认）| hnswlib；hnswlib 未安装/失败时自动回退 linear。
    library_vector_backend: str = Field(
        default="linear", alias="AGENT_LIBRARY_VECTOR_BACKEND"
    )
    library_vector_candidates: int = Field(
        default=50, alias="AGENT_LIBRARY_VECTOR_CANDIDATES"
    )
    # lexical（默认，零依赖）| vector | hybrid；vector/hybrid 失败回退词法。
    library_search_mode: str = Field(
        default="lexical", alias="AGENT_LIBRARY_SEARCH_MODE"
    )
    # 空 = 继承 memory_embedding_provider；remote 仍受 memory 门禁。
    library_embedding_provider: str = Field(
        default="", alias="AGENT_LIBRARY_EMBEDDING_PROVIDER"
    )
    # 0 = 继承 memory_embedding_dim。
    library_embedding_dim: int = Field(
        default=0, alias="AGENT_LIBRARY_EMBEDDING_DIM"
    )
    # 异步任务：并发上限与崩溃/重启后的 stale 判定（秒）。
    library_max_concurrent_jobs: int = Field(
        default=2, alias="AGENT_LIBRARY_MAX_CONCURRENT_JOBS"
    )
    library_job_stale_s: int = Field(
        default=3_600, alias="AGENT_LIBRARY_JOB_STALE_S"
    )

    # 跨模块变更：按 ChangeContract 自动分波，并在波次之间跑 gate。
    interface_first_writes: bool = Field(
        default=False, alias="AGENT_INTERFACE_FIRST_WRITES"
    )
    # deep 模式下允许的写波次上限（仍受总预算/截止时间约束）。
    deep_max_write_stages: int = Field(
        default=4, alias="AGENT_DEEP_MAX_WRITE_STAGES"
    )

    # ---------- Orchestrator（Phase 3A）----------
    # 新架构默认启用：满足规模阈值的仓库走 harness；小仓库仍走 direct 快速路径。
    orchestrator_enabled: bool = Field(
        default=True, alias="AGENT_ORCHESTRATOR_ENABLED"
    )
    # 小于该文件数不做编排，直接单 agent。
    orchestrator_min_files: int = Field(default=30, alias="AGENT_ORCHESTRATOR_MIN_FILES")
    # 在规模阈值之上再加一层请求级快速判定：明显的单文件/只读问答/拼写修复
    # 直接走 single agent，不付 survey + 工作流选择的固定成本。
    orchestrator_smart_entry: bool = Field(
        default=True, alias="AGENT_ORCHESTRATOR_SMART_ENTRY"
    )
    # survey 先只收集廉价画像；repo_map 等到确认要编排后再构建。
    orchestrator_repo_map_lazy: bool = Field(
        default=False, alias="AGENT_ORCHESTRATOR_REPO_MAP_LAZY"
    )
    # 任务图校验失败时，先带错误反馈让模型修一次，再决定是否整图降级。
    # 默认开启；若要回到「校验失败立即降级」的旧行为，设为 false。
    orchestrator_plan_repair: bool = Field(
        default=True, alias="AGENT_ORCHESTRATOR_PLAN_REPAIR"
    )
    # 并行收益比阈值：task_count / critical_path 低于该值时，只读并行计划
    # 会退回 direct（链式任务并行只多付调度/上下文成本）。0 = 关闭预估。
    orchestrator_min_parallel_gain: float = Field(
        default=1.2, alias="AGENT_ORCHESTRATOR_MIN_PARALLEL_GAIN"
    )
    orchestrator_max_tasks: int = Field(default=6, alias="AGENT_ORCHESTRATOR_MAX_TASKS")
    # 嵌套子编排器：默认关闭；开启后 MAX_DEPTH=1、单层扇出有限。
    # 只有子任务确实需要独立契约/检查点时才应由 planner 选用。
    orchestrator_nested_enabled: bool = Field(
        default=False, alias="AGENT_ORCHESTRATOR_NESTED_ENABLED"
    )
    orchestrator_max_depth: int = Field(
        default=1, alias="AGENT_ORCHESTRATOR_MAX_DEPTH"
    )
    orchestrator_max_fanout: int = Field(
        default=2, alias="AGENT_ORCHESTRATOR_MAX_FANOUT"
    )
    # 同一批 ready 任务最多并发几个只读 worker。
    orchestrator_max_concurrent_workers: int = Field(
        default=4, alias="AGENT_ORCHESTRATOR_MAX_CONCURRENT_WORKERS"
    )
    # 单个任务失败后最多重试几次（0 = 不重试）。重试仍失败才把依赖方标 blocked。
    orchestrator_task_retries: int = Field(
        default=1, alias="AGENT_ORCHESTRATOR_TASK_RETRIES"
    )
    # ---- Phase 3C：并发写 worker（默认关闭）----
    # 打开后，orchestrator 才可能生成 implement 任务；还要求 --write、工作区是
    # git 仓库且干净。worker 在独立 git worktree 中修改，完成后由宿主合并补丁。
    orchestrator_write_enabled: bool = Field(
        default=False, alias="AGENT_ORCHESTRATOR_WRITE_ENABLED"
    )
    orchestrator_worker_rounds: int = Field(
        default=12, alias="AGENT_ORCHESTRATOR_WORKER_ROUNDS"
    )
    orchestrator_worker_repairs: int = Field(
        default=2, alias="AGENT_ORCHESTRATOR_WORKER_REPAIRS"
    )
    orchestrator_worker_timeout: int = Field(
        default=240, alias="AGENT_ORCHESTRATOR_WORKER_TIMEOUT"
    )
    # worker 外层兜底超时（秒），独立于各 runner 的内部超时。
    # 0 = 自动推导：implement 用 worker_timeout，explore/review 用 explore_timeout，
    # 其余角色取 max(600, worker_timeout)；都会再加 30s 余量。
    orchestrator_worker_hard_timeout_s: float = Field(
        default=0.0, alias="AGENT_ORCHESTRATOR_WORKER_HARD_TIMEOUT_S"
    )
    # 交给主 agent 的 worker 汇总上限（字符）。
    orchestrator_context_chars: int = Field(
        default=8_000, alias="AGENT_ORCHESTRATOR_CONTEXT_CHARS"
    )

    # 写 worker 的 write_scope 在 I/O 前强制校验（三重防线之一）。
    orchestrator_write_scope_enforce: bool = Field(
        default=True, alias="AGENT_ORCHESTRATOR_WRITE_SCOPE_ENFORCE"
    )
    # 允许多个 implement 修改同一文件；合并时用 git apply --3way。
    # 有真实冲突时只回滚本分量，不会把冲突标记落进主工作区。
    orchestrator_allow_overlap: bool = Field(
        default=False, alias="AGENT_ORCHESTRATOR_ALLOW_OVERLAP"
    )
    # fan-in 是否落 artifact store 并输出结构化结果。
    orchestrator_fanout_artifacts: bool = Field(
        default=True, alias="AGENT_ORCHESTRATOR_FANOUT_ARTIFACTS"
    )
    # 整个编排阶段（含所有 worker）的墙钟上限，0 表示不限制。
    orchestrator_run_deadline_s: int = Field(
        default=900, alias="AGENT_ORCHESTRATOR_RUN_DEADLINE_S"
    )
    # 单任务 token 上限，0 表示不限制（由 provider 回报的 usage 累计判断）。
    orchestrator_task_tokens: int = Field(
        default=0, alias="AGENT_ORCHESTRATOR_TASK_TOKENS"
    )
    # 整个编排阶段的 token 上限，0 = 不限制。scheduler 在派发前按角色预留，
    # 预留 + 已用超过上限时停止派发；只读在飞任务会被取消，implement 继续跑完
    # 并合并。默认 500k，足以覆盖正常多 worker 任务，同时给失控循环止损。
    orchestrator_total_tokens: int = Field(
        default=500_000, alias="AGENT_ORCHESTRATOR_TOTAL_TOKENS"
    )
    # 四维预算的另外两维：完成的任务尝试数与 worker 工具调用总数。
    # 0 = 不限制；默认给失控循环一个硬边界，正常任务远低于该值。
    orchestrator_total_steps: int = Field(
        default=100, alias="AGENT_ORCHESTRATOR_TOTAL_STEPS"
    )
    orchestrator_total_tool_calls: int = Field(
        default=1_000, alias="AGENT_ORCHESTRATOR_TOTAL_TOOL_CALLS"
    )
    # 同一 (tool, args) 在单个编排内重复出现达到该次数即熔断（F2 重试黑洞）。
    orchestrator_repeat_action_limit: int = Field(
        default=3, alias="AGENT_ORCHESTRATOR_REPEAT_ACTION_LIMIT"
    )
    # 70% 预告警；100% 停止派发并输出已完成部分/卡点/建议。
    orchestrator_budget_warning_ratio: float = Field(
        default=0.7, alias="AGENT_ORCHESTRATOR_BUDGET_WARNING_RATIO"
    )
    # 独立计划评审（S4）：契约映射、不可验证步骤、重复步骤。
    orchestrator_plan_review: bool = Field(
        default=True, alias="AGENT_ORCHESTRATOR_PLAN_REVIEW"
    )
    # 执行前预估（S5）：规则先验 + 历史分桶（task_type × role × model）。
    orchestrator_estimation_enabled: bool = Field(
        default=True, alias="AGENT_ORCHESTRATOR_ESTIMATION_ENABLED"
    )
    orchestrator_estimation_min_samples: int = Field(
        default=5, alias="AGENT_ORCHESTRATOR_ESTIMATION_MIN_SAMPLES"
    )
    # 阶段门/轻量控制检查点：只存契约/计划/产物引用/预算，不存全量消息。
    orchestration_control_checkpoints: bool = Field(
        default=True, alias="AGENT_ORCHESTRATION_CONTROL_CHECKPOINTS"
    )
    # 跑偏轨迹回归集：只记结构化摘要/指标，不记提示词或工具原文。
    regression_capture_enabled: bool = Field(
        default=True, alias="AGENT_REGRESSION_CAPTURE"
    )
    orchestrator_short_task_threshold_s: int = Field(
        default=60, alias="AGENT_ORCHESTRATOR_SHORT_TASK_THRESHOLD_S"
    )
    orchestrator_long_task_threshold_s: int = Field(
        default=120, alias="AGENT_ORCHESTRATOR_LONG_TASK_THRESHOLD_S"
    )
    # 在飞 implement + 待合并补丁的上限；超过后暂停启动新的 implement，
    # 先让合并队列排空，避免补丁/快照堆积。
    orchestrator_max_pending_merges: int = Field(
        default=8, alias="AGENT_ORCHESTRATOR_MAX_PENDING_MERGES"
    )
    # 已完成的 implement 补丁若与在飞任务无文件交集，立即按连通分量合并，
    # 不必等整批结束。默认开启；若有兼容性问题可设为 false 回退整批合并。
    orchestrator_incremental_merge: bool = Field(
        default=True, alias="AGENT_ORCHESTRATOR_INCREMENTAL_MERGE"
    )
    # 补丁合并前的轻量审查：只对高风险 patch（改测试/配置/CI/安全路径、
    # 或 diff 超过阈值）调用一次 review 模型。审查拒绝会让任务带 findings 重试；
    # 它只是质量加固，不替代 write_scope 与审批这些安全边界。
    orchestrator_patch_review: bool = Field(
        default=True, alias="AGENT_ORCHESTRATOR_PATCH_REVIEW"
    )
    orchestrator_patch_review_max_lines: int = Field(
        default=400, alias="AGENT_ORCHESTRATOR_PATCH_REVIEW_MAX_LINES"
    )
    orchestrator_patch_review_max_chars: int = Field(
        default=60_000, alias="AGENT_ORCHESTRATOR_PATCH_REVIEW_MAX_CHARS"
    )
    # 同时进行的补丁审查数；审查是 LLM 调用，串行会把合并尾部拉长。
    orchestrator_patch_review_concurrency: int = Field(
        default=2, alias="AGENT_ORCHESTRATOR_PATCH_REVIEW_CONCURRENCY"
    )
    # 单次补丁审查的墙钟上限；0 = 自动推导（review 角色的 hard timeout）。
    # 超时按“审查未完成”放行合并，但用户取消/deadline 会停止整批合并。
    orchestrator_patch_review_timeout_s: float = Field(
        default=0.0, alias="AGENT_ORCHESTRATOR_PATCH_REVIEW_TIMEOUT_S"
    )
    # 波次 gate 验证的墙钟上限；0 = 自动推导（2 × verify_timeout + 30s，
    # 为“先跑选中用例、通过后再补全量”留出两次命令的空间）。
    orchestrator_gate_timeout_s: float = Field(
        default=0.0, alias="AGENT_ORCHESTRATOR_GATE_TIMEOUT_S"
    )
    # 顺序写波次：允许后一波 implement 基于前一波已合并结果继续修改。
    # 默认关闭；关闭时 implement 之间仍禁止互相依赖。
    orchestrator_sequential_writes: bool = Field(
        default=False, alias="AGENT_ORCHESTRATOR_SEQUENTIAL_WRITES"
    )
    # verify 失败后允许重入 orchestrator 修复的最大轮数；0 = 关闭。
    orchestrator_fix_rounds: int = Field(
        default=0, alias="AGENT_ORCHESTRATOR_FIX_ROUNDS"
    )
    # 灰度期最多允许的写波次数（stage 上限）。
    orchestrator_max_write_stages: int = Field(
        default=2, alias="AGENT_ORCHESTRATOR_MAX_WRITE_STAGES"
    )
    # 注入新 worktree 的基线补丁上限；超限 fail closed。
    orchestrator_seed_max_files: int = Field(
        default=200, alias="AGENT_ORCHESTRATOR_SEED_MAX_FILES"
    )
    orchestrator_seed_max_bytes: int = Field(
        default=5_000_000, alias="AGENT_ORCHESTRATOR_SEED_MAX_BYTES"
    )
    # 遇到 429/限流时 scheduler 的首次退避秒数；之后指数增长，上限 30s。
    orchestrator_rate_limit_backoff_s: float = Field(
        default=2.0, alias="AGENT_ORCHESTRATOR_RATE_LIMIT_BACKOFF_S"
    )
    # scheduler 每完成多少个任务返回一次父图，形成 checkpoint/干预边界。
    orchestrator_checkpoint_every: int = Field(
        default=4, alias="AGENT_ORCHESTRATOR_CHECKPOINT_EVERY"
    )
    # worker 专用模型；空串表示继承 DEEPSEEK_MODEL。用于 explore/review 降本。
    orchestrator_worker_model: str = Field(
        default="", alias="AGENT_ORCHESTRATOR_WORKER_MODEL"
    )
    # 工作流选择/任务图生成专用模型；空串表示继承主模型。
    orchestrator_planner_model: str = Field(
        default="", alias="AGENT_ORCHESTRATOR_PLANNER_MODEL"
    )
    # 角色级模型覆盖；空串表示沿用 orchestrator_worker_model（再退到主模型）。
    # explore/review 默认用便宜模型，失败重试时自动升级到主模型；
    # 网关不支持 deepseek-flash 时，把这两个变量置空即可回退。
    orchestrator_explore_model: str = Field(
        default="deepseek-flash", alias="AGENT_ORCHESTRATOR_EXPLORE_MODEL"
    )
    orchestrator_review_model: str = Field(
        default="deepseek-flash", alias="AGENT_ORCHESTRATOR_REVIEW_MODEL"
    )
    orchestrator_implement_model: str = Field(
        default="", alias="AGENT_ORCHESTRATOR_IMPLEMENT_MODEL"
    )
    orchestrator_document_model: str = Field(
        default="", alias="AGENT_ORCHESTRATOR_DOCUMENT_MODEL"
    )
    orchestrator_author_model: str = Field(
        default="", alias="AGENT_ORCHESTRATOR_AUTHOR_MODEL"
    )
    # fan-in 超预算/有冲突时才调用一次 reducer LLM；失败回退确定性汇总。
    orchestrator_fanin_reducer: bool = Field(
        default=False, alias="AGENT_ORCHESTRATOR_FANIN_REDUCER"
    )
    # 只读模式下模型只给一个任务时，是否强行补一个「找测试」explore 任务。
    # 默认关闭：这会增加一次 worker 成本，只有演示并发/确实需要测试画像时才值得。
    orchestrator_add_explore_tests: bool = Field(
        default=False, alias="AGENT_ORCHESTRATOR_ADD_EXPLORE_TESTS"
    )
    # 派发 implement 前一次性展示 write_scope 并等待确认（并发区不弹审批）。
    orchestrator_write_plan_approval: bool = Field(
        default=False, alias="AGENT_ORCHESTRATOR_WRITE_PLAN_APPROVAL"
    )

    # ---------- 持久化与审计 ----------
    # 留空表示自动用 <当前目录>/.agent/checkpoints.sqlite；填 :memory: 强制不落盘
    checkpoint_path: str = Field(default="", alias="AGENT_CHECKPOINT_PATH")
    # 每个 thread/命名空间保留的 checkpoint 数；0 = 不轮转。
    # 每轮工具调用都会落一个 checkpoint，长期使用需要上限。
    checkpoint_keep: int = Field(default=200, alias="AGENT_CHECKPOINT_KEEP")
    # 有副作用工具的执行日志：崩溃后同一 call_id 不自动重放。
    tool_journal_enabled: bool = Field(
        default=True, alias="AGENT_TOOL_JOURNAL_ENABLED"
    )
    # worker 结果 journal：崩溃/resume 时复用已完成的只读结果，避免重跑。
    # 只复用 success（可选 partial）；implement 永不复用（补丁可能已过期）。
    worker_journal_enabled: bool = Field(
        default=True, alias="AGENT_WORKER_JOURNAL_ENABLED"
    )
    # 允许复用/落盘的角色，逗号分隔。test 默认排除：结果依赖当时环境。
    worker_journal_roles: str = Field(
        default="explore,review", alias="AGENT_WORKER_JOURNAL_ROLES"
    )
    worker_journal_reuse_partial: bool = Field(
        default=False, alias="AGENT_WORKER_JOURNAL_REUSE_PARTIAL"
    )
    # 只有「git 仓库 + 干净工作区 + 能拿到 HEAD」时才复用：HEAD 相同
    # 且没有未提交改动，输入与代码状态才足够一致。
    worker_journal_require_clean_git: bool = Field(
        default=True, alias="AGENT_WORKER_JOURNAL_REQUIRE_CLEAN_GIT"
    )
    worker_journal_max_bytes: int = Field(
        default=10_485_760,
        alias="AGENT_WORKER_JOURNAL_MAX_BYTES",
    )
    # 同一工作区只允许一个 runtime 运行；锁文件在审计目录下。
    workspace_lock_enabled: bool = Field(
        default=True, alias="AGENT_WORKSPACE_LOCK"
    )
    audit_enabled: bool = Field(default=True, alias="AGENT_AUDIT_ENABLED")
    # 控制面不阻塞：审计走有界队列 + 专用写线程；队列满或异步写失败都会
    # 显式抛出 AuditError，绝不静默丢记录。同步模式保留给 CLI/单测。
    audit_async_write: bool = Field(
        default=True, alias="AGENT_AUDIT_ASYNC_WRITE"
    )
    audit_queue_size: int = Field(
        default=10_000, alias="AGENT_AUDIT_QUEUE_SIZE"
    )
    # 事件循环 lag watchdog：超过阈值记指标，不阻断运行。
    event_loop_lag_threshold_ms: int = Field(
        default=500, alias="AGENT_EVENT_LOOP_LAG_THRESHOLD_MS"
    )
    event_loop_lag_interval_s: float = Field(
        default=1.0, alias="AGENT_EVENT_LOOP_LAG_INTERVAL_S"
    )
    # 通知面：首卡 T+0、最长静默 heartbeat、进度卡最小间隔。
    card_max_silence_s: float = Field(
        default=120.0, alias="AGENT_CARD_MAX_SILENCE_S"
    )
    card_min_interval_s: float = Field(
        default=10.0, alias="AGENT_CARD_MIN_INTERVAL_S"
    )
    # 审批卡：超时默认动作（pause / allow / deny）。当前写入卡片契约；
    # 自动 resume 由前端/控制面按下发字段执行。
    approval_ack_timeout_s: int = Field(
        default=300, alias="AGENT_APPROVAL_ACK_TIMEOUT_S"
    )
    approval_default_action: str = Field(
        default="pause", alias="AGENT_APPROVAL_DEFAULT_ACTION"
    )
    # 留空表示自动用 <当前目录>/.agent/audit
    audit_dir: str = Field(default="", alias="AGENT_AUDIT_DIR")
    # SIEM 导出：host:port（UDP syslog），每条审计 JSON 一行；空 = 不导出。
    audit_syslog_address: str = Field(
        default="", alias="AGENT_AUDIT_SYSLOG_ADDRESS"
    )
    # SIEM webhook：POST 每行审计 JSON；空 = 不导出。
    audit_webhook_url: str = Field(
        default="", alias="AGENT_AUDIT_WEBHOOK_URL"
    )
    audit_webhook_token: str = Field(
        default="", alias="AGENT_AUDIT_WEBHOOK_TOKEN"
    )
    # 快照校验清单根目录；留空 = 用户主目录下的 .codingagent/snapshots。
    # 放在工作区外，shell 级篡改快照内容也能在恢复时被发现。
    snapshot_manifest_dir: str = Field(
        default="", alias="AGENT_SNAPSHOT_MANIFEST_DIR"
    )
    # 快照保留：每次运行开始清理，保留最新 N 份且不超过 M 天。0 = 不按该维度清理。
    snapshot_keep_count: int = Field(
        default=200, alias="AGENT_SNAPSHOT_KEEP_COUNT"
    )
    snapshot_keep_days: int = Field(
        default=30, alias="AGENT_SNAPSHOT_KEEP_DAYS"
    )
    # 未消费 steering 指令的持久化目录；留空 = <当前目录>/.agent/steering
    steering_dir: str = Field(default="", alias="AGENT_STEERING_DIR")
    # 跨进程限流状态文件；留空 = <当前目录>/.agent/rate_limits.json；:memory: 关闭
    rate_limit_state_path: str = Field(
        default="", alias="AGENT_RATE_LIMIT_STATE_PATH"
    )
    # 单个审计文件超过该大小（MB）就轮转；0 表示不轮转
    audit_max_mb: int = Field(default=256, alias="AGENT_AUDIT_MAX_MB")

    # ---------- 安全 profile 与不可信仓库 ----------
    # local（默认，面向可信仓库）| untrusted（bwrap + 禁网 + 强制 ask +
    # 不信任仓库指令/记忆）。
    security_profile: str = Field(
        default="local", alias="AGENT_SECURITY_PROFILE"
    )
    # 仓库内的 AGENTS.md / CLAUDE.md 是否作为可信指令注入。
    trust_repo_instructions: bool = Field(
        default=True, alias="AGENT_TRUST_REPO_INSTRUCTIONS"
    )
    # 是否读取仓库内 `.agent/memory.md`；false 时记忆改存 memory_root。
    trust_repo_memory: bool = Field(
        default=True, alias="AGENT_TRUST_REPO_MEMORY"
    )
    # 长期记忆根目录（WSL 绝对路径）；空则按 trust_repo_memory 推导。
    memory_root: str = Field(default="", alias="AGENT_MEMORY_ROOT")
    # 跨工作区全局记忆根；空 = 沙箱 $HOME/.agent-memory/global。
    memory_global_root: str = Field(
        default="", alias="AGENT_MEMORY_GLOBAL_ROOT"
    )
    # 可选向量检索：默认关闭，开启后仍以 BM25 为回退。
    memory_vector_enabled: bool = Field(
        default=False, alias="AGENT_MEMORY_VECTOR_ENABLED"
    )
    memory_embedding_provider: str = Field(
        default="local", alias="AGENT_MEMORY_EMBEDDING_PROVIDER"
    )
    memory_embedding_model: str = Field(
        default="BAAI/bge-small-zh-v1.5",
        alias="AGENT_MEMORY_EMBEDDING_MODEL",
    )
    memory_embedding_dim: int = Field(
        default=512, alias="AGENT_MEMORY_EMBEDDING_DIM"
    )
    memory_embedding_cache_dir: str = Field(
        default="", alias="AGENT_MEMORY_EMBEDDING_CACHE_DIR"
    )
    # 远程 OpenAI-compatible embedding（默认关闭，需双重显式开启）。
    memory_embedding_base_url: str = Field(
        default="", alias="AGENT_MEMORY_EMBEDDING_BASE_URL"
    )
    memory_embedding_api_key: str = Field(
        default="", alias="AGENT_MEMORY_EMBEDDING_API_KEY"
    )
    memory_embedding_timeout_s: float = Field(
        default=10.0, alias="AGENT_MEMORY_EMBEDDING_TIMEOUT_S"
    )
    memory_embedding_batch_size: int = Field(
        default=64, alias="AGENT_MEMORY_EMBEDDING_BATCH_SIZE"
    )
    memory_allow_remote_embeddings: bool = Field(
        default=False, alias="AGENT_MEMORY_ALLOW_REMOTE_EMBEDDINGS"
    )
    memory_vector_top_n: int = Field(
        default=20, alias="AGENT_MEMORY_VECTOR_TOP_N"
    )
    memory_fusion: str = Field(default="rrf", alias="AGENT_MEMORY_FUSION")
    # 秘密文件读保护 glob（逗号分隔）；.env.example 等模板自动放行。
    read_deny_globs: str = Field(
        default=DEFAULT_READ_DENY_GLOBS,
        alias="AGENT_READ_DENY_GLOBS",
    )

    # ---------- Web 多用户 ----------
    # JSON：{"token": "user"}；与 AGENT_WEB_AUTH_TOKENS_FILE 二选一。
    web_auth_tokens: str = Field(
        default="", alias="AGENT_WEB_AUTH_TOKENS"
    )
    web_auth_tokens_file: str = Field(
        default="", alias="AGENT_WEB_AUTH_TOKENS_FILE"
    )
    # 企业账号：打开后使用 control_users / session Cookie 登录。
    web_accounts_enabled: bool = Field(
        default=False, alias="AGENT_WEB_ACCOUNTS_ENABLED"
    )
    web_account_tenant: str = Field(
        default="default", alias="AGENT_WEB_ACCOUNT_TENANT"
    )
    # 企业账号的生产约束：只允许 MySQL；本地测试可显式关闭。
    web_accounts_require_mysql: bool = Field(
        default=True, alias="AGENT_WEB_ACCOUNTS_REQUIRE_MYSQL"
    )
    # 跨源部署时 Cookie 必须 Secure；同源反代可关。
    web_cookie_secure: bool = Field(
        default=False, alias="AGENT_WEB_COOKIE_SECURE"
    )
    web_session_ttl_s: int = Field(
        default=12 * 3600, alias="AGENT_WEB_SESSION_TTL_S"
    )
    # 对外访问根，用于邮件里的重置链接；空 = 邮件只带 token。
    web_public_url: str = Field(default="", alias="AGENT_WEB_PUBLIC_URL")
    web_password_reset_ttl_s: int = Field(
        default=1800, alias="AGENT_WEB_PASSWORD_RESET_TTL_S"
    )
    # 自助密码重置邮件（SMTP）；host 为空 = 不发送。
    smtp_host: str = Field(default="", alias="AGENT_SMTP_HOST")
    smtp_port: int = Field(default=587, alias="AGENT_SMTP_PORT")
    smtp_user: str = Field(default="", alias="AGENT_SMTP_USER")
    smtp_password: str = Field(default="", alias="AGENT_SMTP_PASSWORD")
    smtp_from: str = Field(default="", alias="AGENT_SMTP_FROM")
    smtp_starttls: bool = Field(default=True, alias="AGENT_SMTP_STARTTLS")
    # 空闲超时：超过该时长没有请求则失效；仍受上面的绝对 TTL 限制。
    # 0 = 关闭（只使用绝对 TTL）。
    web_session_idle_ttl_s: int = Field(
        default=0, alias="AGENT_WEB_SESSION_IDLE_TTL_S"
    )
    # A2 /metrics 的抓取 token；空 = 复用普通 Web 认证。
    metrics_token: str = Field(
        default="", alias="AGENT_METRICS_TOKEN"
    )
    # text | json；容器/日志采集建议 json。
    log_format: str = Field(default="text", alias="AGENT_LOG_FORMAT")
    # /openapi.json 与 /docs；默认关闭，避免把完整路由表暴露给公网。
    web_openapi: bool = Field(default=False, alias="AGENT_WEB_OPENAPI")
    # OTLP HTTP trace；空 = 关闭。需要 `pip install -e ".[otel]"`。
    otel_endpoint: str = Field(default="", alias="AGENT_OTEL_ENDPOINT")
    otel_service_name: str = Field(
        default="coding-agent", alias="AGENT_OTEL_SERVICE_NAME"
    )
    # `k=v,k2=v2` 形式的 collector headers。
    otel_headers: str = Field(default="", alias="AGENT_OTEL_HEADERS")

    # --- 企业身份（RFC-0004）---
    oidc_enabled: bool = Field(
        default=False, alias="AGENT_OIDC_ENABLED"
    )
    oidc_issuer: str = Field(default="", alias="AGENT_OIDC_ISSUER")
    oidc_client_id: str = Field(
        default="", alias="AGENT_OIDC_CLIENT_ID"
    )
    oidc_client_secret: str = Field(
        default="", alias="AGENT_OIDC_CLIENT_SECRET"
    )
    oidc_redirect_uri: str = Field(
        default="", alias="AGENT_OIDC_REDIRECT_URI"
    )
    oidc_scopes: str = Field(
        default="openid profile email", alias="AGENT_OIDC_SCOPES"
    )
    oidc_auto_create_users: bool = Field(
        default=True, alias="AGENT_OIDC_AUTO_CREATE_USERS"
    )
    oidc_default_role: str = Field(
        default="member", alias="AGENT_OIDC_DEFAULT_ROLE"
    )
    oidc_role_claim: str = Field(
        default="groups", alias="AGENT_OIDC_ROLE_CLAIM"
    )
    # JSON: {"idp-group": "admin", ...}；未命中用 default_role。
    oidc_role_map: str = Field(
        default="", alias="AGENT_OIDC_ROLE_MAP"
    )
    oidc_username_claim: str = Field(
        default="preferred_username", alias="AGENT_OIDC_USERNAME_CLAIM"
    )
    oidc_email_claim: str = Field(
        default="email", alias="AGENT_OIDC_EMAIL_CLAIM"
    )
    oidc_state_ttl_s: int = Field(
        default=600, alias="AGENT_OIDC_STATE_TTL_S"
    )
    oidc_http_timeout_s: float = Field(
        default=10.0, alias="AGENT_OIDC_HTTP_TIMEOUT_S"
    )
    # 仅本地开发允许 http:// issuer；生产必须 HTTPS。
    oidc_allow_insecure_http: bool = Field(
        default=False, alias="AGENT_OIDC_ALLOW_INSECURE_HTTP"
    )
    mfa_enabled: bool = Field(
        default=False, alias="AGENT_MFA_ENABLED"
    )
    mfa_issuer: str = Field(
        default="CodingAgent", alias="AGENT_MFA_ISSUER"
    )
    mfa_challenge_ttl_s: int = Field(
        default=300, alias="AGENT_MFA_CHALLENGE_TTL_S"
    )
    mfa_max_attempts: int = Field(
        default=5, alias="AGENT_MFA_MAX_ATTEMPTS"
    )
    mfa_window_steps: int = Field(
        default=1, alias="AGENT_MFA_WINDOW_STEPS"
    )
    # 四眼审批：L3 默认；0 = 关闭（但仍可用独立开关关闭整个特性）。
    approval_four_eyes: bool = Field(
        default=False, alias="AGENT_APPROVAL_FOUR_EYES"
    )
    approval_four_eyes_min_level: int = Field(
        default=3, alias="AGENT_APPROVAL_FOUR_EYES_MIN_LEVEL"
    )
    approval_ttl_s: int = Field(
        default=3600, alias="AGENT_APPROVAL_TTL_S"
    )
    # 运行前为 token 限额预留的量；0 = 只做事后限额。默认预留一段，
    # 让并发/长 run 在真正超额前被硬上限拦住。
    web_quota_reserve_tokens: int = Field(
        default=20_000, alias="AGENT_WEB_QUOTA_RESERVE_TOKENS"
    )
    # 成本展示与额度计量货币；上游价格仍按 USD 计价。
    cost_currency: str = Field(
        default="CNY", alias="AGENT_COST_CURRENCY"
    )
    usd_to_cny: float = Field(
        default=7.2, alias="AGENT_USD_TO_CNY"
    )
    # 登录按来源 IP 的固定窗口限流。
    web_login_rate_limit_per_min: int = Field(
        default=10, alias="AGENT_WEB_LOGIN_RATE_LIMIT_PER_MIN"
    )
    # 匿名 CSP 违规上报按来源 IP 限流；防止被当作写日志放大器。
    web_csp_report_rate_limit_per_min: int = Field(
        default=300, alias="AGENT_WEB_CSP_REPORT_RATE_LIMIT_PER_MIN"
    )
    # memory = 单进程内存；redis = 多实例共享固定窗口。
    rate_limit_backend: str = Field(
        default="memory", alias="AGENT_RATE_LIMIT_BACKEND"
    )
    rate_limit_redis_url: str = Field(
        default="", alias="AGENT_RATE_LIMIT_REDIS_URL"
    )
    # 配额预留的存活时间；进程崩溃后由其他实例/下次启动清理。
    quota_reservation_ttl_s: int = Field(
        default=3600, alias="AGENT_QUOTA_RESERVATION_TTL_S"
    )
    # 上游 provider Key 的 AES-GCM 主密钥（base64 32B）或文件。
    secret_master_key: str = Field(
        default="", alias="AGENT_SECRET_MASTER_KEY"
    )
    # 浏览器 Web Push（VAPID）。公钥/私钥都配置且安装 pywebpush 才启用；
    # 私钥只放服务端，绝不下发。
    webpush_public_key: str = Field(
        default="", alias="AGENT_WEBPUSH_PUBLIC_KEY"
    )
    webpush_private_key: str = Field(
        default="", alias="AGENT_WEBPUSH_PRIVATE_KEY"
    )
    webpush_subject: str = Field(
        default="mailto:admin@localhost",
        alias="AGENT_WEBPUSH_SUBJECT",
    )
    webpush_ttl: int = Field(default=3600, alias="AGENT_WEBPUSH_TTL")
    secret_master_key_file: str = Field(
        default="", alias="AGENT_SECRET_MASTER_KEY_FILE"
    )
    # 主密钥代次；轮换脚本递增，写入每条密文的 key_version。
    secret_master_key_version: int = Field(
        default=1, alias="AGENT_SECRET_MASTER_KEY_VERSION"
    )
    # per-user 工作区根目录；空 = 沙箱 $HOME/agent-ws/users。
    web_workspaces_root: str = Field(
        default="", alias="AGENT_WEB_WORKSPACES_ROOT"
    )
    # per-user 宿主侧数据根目录（审计/checkpoint/steering）；
    # 空 = <当前目录>/.agent/web/users。
    web_data_root: str = Field(default="", alias="AGENT_WEB_DATA_ROOT")
    # per-user WSL 侧记忆根目录；空 = 沙箱 $HOME/agent-memory。
    # 记忆在沙箱内、仓库之外，避免不可信仓库预置记忆。
    web_memory_root: str = Field(
        default="", alias="AGENT_WEB_MEMORY_ROOT"
    )
    # per-user 资料库根目录；空 = 沙箱 $HOME/agent-library。
    # 每个用户一份，跨工作区保留。
    web_library_root: str = Field(
        default="", alias="AGENT_WEB_LIBRARY_ROOT"
    )
    web_max_body_bytes: int = Field(
        default=256 * 1024, alias="AGENT_WEB_MAX_BODY_BYTES"
    )
    web_max_prompt_chars: int = Field(
        default=100_000, alias="AGENT_WEB_MAX_PROMPT_CHARS"
    )
    # 单次上传的单文件上限；上传走单独授权，写入 .agent/uploads。
    web_max_upload_bytes: int = Field(
        default=20_971_520, alias="AGENT_WEB_MAX_UPLOAD_BYTES"
    )
    # 单次请求最多上传几个文件。
    web_max_upload_files: int = Field(
        default=20, alias="AGENT_WEB_MAX_UPLOAD_FILES"
    )
    # 每个会话累计上传上限，防止反复上传占满工作区。
    web_max_upload_total_bytes: int = Field(
        default=209_715_200, alias="AGENT_WEB_MAX_UPLOAD_TOTAL_BYTES"
    )
    web_rate_limit_per_min: int = Field(
        default=60, alias="AGENT_WEB_RATE_LIMIT_PER_MIN"
    )
    web_max_runtimes: int = Field(
        default=8, alias="AGENT_WEB_MAX_RUNTIMES"
    )
    web_max_runtimes_per_user: int = Field(
        default=2, alias="AGENT_WEB_MAX_RUNTIMES_PER_USER"
    )
    # 挂起 runtime 的空闲保留时间；超时后释放，resume 时从 checkpoint 重建。
    web_runtime_ttl_s: int = Field(
        default=1800, alias="AGENT_WEB_RUNTIME_TTL_S"
    )
    # 未配置 token 时是否允许 loopback 匿名访问（单用户本地模式）。
    web_allow_anonymous_local: bool = Field(
        default=True, alias="AGENT_WEB_ALLOW_ANONYMOUS_LOCAL"
    )
    # 独立源站前端允许的精确 origin（逗号分隔）；空 = 不启用 CORS。
    # 只允许 scheme://host[:port]，不接受路径、通配符或 null。
    web_cors_allowed_origins: str = Field(
        default="", alias="AGENT_CORS_ALLOWED_ORIGINS"
    )
    # 是否由 A2 托管前端页面。独立静态站/CDN 部署时关闭，只保留 API；
    # 桌面模式（desktop_mode）始终强制开启，否则没有页面可加载。
    web_serve_frontend: bool = Field(
        default=True, alias="AGENT_WEB_SERVE_FRONTEND"
    )
    # 前端发布包根目录（含 index.html / sw.js / manifest.webmanifest）。
    # 空 = 包内 web/static（桌面与开发回退）。
    web_frontend_dir: str = Field(
        default="", alias="AGENT_WEB_FRONTEND_DIR"
    )
    # /static 挂载目录；空 = 与 web_frontend_dir 相同。
    # 发布包通常为 <frontend_dir>/static，包内则两者同目录。
    web_static_dir: str = Field(
        default="", alias="AGENT_WEB_STATIC_DIR"
    )
    # ---------- 服务端外连守卫（SSRF） ----------
    # provider 连通性探测会带上真实 API Key，目标必须过
    # security/netguard.py 的解析后 IP 判定。默认拒绝回环 / 内网 / 保留地址；
    # 链路本地（169.254.169.254 等云元数据）**永远**拒绝，不受此开关影响。
    # 本机或内网 OpenAI-compatible 端点显式打开它。
    provider_probe_allow_private_network: bool = Field(
        default=False, alias="AGENT_PROVIDER_PROBE_ALLOW_PRIVATE_NETWORK"
    )
    # 目标主机后缀白名单（逗号分隔，如 `deepseek.com,openai.com`）；
    # 空 = 不额外限制，仍需过 IP 判定。企业部署建议填上，可以顺带收敛
    # DNS rebinding 的残余风险。
    provider_probe_allowed_hosts: str = Field(
        default="", alias="AGENT_PROVIDER_PROBE_ALLOWED_HOSTS"
    )

    # ---------- A2 控制面（Phase B，可选） ----------
    # 空 = 继续用 gui-sessions.json / 本地文件（Standalone 与旧部署）。
    # 例：mysql+pymysql://user:pass@127.0.0.1/coding_agent
    control_db_url: str = Field(default="", alias="AGENT_CONTROL_DB_URL")
    # SQLite/开发环境自动建表；MySQL 生产迁移用 alembic upgrade 或
    # control_server --migrate（迁移前自动备份）。
    control_db_auto_create: bool = Field(
        default=True, alias="AGENT_CONTROL_DB_AUTO_CREATE"
    )
    # auto | keyring | systemd；服务器部署用 systemd-creds，Vault/KMS 走同一 Protocol。
    secret_provider: str = Field(default="auto", alias="AGENT_SECRET_PROVIDER")

    # ---------- 工作区注册表（RFC-0001，默认关闭） ----------
    # false = 继续使用 control_sessions.workspace 裸路径，行为与旧部署一致。
    workspace_registry: bool = Field(
        default=False, alias="AGENT_WORKSPACE_REGISTRY"
    )
    # 工作区根；空 = 沿用租户 workspaces_root（TenantManager）。
    # 必须是 A3/A4 视角的绝对 POSIX 路径；host 工作区也可以写成 Windows
    # 路径（`D:\ws`），校验时两侧都会归一化成 POSIX 再比较。
    workspace_root: str = Field(default="", alias="AGENT_WORKSPACE_ROOT")
    # revision 归档根；空 = 关闭归档（仍可运行，但不能灾难恢复）。
    workspace_archive_root: str = Field(
        default="", alias="AGENT_WORKSPACE_ARCHIVE_ROOT"
    )
    # 每个工作区保留的 revision 数；超出后清理最旧的非 pinned 归档。
    workspace_revisions_keep: int = Field(
        default=10, alias="AGENT_WORKSPACE_REVISIONS_KEEP"
    )
    # copy = 桌面默认复制到 ext4；mount = 显式降级，需二次确认。
    workspace_import_mode: str = Field(
        default="copy", alias="AGENT_WORKSPACE_IMPORT_MODE"
    )
    # none | clamav；企业部署应在 M3 设为 clamav 并配合 fail-closed。
    workspace_scan_provider: str = Field(
        default="none", alias="AGENT_WORKSPACE_SCAN_PROVIDER"
    )
    # 允许的 /mnt 挂载根，逗号分隔；空 = 禁止任何挂载工作区。
    workspace_mount_roots: str = Field(
        default="", alias="AGENT_WORKSPACE_MOUNT_ROOTS"
    )
    # 本期不实现 git clone；保留开关，未来接入内部 Git 服务时打开。
    allow_git_clone: bool = Field(
        default=False, alias="AGENT_ALLOW_GIT_CLONE"
    )
    # 项目归档上传上限（单包、需 Content-Length）；默认 200 MiB。
    workspace_upload_max_bytes: int = Field(
        default=209_715_200, alias="AGENT_WORKSPACE_UPLOAD_MAX_BYTES"
    )
    # 解包后的文件数上限。
    workspace_upload_max_files: int = Field(
        default=200_000, alias="AGENT_WORKSPACE_UPLOAD_MAX_FILES"
    )
    # 解压比上限：解压后总字节 / 归档字节，防止 zip bomb。
    workspace_extract_max_ratio: int = Field(
        default=200, alias="AGENT_WORKSPACE_EXTRACT_MAX_RATIO"
    )
    # ClamAV 单次扫描超时。
    workspace_scan_timeout_s: int = Field(
        default=300, alias="AGENT_WORKSPACE_SCAN_TIMEOUT_S"
    )
    # A3 导入/上传任务状态保留天数；超期的终态文件在下次启动时清理。
    workspace_job_retention_days: int = Field(
        default=7, alias="AGENT_WORKSPACE_JOB_RETENTION_DAYS"
    )

    # ---------- 一致性与内部签名（M4） ----------
    # 会话级运行租约 TTL；A2 崩溃后到期自动释放。
    run_lease_ttl_s: int = Field(
        default=120, alias="AGENT_RUN_LEASE_TTL_S"
    )
    # 幂等键保留时间。
    idempotency_ttl_s: int = Field(
        default=600, alias="AGENT_IDEMPOTENCY_TTL_S"
    )
    # SIGTERM 优雅退出：A3 停止接收新运行并等待进行中的运行自然结束；
    # 超时后取消，A2 用同一数值约束 uvicorn 在途请求。0 = 不等待。
    drain_timeout_s: int = Field(
        default=60, ge=0, le=3600, alias="AGENT_DRAIN_TIMEOUT_S"
    )
    # 空 = 只依赖内部 Bearer token（兼容旧部署）；
    # 非空 = A2 对 X-Agent-Context 做 HMAC，A3 验签。
    agent_internal_hmac_key: str = Field(
        default="", alias="AGENT_INTERNAL_HMAC_KEY"
    )
    # 是否**要求** A2 发来的 X-Agent-Context 带 HMAC 签名（非空 key）。
    # 空 = 兼容未升级的部署，只靠内部 Bearer token；拆分部署应显式设 true。
    # A3 绑在非 loopback 地址时无论这里怎么配都强制要求（启动时校验）。
    agent_internal_hmac_required: bool = Field(
        default=False, alias="AGENT_INTERNAL_HMAC_REQUIRED"
    )

    # ---------- 共享事件传输（Phase B3） ----------
    # memory = 单实例内存；redis = Redis Streams（多实例 SSE 重放）。
    event_transport: str = Field(
        default="memory", alias="AGENT_EVENT_TRANSPORT"
    )
    event_transport_url: str = Field(
        default="", alias="AGENT_EVENT_TRANSPORT_URL"
    )
    event_transport_prefix: str = Field(
        default="coding-agent:events",
        alias="AGENT_EVENT_TRANSPORT_PREFIX",
    )
    event_transport_maxlen: int = Field(
        default=10_000, alias="AGENT_EVENT_TRANSPORT_MAXLEN"
    )
    event_transport_ttl_s: int = Field(
        default=3600, alias="AGENT_EVENT_TRANSPORT_TTL_S"
    )

    # ---------- A2 → A3 内部连接（Phase B4） ----------
    # 空 = A2 与 Agent 同进程（默认，兼容旧部署）。
    agent_internal_url: str = Field(
        default="", alias="AGENT_INTERNAL_URL"
    )
    agent_internal_token: str = Field(
        default="", alias="AGENT_INTERNAL_TOKEN"
    )
    # Agent Server 的租户数据根；空 = 审计目录旁的 agent/。
    agent_data_root: str = Field(default="", alias="AGENT_AGENT_DATA_ROOT")

    # ---------- Agent 循环 ----------
    max_repair_rounds: int = Field(default=3, alias="AGENT_MAX_REPAIR_ROUNDS")
    # 连续出现同一失败签名多少次后停止 repair、改走 replan。
    # 24 次相同失败验证的教训：没有这道闸，repair 会把同一类修改重复到额度耗尽。
    max_same_failures: int = Field(default=2, alias="AGENT_MAX_SAME_FAILURES")
    # 执行中「重建剩余计划」的次数上限。计划可以修正，但不能无限改 ——
    # 边做边改没有收敛点，而每次重规划都要多一次模型调用
    max_replans: int = Field(default=2, alias="AGENT_MAX_REPLANS")
    max_tool_rounds: int = Field(default=12, alias="AGENT_MAX_TOOL_ROUNDS")
    max_plan_steps: int = Field(default=5, alias="AGENT_MAX_PLAN_STEPS")
    # 同一会话内「挂起→恢复」的总次数上限，防前端反复 resume 绕开 recursion_limit
    max_resumes: int = Field(default=50, alias="AGENT_MAX_RESUMES")
    # 一次完整运行（含审批/暂停后的 resume）的总墙钟上限，0 表示不限制。
    # 与 recursion_limit 互补：后者管步数，这里管时间（例如模型长时间卡住）。
    run_deadline_s: int = Field(default=1800, alias="AGENT_RUN_DEADLINE_S")
    # ask（逐条询问）| approve（自动放行 L2；L3 仍需确认）| deny（一律拒绝）
    approval_mode: str = Field(default="ask", alias="AGENT_APPROVAL_MODE")
    # 持久放行规则，逗号分隔的 `tool:glob`，例如
    # `shell_exec:git status*,run_tests:*`。只覆盖原本会 ASK 的 L2。
    allow_rules: str = Field(default="", alias="AGENT_ALLOW_RULES")
    llm_temperature: float = Field(default=0.0, alias="AGENT_LLM_TEMPERATURE")
    llm_timeout: int = Field(default=120, alias="AGENT_LLM_TIMEOUT")

    @property
    def context_budget(self) -> Any:
        """延迟导入：config 不该在导入期拉起 llm 层。"""
        from ubismart.llm.context import ContextBudget

        return ContextBudget(
            max_chars=self.context_max_chars,
            max_tokens=self.context_max_tokens,
            keep_recent=self.context_keep_recent,
            tool_chars=self.context_tool_chars,
        )

    @property
    def resolved_checkpoint_path(self) -> str:
        """checkpoint 落盘位置；空配置落到当前目录的 .agent/ 下。"""
        return self.checkpoint_path or str(Path.cwd() / ".agent" / "checkpoints.sqlite")

    @property
    def resolved_audit_dir(self) -> Path:
        return Path(self.audit_dir) if self.audit_dir else Path.cwd() / ".agent" / "audit"

    @property
    def resolved_snapshot_manifest_dir(self) -> Path:
        return (
            Path(self.snapshot_manifest_dir)
            if self.snapshot_manifest_dir
            else Path.home() / ".codingagent" / "snapshots"
        )

    @property
    def resolved_steering_dir(self) -> Path:
        return (
            Path(self.steering_dir)
            if self.steering_dir
            else Path.cwd() / ".agent" / "steering"
        )

    @property
    def resolved_rate_limit_state_path(self) -> str | None:
        raw = str(self.rate_limit_state_path or "").strip()
        if raw == ":memory:":
            return None
        return raw or str(Path.cwd() / ".agent" / "rate_limits.json")

    def apply_tracing_env(self) -> None:
        """把配置写回 os.environ，供 langsmith 自动读取。"""
        if not self.langsmith_tracing:
            os.environ["LANGSMITH_TRACING"] = "false"
            return
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGCHAIN_TRACING_V2"] = "true"
        os.environ["LANGSMITH_PROJECT"] = self.langsmith_project
        if self.langsmith_api_key:
            os.environ["LANGSMITH_API_KEY"] = self.langsmith_api_key


def apply_security_profile(settings: Settings) -> Settings:
    """把 `AGENT_SECURITY_PROFILE` 展开成具体的安全开关。

    `untrusted` 面向不可信仓库/公网：bwrap 不可用时 fail closed、命令无网络、
    L2/L3 全部人工确认、不注入仓库自带的指令/记忆、清空持久放行规则。
    """
    profile = str(settings.security_profile or "").strip().lower()
    if profile == "untrusted":
        update = {
            "shell_sandbox": "bwrap",
            "shell_network": False,
            "approval_mode": "ask",
            # 持久放行规则会把 L2 自动放行，与 untrusted 的“全部人工确认”冲突。
            "allow_rules": "",
            "trust_repo_instructions": False,
            "trust_repo_memory": False,
            "audit_enabled": True,
            "tool_journal_enabled": True,
            "workspace_lock_enabled": True,
        }
        if not str(settings.read_deny_globs or "").strip():
            update["read_deny_globs"] = DEFAULT_READ_DENY_GLOBS
        return settings.model_copy(update=update)

    if profile in PRODUCTION_PROFILES:
        # production/ops 是**只读审计档**：任何 L2/L3 变更都直接拒绝，
        # 写能力由 AgentRuntime/build_graph 强制关闭（见 allow_write 覆盖）。
        update = {
            "shell_sandbox": "bwrap",
            "shell_network": False,
            "approval_mode": "deny",
            "allow_rules": "",
            "trust_repo_instructions": False,
            "trust_repo_memory": False,
            "audit_enabled": True,
            "tool_journal_enabled": True,
            "workspace_lock_enabled": True,
        }
        if not str(settings.read_deny_globs or "").strip():
            update["read_deny_globs"] = DEFAULT_READ_DENY_GLOBS
        return settings.model_copy(update=update)

    return settings


def shadowed_env_keys(env_file: str = ".env") -> dict[str, str]:
    """找出「环境变量里有、但被 .env 覆盖掉」的键。

    优先级是 `.env` > 环境变量，所以这是**有意的**行为；但它同样静默 ——
    如果环境变量里那份才是你以为在生效的（比如 CI 注入的），
    结果就会与预期不符。doctor 把差异列出来，避免又一次「改了没生效」的排查。

    返回值：{键: ".env"（当前生效的来源）}。
    """
    from dotenv import dotenv_values

    try:
        file_values = {k: v for k, v in dotenv_values(env_file).items() if v}
    except OSError:
        return {}

    return {
        key: ".env"
        for key, value in file_values.items()
        if key in os.environ and os.environ[key] != value
    }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = apply_security_profile(Settings())
    settings.apply_tracing_env()
    return settings
