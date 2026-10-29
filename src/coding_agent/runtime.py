"""AgentRuntime —— 唯一编排入口。

所有前端（CLI / TUI / Web）都只跟这一层打交道：
它持有图、沙箱与安全策略，把 LangGraph 的原始流翻译成语义化的领域事件。
前端拿不到工具，也就无法绕过命令分级审批。

    async for event in AgentRuntime(settings).run("列出文件", thread_id="x"):
        match event.type: ...
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.types import Command

from coding_agent.access import (
    ACCESS_READ_ONLY,
    ACCESS_WRITE_ASK,
    ACCESS_WRITE_AUTO,
    normalize_access_mode,
)
from coding_agent.agents.policy import (
    RepoProfile,
    delegation_policy,
    profile_workspace,
)
from coding_agent.audit import AuditLogger, AuditRecord
from coding_agent.audit import models as audit_models
from coding_agent.audit.logger import (
    now_iso,
    sanitize_args,
    truncate,
)
from coding_agent.concurrency import ConcurrencyGovernor
from coding_agent.config import (
    PRODUCTION_PROFILES,
    Settings,
    apply_security_profile,
    get_settings,
)
from coding_agent.events import (
    ApprovalRequested,
    AssistantToken,
    ContextTrimmed,
    CostReport,
    Event,
    FileChanged,
    OrchestrationFinished,
    PlanCreated,
    PlanRevised,
    ReasoningToken,
    RepairStarted,
    ReviewFinished,
    RunFailed,
    RunFinished,
    RunStarted,
    SteeringApplied,
    StepFinished,
    StepStarted,
    SubAgentFinished,
    SubAgentStarted,
    TaskGraphCreated,
    TaskGraphExtended,
    ToolCallFinished,
    ToolCallStarted,
    Verification,
    WorkerFinished,
    WorkerStarted,
    WorkflowRevised,
    WorkflowSelected,
)
from coding_agent.execution import (
    ExecutionSandbox,
    ExecutionService,
    SandboxExecutionBackend,
)
from coding_agent.graph.build import build_graph, estimate_recursion_limit
from coding_agent.graph.nodes.approve import tool_level
from coding_agent.llm.billing import fetch_billing
from coding_agent.llm.context import summarize_messages
from coding_agent.llm.ratelimit import GLOBAL_LIMITER
from coding_agent.memory.checkpointer import CheckpointStore
from coding_agent.memory.longterm import (
    LayeredMemory,
    LongTermMemory,
    resolve_global_memory_root,
    resolve_memory_root,
)
from coding_agent.memory.semantic import SemanticMemory, wrap_memory
from coding_agent.memory.sessions import SessionIndex
from coding_agent.messages import (
    reasoning_of,
    recover_dangling_tool_calls,
    text_of,
    unanswered_tool_calls,
)
from coding_agent.observability import note_silent_failure
from coding_agent.orchestrator.merge_journal import MergeJournal
from coding_agent.runtime_snapshots import SnapshotMixin
from coding_agent.sandbox.backend import build_sandbox
from coding_agent.sandbox.fs import SandboxFs
from coding_agent.sandbox.pathguard import normalize_root
from coding_agent.sandbox.policy import (
    APPROVED,
    DENIED,
    SessionPolicy,
    classify,
    parse_allow_rules,
)
from coding_agent.sandbox.snapshots import (
    ACTION_RESTORE,
    SnapshotStore,
)
from coding_agent.sandbox.wsl_exec import resolve_workspace
from coding_agent.secrets_store import SecretStore
from coding_agent.session_brief import build_session_brief
from coding_agent.steering import SteeringCommand, SteeringController
from coding_agent.tools.artifacts import (
    CallArtifact,
    FileArtifact,
    InterruptedArtifact,
    ShellArtifact,
    SubAgentArtifact,
    parse_artifact,
)
from coding_agent.tools.delegate import DELEGATE_EXPLORE
from coding_agent.tools.shell import SHELL_TOOL_NAME
from coding_agent.workspace.model import normalize_workspace_kind
from coding_agent.workspace_lock import WorkspaceLock

_LOG = logging.getLogger("coding_agent.runtime")


def _swallow_audit(site: str, exc: BaseException) -> None:
    """best-effort 审计/清理分支的统一出口：吞掉可以，不留痕不行。

    审计是承重不变量（哈希链 + "日志有 end 无 begin"就算验收缺陷）。写入失败
    时继续跑是对的（不该让主流程为审计陪葬），但必须计数并告警——否则
    "审计链断了"只在人工翻日志时才会被发现。
    """
    note_silent_failure(site)
    _LOG.warning("%s 的 best-effort 步骤失败，已吞掉：%s", site, exc)

# 只有这几个 action 才真正改动了文件，read 与空回滚不发 FileChanged
MUTATING_FILE_ACTIONS = frozenset(
    {
        "create",
        "overwrite",
        "edit",
        "move",
        "copy",
        "delete",
        "mkdir",
        ACTION_RESTORE,
    }
)

# 只有这两个节点的模型输出是给用户看的文本；planner 的结构化调用不流式渲染
STREAMING_NODES = frozenset({"act", "respond"})

_PREVIEW_CHARS = 240
_FAILURE_MARKERS = ("工具执行异常：", "错误：")
# 同一 runtime 同时只允许一个事件流。工作区锁是可重入的，不能替代这层保护：
# Web 端并发 resume 会同时驱动同一个 LangGraph/checkpointer。
_STREAM_BUSY_MESSAGE = (
    "该会话已有正在执行的事件流；请等待当前运行结束，"
    "或先消费挂起/审批事件。"
)
_SUMMARY_KEYS = ("command", "path", "pattern", "query")
# planner 没解析出步骤、退化成单步时记进审计的 detail，供事后复盘
_PLAN_DEGRADED_DETAIL = "规划未解析，已退化为单步执行"
# 验证是否「放行」的唯一口径。与路由判定同源（graph/routing.route_after_verify
# 只在 failed 时拦下任务）：skipped / not_configured 表示**没验证**，不是**验证
# 失败**。曾经事件层按前者记、审计层按后者记，于是同一次 skipped 在两边结论相反，
# 事后对账对不上 —— 审计本该是可信来源，所以两处都必须走这个函数。
_VERIFICATION_BLOCKING_STATUS = "failed"


def _verification_passed(raw: dict[str, Any]) -> bool:
    return str(raw.get("status", "skipped")) != _VERIFICATION_BLOCKING_STATUS


def _verification_verdict(raw: dict[str, Any]) -> bool | None:
    """**审计**里的验证结论：True 通过 / False 失败 / **None 没验证**。

    与 `_verification_passed`（路由口径）刻意分开：路由只需要"要不要拦"，所以
    `not_configured` / `skipped` 都算"不拦"；但审计要回答的是"这次到底验证了没有"，
    把"没做"记成 `ok=True` 会让**验证有缺口在账上看不出来** —— 而审计本该是可信
    来源，这正是本项目最不能接受的失败模式之一（原 IMPROVEMENT_PLAN 的 P0-2，
    去向见 `docs/archive/README.md`）。
    """
    status = str(raw.get("status", "skipped"))
    if status == _VERIFICATION_BLOCKING_STATUS:
        return False
    if status == "ok":
        return True
    # error / not_configured / skipped：没验证成功，既不是通过也不是测试失败
    return None


# 审查是否「拦下了这一步」的唯一口径。与路由同源：只有 blocked 才改控制流，
# warned / clean / skipped 都只是记录。审查没能执行（status=warned 且带
# review-unavailable）**不算阻断** —— 工具坏了不该把用户的任务卡死。
_REVIEW_BLOCKING_STATUS = "blocked"


def _review_blocked(raw: dict[str, Any]) -> bool:
    return str(raw.get("status", "skipped")) == _REVIEW_BLOCKING_STATUS


@dataclass(frozen=True, slots=True)
class HistoryMessage:
    """还原历史会话时用的一轮对话。"""

    role: str  # user | assistant
    text: str


def _last_message(update: dict[str, Any]) -> Any:
    messages = update.get("messages") or []
    return messages[-1] if messages else None


def _worker_key(result: dict[str, Any]) -> str:
    """去重键：同一任务的重试是不同的 worker 运行。"""
    return (
        f"{result.get('worker_id', '')}:"
        f"{int(result.get('attempt', 1) or 1)}"
    )


def _preview(text: str, limit: int = _PREVIEW_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else f"{text[:limit]}…"


def _summarize_args(args: dict[str, Any]) -> str:
    for key in _SUMMARY_KEYS:
        if args.get(key):
            return str(args[key])
    return ", ".join(f"{k}={v}" for k, v in list(args.items())[:3])


def _add_usage(message: Any, input_tokens: int, output_tokens: int) -> tuple[int, int]:
    """把模型回报的用量累加进来（provider 没给就是 0）。

    用 provider 的 `usage_metadata` 而不是 tokenizer：无需额外依赖，
    口径也就是实际计费口径。
    """
    meta = getattr(message, "usage_metadata", None) or {}
    return (
        input_tokens + int(meta.get("input_tokens") or 0),
        output_tokens + int(meta.get("output_tokens") or 0),
    )


def submit_steering_command(
    settings: Settings,
    thread_id: str,
    command: SteeringCommand,
    *,
    workspace: str | None = None,
) -> None:
    """跨进程入口：先落审计，再写入 steering 队列。

    供 `agent steer` 使用。刻意不构造 `AgentRuntime` —— 它需要沙箱/checkpoint
    等运行期资源，而这里只想写一条宿主侧协调数据。审计字段与
    `AgentRuntime.steer()` 保持一致（提交时落账，source 记录来源）。
    """
    audit = AuditLogger(
        settings.resolved_audit_dir,
        enabled=settings.audit_enabled,
        max_bytes=settings.audit_max_mb * 1024 * 1024,
        syslog_address=settings.audit_syslog_address,
        webhook_url=settings.audit_webhook_url,
        webhook_token=settings.audit_webhook_token,
    )
    detail = (
        command.action
        if not command.text
        else f"{command.action}: {command.text}"
    )
    audit.write(
        AuditRecord(
            ts=now_iso(),
            kind=audit_models.STEERING,
            thread_id=thread_id,
            workspace=workspace or "",
            detail=truncate(detail),
            source=command.source,
        )
    )
    SteeringController(settings.resolved_steering_dir).submit(
        thread_id, command
    )


@dataclass
class _StreamCtx:
    """一次 `_stream` 内跨节点共享的可变状态。

    `progress` 按 thread 留存（挂起/恢复是两次 `_stream`）；其余字段是本次流
    的游标，事件处理器直接读写它们，避免把十几个 `nonlocal` 散在函数里。
    """

    runtime: AgentRuntime
    thread_id: str
    progress: dict[str, Any]
    pending: dict[str, ToolCallStarted]
    streamed: list[str]
    plan: list[str]
    plan_kinds: list[str]
    step_idx: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    run_epoch: str = ""
    answer: str = ""
    last_verification: dict[str, Any] = field(default_factory=dict)
    last_review: dict[str, Any] = field(default_factory=dict)
    step_open: bool = False


@dataclass
class _CostSummary:
    """收尾时的成本、账单与告警。"""

    usage: dict[str, Any]
    budget: float
    ratio: float
    alerts: list[str]
    billed_usd: float
    billing_delta: float
    billing_error: str
    billing_providers: dict[str, dict[str, float]]


class AgentRuntime(SnapshotMixin):
    """四层架构的编排中枢。

    `SnapshotMixin` 提供快照/回滚/差异/上传留底那一组方法；拆出去是为了让
    这个文件不再因为"加一个快照命令"就继续变长，见 `runtime_snapshots.py`。
    """
    """跑一轮任务，产出事件流。"""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        workspace: str | None = None,
        allow_write: bool = False,
        checkpointer: Any = None,
        audit: AuditLogger | None = None,
        approval_mode: str | None = None,
        governor: ConcurrencyGovernor | None = None,
        access_mode: str | None = None,
        model_override: str = "",
        provider_name: str = "",
        secret_store: SecretStore | None = None,
        file_access: Any | None = None,
        execution_targets: dict[str, Any] | None = None,
        workspace_kind: str = "sandbox",
        host_fs: Any | None = None,
        host_snapshots: Any | None = None,
    ) -> None:
        self.settings = apply_security_profile(settings or get_settings())
        # production/ops 是只读审计档：无论调用方传了什么，都强制关闭写能力
        # 并把审批模式钉在 deny。否则 `agent run --write` 或 Web 的显式参数
        # 会把 profile 的保证绕过去。
        if (
            str(self.settings.security_profile or "").strip().lower()
            in PRODUCTION_PROFILES
        ):
            allow_write = False
            approval_mode = "deny"
            access_mode = ACCESS_READ_ONLY
        # 会话级访问模式：GUI/Web 的默认值。
        # - read_only 只暴露只读工具，并在策略层拒绝 shell/tests/git/deps
        # - write_ask 仍暴露写工具，但 L1/L2 逐条审批
        # - write_auto 等价于传统的 --write（L1 自动）
        # 空串保留旧语义，避免改变 CLI/TUI 的行为。
        self.access_mode = normalize_access_mode(access_mode)
        if self.access_mode == ACCESS_READ_ONLY:
            allow_write = False
            # 只读 = 不运行。自动 verify 会执行测试命令，编排 implement 会写
            # 工作区；必须在 runtime 入口就关掉，不能只靠工具审批。
            self.settings = self.settings.model_copy(
                update={
                    "verify_enabled": False,
                    "orchestrator_enabled": False,
                }
            )
        elif self.access_mode in {ACCESS_WRITE_ASK, ACCESS_WRITE_AUTO}:
            # 暴露写工具；是否放行由 SessionPolicy 决定。
            allow_write = True
        GLOBAL_LIMITER.configure(self.settings.resolved_rate_limit_state_path)
        # 一次运行共享一套资源池：worker / 主 agent 工具 / WSL / LLM。
        # 多 runtime 的宿主（Web）应传入同一个 governor，否则 WSL/LLM 上限
        # 会按 runtime 数量相乘。
        self.concurrency = governor or ConcurrencyGovernor(self.settings)
        self.allow_write = allow_write
        # 会话级模型选择：只影响主模型（worker 未单独配置时沿用同一实例）。
        self.model_override = str(model_override or "").strip()
        self.provider_name = str(provider_name or "").strip()
        self.secret_store = secret_store
        self.file_access = file_access
        self.execution_targets = dict(execution_targets or {})
        self.workspace_kind = normalize_workspace_kind(workspace_kind)
        self.host_fs = host_fs
        self.host_snapshots = host_snapshots
        self.policy = SessionPolicy(
            allow_write=allow_write,
            approval_mode=approval_mode or self.settings.approval_mode,
            allow_rules=parse_allow_rules(self.settings.allow_rules),
            read_deny_globs=self.settings.read_deny_globs,
            security_profile=self.settings.security_profile,
            access_mode=self.access_mode,
        )
        self._sandbox = build_sandbox(
            self.settings, slots=self.concurrency.wsl
        )
        self._workspace = normalize_root(workspace) if workspace else None
        # Phase C：workspace 解析后把 sandbox 包成 ExecutionService 代理。
        self._execution: ExecutionService | None = None
        self._wrap_execution()
        self._graph: Any = None
        # Phase 3D：用户干预队列。图节点与 runtime 共用同一实例；
        # 不放进 state —— 它是外部输入，且 worker 需要在运行中非破坏性轮询。
        self.steering = SteeringController(self.settings.resolved_steering_dir)
        # 仓库画像每个 runtime 只探一次；工作区在本实例生命周期内固定。
        self._repo_profile_cache: RepoProfile | None = None
        self._snapshots: SnapshotStore | None = None
        self._memory: SemanticMemory | None = None
        self._fs: SandboxFs | None = None
        # thread_id -> {call_id: ToolCallStarted}。
        # 必须活在实例上而不是 `_stream` 局部：需要审批的调用在挂起时结束一次
        # `_stream`，resume 时才在另一次 `_stream` 里执行。若只存局部变量，
        # 审批恢复后的 tool_call 审计会丢掉命令参数（最高风险的那批调用）。
        self._pending_calls: dict[str, dict[str, ToolCallStarted]] = {}
        # thread_id -> {plan, step_idx, last_verification}。
        # 同理：挂起与恢复是两次 `_stream`，这些"用于翻译事件"的状态若只存局部，
        # 恢复后的 StepStarted 会退化成空文案、repair 事件会丢 summary。
        self._progress: dict[str, dict[str, Any]] = {}
        # thread_id -> 已 resume 次数（fresh run 归零），用于封顶挂起-恢复循环
        self._resume_counts: dict[str, int] = {}
        self._workspace_lock: WorkspaceLock | None = None
        self._workspace_lock_error = ""
        # 同一 runtime 的 run/resume 不能并发驱动同一张图。
        self._stream_active = False

        # 显式传入的 checkpointer 由调用方负责生命周期；否则按配置自行管理
        self._external_checkpointer = checkpointer
        self._store = (
            None
            if checkpointer is not None
            else CheckpointStore(self.settings.resolved_checkpoint_path)
        )
        self.audit = audit or AuditLogger(
            self.settings.resolved_audit_dir,
            enabled=self.settings.audit_enabled,
            max_bytes=self.settings.audit_max_mb * 1024 * 1024,
            syslog_address=self.settings.audit_syslog_address,
            webhook_url=self.settings.audit_webhook_url,
            webhook_token=self.settings.audit_webhook_token,
        )

    @property
    def workspace(self) -> str:
        """沙箱内的工作区绝对路径，按需解析并缓存。"""
        if self._workspace is None:
            self._workspace = resolve_workspace(self.settings, self._sandbox)
            # bwrap 要求 workdir 存在；默认 $HOME/agent-ws 可能尚未创建。
            if self.settings.shell_sandbox == "bwrap":
                self._sandbox.ensure_directory(self._workspace)
            # bwrap 的 workdir/可写绑定来自 sandbox.settings.wsl_workspace。
            # 配置留空时由 resolve_workspace 推导，必须写回沙箱，否则
            # untrusted 模式下所有不带 cwd 的文件/检索脚本都会失败。
            self._sandbox.settings = self.settings.model_copy(
                update={"wsl_workspace": self._workspace}
            )
        self._wrap_execution()
        return self._workspace

    def _wrap_execution(self) -> None:
        """只包装一次：既有工具继续调用 sandbox.run，实际走 A4 backend。"""
        if self._execution is not None or not self._workspace:
            return
        service = ExecutionService(
            SandboxExecutionBackend(
                self._sandbox,
                self._workspace,
                manifest_root=self.settings.resolved_snapshot_manifest_dir,
            ),
            self._workspace,
            targets=self.execution_targets,
            workspace_kind=self.workspace_kind,
        )
        self._execution = service
        self._sandbox = ExecutionSandbox(self._sandbox, service)

    @property
    def execution(self) -> ExecutionService | None:
        return self._execution

    def _repo_profile(self) -> RepoProfile:
        """仓库画像（文件数/测试文件数），按 runtime 缓存。"""
        if self.workspace_kind == "host":
            # Phase 1 host 只读不跑沙箱命令；画像未知而不是误探 Windows 路径。
            return RepoProfile()
        if self._repo_profile_cache is None:
            self._repo_profile_cache = profile_workspace(self._sandbox, self.workspace)
        return self._repo_profile_cache

    @property
    def audit_path(self) -> Path:
        return self.audit.path

    def audit_files(self) -> list[Path]:
        """当天审计文件的全部片段（含轮转），供 `/audit` 等读取。"""
        return self.audit.files_today()

    def _trace_metadata(self, thread_id: str) -> dict[str, Any]:
        """挂在 trace 上的运行上下文。

        只放标识与开关，**不放提示词或文件内容** —— 追踪数据会离开本机。
        """
        return {
            "thread_id": thread_id,
            "workspace": self.workspace,
            "model": self.settings.deepseek_model,
            "allow_write": self.allow_write,
            "approval_mode": self.policy.approval_mode,
            "verify": self.settings.verify_command or "auto",
            "persistent": self.persistent,
            "orchestrator": self.settings.orchestrator_enabled,
        }

    def _memory_root(self) -> str:
        return resolve_memory_root(
            self.settings, self._sandbox, self.workspace
        )

    def _global_memory_root(self) -> str:
        return resolve_global_memory_root(self.settings, self._sandbox)

    def _ensure_memory_bind(self) -> None:
        """记忆在仓库外时，bwrap 需要显式可写绑定。"""
        roots = [self._memory_root()]
        global_root = self._global_memory_root()
        if global_root and global_root not in roots:
            roots.append(global_root)
        for root in roots:
            if root == self.workspace:
                continue
            self._sandbox.ensure_directory(root)
            self._sandbox.add_extra_bind(root)

    @property
    def memory(self) -> SemanticMemory:
        """项目记忆 + 可选全局记忆 + 可选向量检索。"""
        if self._memory is None:
            self._ensure_memory_bind()
            memory_root = self._memory_root()
            project = LongTermMemory(
                SandboxFs(self._sandbox, memory_root),
                self.workspace,
                state_root=memory_root,
            )
            global_memory = None
            global_root = self._global_memory_root()
            if global_root:
                global_memory = LongTermMemory(
                    SandboxFs(self._sandbox, global_root),
                    global_root,
                    state_root=global_root,
                )
            self._memory = wrap_memory(
                LayeredMemory(project, global_memory), self.settings
            )
        return self._memory

    @property
    def sessions(self) -> SessionIndex:
        """历史会话列表（从审计日志归纳）。"""
        return SessionIndex(self.settings.resolved_audit_dir)

    def _audit_memory(
        self, thread_id: str, detail: str, *, source: str = "user"
    ) -> None:
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.MEMORY,
                thread_id=thread_id,
                detail=truncate(detail),
                source=source,
            )
        )

    def remember(
        self,
        text: str,
        *,
        thread_id: str = "",
        reason: str = "",
        ttl_days: int | None = None,
        scope: str = "project",
    ) -> list[str]:
        facts = self.memory.add(
            text,
            source="user",
            reason=reason,
            thread_id=thread_id,
            ttl_days=ttl_days,
            scope=scope,
        )
        detail = f"add[{scope}]: {text}"
        if reason:
            detail += f"；原因: {reason}"
        if self.memory.last_replaced:
            detail += "；替换: " + "；".join(self.memory.last_replaced)
        self._audit_memory(thread_id, detail)
        return facts

    def forget(
        self, index: int, *, thread_id: str = "", scope: str = "project"
    ) -> list[str]:
        facts = self.memory.remove(index, scope=scope)
        removed = self.memory.last_removed
        self._audit_memory(
            thread_id,
            f"forget[{scope}] #{index}: {removed}"
            if removed
            else f"forget[{scope}] #{index}",
        )
        return facts

    def restore_memory(
        self,
        backup_id: str,
        *,
        thread_id: str = "",
        scope: str = "project",
    ) -> list[str]:
        facts = self.memory.restore(backup_id, scope=scope)
        self._audit_memory(thread_id, f"restore[{scope}]: {backup_id}")
        return facts

    def pin_memory(
        self,
        index: int,
        *,
        pinned: bool,
        thread_id: str = "",
        scope: str = "project",
    ) -> list[str]:
        facts = self.memory.set_pinned(index, pinned, scope=scope)
        self._audit_memory(
            thread_id,
            f"{'pin' if pinned else 'unpin'}[{scope}] #{index}",
        )
        return facts

    def set_memory_priority(
        self,
        index: int,
        priority: int,
        *,
        thread_id: str = "",
        scope: str = "project",
    ) -> list[str]:
        facts = self.memory.set_priority(index, priority, scope=scope)
        self._audit_memory(
            thread_id, f"priority[{scope}] #{index}={priority}"
        )
        return facts

    def clear_memory(
        self, *, thread_id: str = "", scope: str = "project"
    ) -> list[str]:
        facts = self.memory.clear(scope=scope)
        self._audit_memory(thread_id, f"clear[{scope}]")
        return facts

    def memories(self, *, scope: str | None = None) -> list[str]:
        return self.memory.load(scope=scope)

    async def history(
        self,
        thread_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[HistoryMessage]:
        """从 checkpoint 还原某个会话的对话（供 `/switch` 展示）。

        只取人和助手的自然语言往来；工具调用与工具结果不展示 ——
        它们是过程噪声，重新渲染只会淹没真正的对话。

        `limit`/`offset` 对**过滤后的对话**分页（按时间正序），长会话前端
        不必一次拉回全部历史；`limit=None` 保持旧行为。

        刻意**直读 checkpoint 而不建图**：回放历史不该要求 API Key 或可用的沙箱
        （`_ensure_graph` 会拉起 `build_llm` 与 WSL 探测）。
        耦合点：直接取 `channel_values["messages"]`，与 `AgentState.messages` 对应。
        """
        saver = await self._checkpointer()
        fetch = getattr(saver, "aget_tuple", None)
        if fetch is None:
            return []
        tuple_ = await fetch({"configurable": {"thread_id": thread_id}})
        if tuple_ is None:
            return []
        checkpoint = getattr(tuple_, "checkpoint", None) or {}
        messages = (checkpoint.get("channel_values") or {}).get("messages") or []

        history: list[HistoryMessage] = []
        for message in messages:
            if isinstance(message, HumanMessage):
                text = text_of(message)
                if text.strip():
                    history.append(HistoryMessage("user", text))
            elif isinstance(message, AIMessage) and not getattr(message, "tool_calls", None):
                text = text_of(message)
                if text.strip():
                    history.append(HistoryMessage("assistant", text))
        offset = max(0, int(offset))
        if offset:
            history = history[offset:]
        if limit is not None:
            history = history[: max(0, int(limit))]
        return history

    async def final_state(self, thread_id: str) -> dict[str, Any]:
        """读取该 thread 最近一次 checkpoint 的状态。

        只读、不建图；评测用它取上下文画像（repo_map / worker 摘要 / 裁剪
        报告）。运行失败或 checkpoint 不可用时返回空 dict。
        """
        return await self._prior_state(thread_id)

    async def _prior_state(self, thread_id: str) -> dict[str, Any]:
        """读取该 thread 上一次 checkpoint 的状态值。

        与 `history()` 一样直读 checkpoint，不建图。读取失败只是少了一段
        上下文与一次悬空调用修复的机会，不能阻断本次运行。
        """
        try:
            saver = await self._checkpointer()
            fetch = getattr(saver, "aget_tuple", None)
            if fetch is None:
                return {}
            tuple_ = await fetch({"configurable": {"thread_id": thread_id}})
            if tuple_ is None:
                return {}
            checkpoint = getattr(tuple_, "checkpoint", None) or {}
            return dict(checkpoint.get("channel_values") or {})
        except Exception:  # noqa: BLE001 - 读取失败不阻断运行
            return {}

    def _recovery_notice(self, prior: dict[str, Any]) -> str:
        """上一轮是否中断，以及工作区可能残留什么。

        不自动回滚、也不自动重跑：只把事实交给模型和用户。自动回滚会在用户
        已经手工改过文件时造成二次破坏；自动重跑则可能重复非幂等副作用。
        """
        if str(prior.get("run_status") or "") != "running":
            return ""
        # 只在"上一轮确实没收尾"时去读合并登记：读一次要起一次沙箱进程，
        # 不能挂在每次 run 的启动路径上。
        journal_notice = ""
        try:
            journal_notice = MergeJournal(self._sandbox, self.workspace).notice()
        except Exception:  # noqa: BLE001 - 登记读不出来不该挡住运行
            journal_notice = ""
        verification = prior.get("last_verification") or {}
        status = str(verification.get("status") or "未验证")
        paths = [
            str(item) for item in (prior.get("stale_paths") or []) if str(item)
        ]
        notice = (
            "上一次运行没有正常收尾（可能因网络、沙箱或进程中断）。"
            f"工作区可能有未完成的改动，最后一次验证状态：{status}。"
            "继续之前先确认文件当前内容；涉及实现时先跑验证，"
            "不要把上一步当作已完成。"
        )
        if paths:
            notice += " 上次运行修改过的文件：" + "、".join(paths[:10])
            if len(paths) > 10:
                notice += f" 等 {len(paths)} 个"
        if journal_notice:
            notice += f" {journal_notice}"
        return notice

    async def checkpoints(
        self, thread_id: str, *, limit: int = 20
    ) -> list[dict[str, Any]]:
        """列出会话的 checkpoint（新的在前），供 fork/rewind 选择落点。

        直读 checkpointer 的 `alist`，不建图：列个历史不该要求 API Key
        或可用的沙箱（与 `history()` 同一考虑）。
        """
        saver = await self._checkpointer()
        config = {"configurable": {"thread_id": thread_id}}
        found: list[dict[str, Any]] = []
        async for item in saver.alist(config, limit=limit or None):
            checkpoint = getattr(item, "checkpoint", None) or {}
            metadata = getattr(item, "metadata", None) or {}
            values = checkpoint.get("channel_values") or {}
            preview = ""
            for message in reversed(values.get("messages") or []):
                if isinstance(message, (HumanMessage, AIMessage)):
                    text = text_of(message).strip()
                    if text:
                        preview = " ".join(text.split())[:120]
                        break
            found.append(
                {
                    "checkpoint_id": str(checkpoint.get("id", "")),
                    "step": int(metadata.get("step", 0) or 0),
                    "created_at": str(metadata.get("created_at", "")),
                    "preview": preview,
                }
            )
            if limit > 0 and len(found) >= limit:
                break
        return found

    async def _resolve_snapshot(
        self,
        graph: Any,
        thread_id: str,
        *,
        checkpoint_id: str = "",
        steps: int = 1,
    ) -> Any:
        config = {"configurable": {"thread_id": thread_id}}
        if not checkpoint_id:
            if steps <= 0:
                return await graph.aget_state(config)
            index = 0
            async for snapshot in graph.aget_state_history(config):
                if index == steps:
                    return snapshot
                index += 1
            raise ValueError(
                f"会话 {thread_id} 没有第 {steps} 个历史 checkpoint。"
            )
        async for snapshot in graph.aget_state_history(config):
            found = (
                (getattr(snapshot, "config", {}) or {})
                .get("configurable", {})
                .get("checkpoint_id", "")
            )
            if str(found) == checkpoint_id:
                return snapshot
        raise ValueError(f"找不到 checkpoint：{checkpoint_id}")

    async def fork(
        self,
        thread_id: str,
        *,
        checkpoint_id: str = "",
        new_thread_id: str = "",
    ) -> str:
        """从当前或指定 checkpoint 分叉出一个新会话，返回新 thread_id。

        分叉复制的是**状态值**（消息、计划等），原会话不受影响。
        """
        graph = await self._ensure_graph()
        source = await self._resolve_snapshot(
            graph, thread_id, checkpoint_id=checkpoint_id, steps=0
        )
        target = new_thread_id.strip() or (
            f"{thread_id}-fork-{uuid4().hex[:8]}"
        )
        await graph.aupdate_state(
            {"configurable": {"thread_id": target}},
            dict(getattr(source, "values", None) or {}),
            as_node=None,
        )
        return target

    async def compact_thread(
        self,
        thread_id: str,
        *,
        keep_recent: int = 6,
        new_thread_id: str = "",
    ) -> str:
        """把较早的对话压成一条确定性摘要，并分叉出一个新会话。

        不原地删除消息：原 thread 完整保留。新 thread 只带摘要 + 最近若干轮
        **纯文本**消息（不含 tool_calls / ToolMessage），因此不会破坏
        `tool_calls ↔ tool_call_id` 配对。
        """
        graph = await self._ensure_graph()
        source = await graph.aget_state(
            {"configurable": {"thread_id": thread_id}}
        )
        values = dict(getattr(source, "values", None) or {})
        messages = list(values.get("messages") or [])
        summary = summarize_messages(
            messages,
            max_chars=max(500, int(self.settings.session_brief_chars or 4000)),
        )
        keep = max(1, min(int(keep_recent), 50))
        recent: list[Any] = []
        human_count = 0
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                human_count += 1
                if human_count > keep:
                    break
                recent.append(HumanMessage(content=text_of(message)))
            elif (
                isinstance(message, AIMessage)
                and not getattr(message, "tool_calls", None)
            ):
                text = text_of(message)
                if text:
                    recent.append(AIMessage(content=text))
        recent.reverse()
        # 新会话必须从用户消息开始；孤立的前置 AI 文本没有上下文意义。
        while recent and not isinstance(recent[0], HumanMessage):
            recent.pop(0)

        summary_text = (
            "【会话压缩摘要】\n" + summary
            if summary
            else "【会话压缩摘要】原会话没有可提炼的文本。"
        )
        new_messages: list[Any] = [HumanMessage(content=summary_text), *recent]
        target = new_thread_id.strip() or (
            f"{thread_id}-compact-{uuid4().hex[:8]}"
        )
        reset: dict[str, Any] = {
            "messages": new_messages,
            "request": "",
            "plan": [],
            "plan_kinds": [],
            "step_idx": 0,
            "plan_degraded": False,
            "replan_count": 0,
            "tweak_count": 0,
            "tool_rounds": 0,
            "budget_exhausted": False,
            "dirty": False,
            "run_dirty": False,
            "pending_verification": False,
            "empty_step_nudged": False,
            "verification": {},
            "review": {},
            "review_watermark": "",
            "retry": 0,
            "same_failure_count": 0,
            "last_failure_signature": "",
            "last_verification": {},
            "stale_paths": [],
            "approvals": {},
            "run_status": "finished",
            "session_brief": "",
            "recovery_notice": "",
            "project_instructions": "",
            "memories": [],
            # 编排/委派/steering 都是 run 级状态，不能从原 thread 带过来。
            "transient_retries": 0,
            "delegate_calls": 0,
            "delegation_allowed": False,
            "delegation_budget": 0,
            "execution_mode": "direct",
            "workflow": "",
            "workflow_reason": "",
            "repo_summary": {},
            "prefetched_tasks": [],
            "prefetched_degraded": False,
            "tasks": [],
            "task_status": {},
            "task_attempts": {},
            "worker_results": [],
            "orchestrator_round": 0,
            "orchestrator_degraded": False,
            "orchestrator_summary": "",
            "orchestrator_completed": 0,
            "orchestrator_failed": 0,
            "worker_context": "",
            "orchestration_handoff": {},
            "task_extensions_seen": 0,
            "task_extensions_applied": [],
            "task_extension_errors": [],
            "steering_cancelled": False,
            "steering_replan": False,
            "steering_replans": 0,
            "steering_applied": [],
            "workflow_revised_reason": "",
        }
        values.update(reset)
        await graph.aupdate_state(
            {"configurable": {"thread_id": target}},
            values,
            as_node=None,
        )
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.COMPACT,
                thread_id=thread_id,
                detail=truncate(
                    f"compact -> {target}；保留最近 {keep} 轮，"
                    f"摘要 {len(summary)} 字符"
                ),
            )
        )
        return target

    async def rewind(
        self,
        thread_id: str,
        *,
        checkpoint_id: str = "",
        steps: int = 1,
    ) -> str:
        """把会话回退到更早的 checkpoint（默认退一步），返回新 checkpoint id。

        实现方式是**在回退点上新建一个分支**，历史记录不会被删除 ——
        误回退时仍可通过 checkpoint id 找回来。
        """
        graph = await self._ensure_graph()
        target = await self._resolve_snapshot(
            graph, thread_id, checkpoint_id=checkpoint_id, steps=steps
        )
        updated = await graph.aupdate_state(
            target.config, {}, as_node=None
        )
        return str(
            (updated or {}).get("configurable", {}).get("checkpoint_id", "")
        )

    async def _prune_checkpoints(self, thread_id: str) -> None:
        """按 `checkpoint_keep` 轮转当前 thread 的 checkpoint。

        失败只影响磁盘占用，不能影响已经完成的这一轮 —— 因此静默吞掉。
        """
        keep = int(getattr(self.settings, "checkpoint_keep", 0) or 0)
        if keep <= 0 or self._store is None or not self._store.persistent:
            return
        try:
            await self._store.aprune(thread_id, keep)
        except Exception:  # noqa: BLE001 - 轮转是维护动作，不阻断会话
            pass

    @property
    def persistent(self) -> bool:
        """会话是否跨进程可恢复。"""
        return self._store is not None and self._store.persistent

    async def aclose(self) -> None:
        """释放 sqlite 连接。显式传入 checkpointer 时由调用方负责。

        前端可以不调用：每次写入都已 commit，进程退出不会丢已落盘的 checkpoint。
        """
        self._release_workspace_lock()
        if self._execution is not None:
            self._execution.close()
            self._execution = None
        if self._store is not None:
            await self._store.aclose()
            self._graph = None

    def _forget_thread(self, thread_id: str) -> None:
        """运行收尾后清掉按 thread_id 累积的状态。

        `_pending_calls` / `_progress` 只为「挂起与恢复是两次 `_stream`」而存在
        （见 `__init__` 注释），一旦这轮以 RunFinished / RunFailed 收场就再无用处。
        长会话（TUI 一个进程跑几十轮）里不清会一直累积，而且 `_pending_calls`
        留着还会让下一轮误以为有旧的待配对调用。

        `_resume_counts` **故意不清**：它封顶的是「同一 thread 反复挂起-恢复」，
        跑完一轮就清零等于把护栏拆了（前端跑完再 resume 就能无限续）。它每
        thread 只占一个 int，本来也不是内存问题的所在。
        """
        self._pending_calls.pop(thread_id, None)
        self._progress.pop(thread_id, None)
        # 一轮结束：未消费的迟到指令与暂停标记都作废，但丢弃动作留审计。
        discarded = self.steering.clear(thread_id)
        if discarded:
            self._audit_discarded_steering(thread_id, discarded, "run 结束")

    def _audit_discarded_steering(
        self,
        thread_id: str,
        commands: list[Any],
        reason: str,
    ) -> None:
        """记录被丢弃的 steering 指令，避免“提交过但没生效”无迹可查。"""
        detail = "、".join(
            f"{getattr(item, 'action', '')}"
            + (f": {getattr(item, 'text', '')}" if getattr(item, "text", "") else "")
            for item in commands
        )
        try:
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.STEERING,
                    thread_id=thread_id,
                    detail=truncate(f"{reason}，丢弃指令：{detail}"),
                    source="runtime",
                )
            )
        except Exception as exc:  # noqa: BLE001 - 提交时已审计，清理审计不阻断收尾
            _swallow_audit("audit_discarded_steering", exc)

    @property
    def effective_settings(self) -> Settings:
        """把解析后的工作区写回配置。

        工具的路径守卫是从 settings 里读工作区的，若这里不写回，
        `--workspace` 覆盖只会影响图状态里的 cwd，工具仍按旧根目录判边界，
        结果是所有命令都被判「工作目录非法」。
        """
        update: dict[str, Any] = {"wsl_workspace": self.workspace}
        if self.workspace_kind == "host":
            # host 的 verify/tests 仍会走 WSL，不能拿它验证 NTFS 工作区。
            update["verify_enabled"] = False
            update["orchestrator_enabled"] = False
        if self.access_mode == ACCESS_READ_ONLY:
            # 只读 = 不运行。自动 verify 会执行测试命令，编排 implement 会写
            # 工作区；两者都必须在配置层关掉，不能只靠 approval_gate。
            update["verify_enabled"] = False
            update["orchestrator_enabled"] = False
        return self.settings.model_copy(update=update)

    async def _checkpointer(self) -> Any:
        """本次运行用的 checkpoint 后端：外部传入的优先，否则自管 sqlite。"""
        if self._external_checkpointer is not None:
            return self._external_checkpointer
        return await self._store.aopen()

    async def _ensure_graph(self) -> Any:
        """编译后的图，首次使用时才构建。

        构造 AgentRuntime 不应触发建图 —— 否则只想读 workspace 或渲染 UI 的
        前端也会被迫要求 API Key、并做一次无谓的沙箱探测。
        """
        if self._graph is None:
            self._graph = build_graph(
                self.effective_settings,
                checkpointer=await self._checkpointer(),
                allow_write=self.allow_write,
                policy=self.policy,
                # 与 runtime 共用一份沙箱：探测缓存挂在实例上，各建一份就各探一遍
                sandbox=self._sandbox,
                steering=self.steering,
                governor=self.concurrency,
                model_override=self.model_override,
                provider_name=self.provider_name,
                secret_store=self.secret_store,
                file_access=self.file_access,
                host_mode=self.workspace_kind == "host",
                host_fs=self.host_fs,
                host_snapshots=self.host_snapshots,
                host_exec=self.execution_targets.get("host"),
            )
        return self._graph

    # ------------------------------------------------------------------
    # 事件流
    # ------------------------------------------------------------------

    def _acquire_workspace_lock(self) -> bool:
        if not self.settings.workspace_lock_enabled:
            return True
        if self._workspace_lock is not None:
            return True
        try:
            lock = WorkspaceLock(self.audit.directory, self.workspace)
            acquired = lock.acquire()
        except Exception as exc:  # noqa: BLE001
            # 锁不可用意味着无法证明“只有一个写者”，必须 fail closed；
            # 但错误信息要指向真实原因，不能说成“工作区被占用”。
            self._workspace_lock_error = (
                f"工作区单写者锁不可用：{type(exc).__name__}: {exc}"
            )
            return False
        if not acquired:
            return False
        self._workspace_lock = lock
        self._workspace_lock_error = ""
        return True

    def _release_workspace_lock(self) -> None:
        if self._workspace_lock is not None:
            self._workspace_lock.release()
            self._workspace_lock = None

    @property
    def _workspace_lock_busy(self) -> str:
        if self._workspace_lock_error:
            return self._workspace_lock_error
        return (
            "该工作区已有另一个运行中的会话（工作区单写者锁）。"
            "请等待它结束，或在另一个工作区运行。"
        )

    async def run(
        self, prompt: str, *, thread_id: str, plan_only: bool = False
    ) -> AsyncIterator[Event]:
        """跑一轮任务（持有工作区单写者锁）。

        `plan_only=True` 只生成计划并立即正常收尾，不执行任何工具或写操作。
        事件流以 `RunFinished` / `RunFailed` 收尾；若以 `ApprovalRequested`
        收尾，说明图已挂起等待人工确认，前端应收集答复后调用 `resume()`。
        """
        if self._stream_active:
            yield RunFailed(message=_STREAM_BUSY_MESSAGE)
            return
        if not self._acquire_workspace_lock():
            yield RunFailed(message=self._workspace_lock_busy)
            return
        # 持有单写者锁时清理旧快照，避免多个 runtime 并发删除。
        self.prune_snapshots()
        self._stream_active = True
        try:
            async for event in self._run_unlocked(
                prompt, thread_id=thread_id, plan_only=plan_only
            ):
                yield event
        finally:
            self._stream_active = False
            self._release_workspace_lock()

    async def _run_unlocked(
        self, prompt: str, *, thread_id: str, plan_only: bool = False
    ) -> AsyncIterator[Event]:
        # 新一轮先作废旧指令。**必须放在任何耗时初始化之前**：Web 在 run 的
        # SSE 响应建立后、TUI 在 _execute 一开始就能提交 /pause /cancel，
        # 而记忆读取、仓库画像探测、checkpoint 回读可能要数秒 —— 放到后面
        # 会把这段时间里提交的指令静默清掉（审计只留下"用户提交过"）。
        #
        # run_id 让“新旧”有明确边界：旧 run 的 add/replan 会被清掉，本 run
        # 开始后提交的指令则会自动绑定这个 id；清理结果落审计。
        stale = self.steering.begin_run(thread_id, uuid4().hex[:12])
        if stale:
            self._audit_discarded_steering(
                thread_id, stale, "新 run 开始"
            )

        # 读记忆是增强信息：沙箱/文件异常时按空记忆继续，不能因此中断整轮。
        # 与 worker 的 `_memories()`、分层指令的降级策略保持一致。
        try:
            memories = self.memory.search(prompt, self.settings.memory_top_k)
        except Exception as exc:  # noqa: BLE001 - 记忆不是执行前提
            memories = []
            try:
                self._audit(
                    AuditRecord(
                        ts=now_iso(),
                        kind=audit_models.RECOVERY,
                        thread_id=thread_id,
                        detail=truncate(
                            f"记忆读取失败，按空记忆继续："
                            f"{type(exc).__name__}: {exc}"
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 审计失败也不阻断运行
                _swallow_audit("audit_memory_load_failed", exc)
        unsafe = getattr(self.memory, "last_unsafe", [])
        if unsafe:
            try:
                self._audit(
                    AuditRecord(
                        ts=now_iso(),
                        kind=audit_models.RECOVERY,
                        thread_id=thread_id,
                        detail=truncate(
                            f"记忆中有 {len(unsafe)} 条疑似密钥，"
                            "已跳过注入"
                        ),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 审计失败不阻断运行
                _swallow_audit("audit_memory_unsafe_skipped", exc)

        # 分层项目指令是增强信息：发现失败（沙箱不可用、find 缺失）不影响运行
        instructions = ""
        if (
            self.settings.instructions_enabled
            and self.workspace_kind != "host"
        ):
            try:
                from coding_agent.instructions import load_project_instructions

                instructions = load_project_instructions(
                    self._sandbox,
                    self.workspace,
                    trusted=self.settings.trust_repo_instructions,
                )
            except Exception:  # noqa: BLE001 - 指令加载失败不应阻断任务
                instructions = ""

        # 成本策略 / 编排策略都需要仓库画像；只开启其中任一功能时才探一次。
        profile = (
            self._repo_profile()
            if self.settings.explore_enabled or self.settings.orchestrator_enabled
            else RepoProfile()
        )
        policy = delegation_policy(profile, self.settings)
        execution_mode = "direct"
        if (
            not plan_only
            and self.settings.orchestrator_enabled
            and profile.known
            and profile.file_count >= self.settings.orchestrator_min_files
        ):
            execution_mode = "orchestrated"

        # 上一次 checkpoint 的消息有两个用途：
        # 1) 补上崩溃前没有返回的 tool_calls，否则同一 thread 重跑时
        #    悬空的 assistant(tool_calls) 会让下一轮 API 直接 400；
        # 2) 给独立 orchestrator 子图生成有界会话约束摘要（它不复制 messages）。
        prior = await self._prior_state(thread_id)
        prior_messages = list(prior.get("messages") or [])
        recovered_tool_messages = recover_dangling_tool_calls(prior_messages)
        brief_limit = int(self.settings.session_brief_chars or 0)
        session_brief = (
            build_session_brief(prior_messages, limit=brief_limit)
            if execution_mode == "orchestrated" and brief_limit > 0
            else ""
        )
        recovery_notice = self._recovery_notice(prior)
        if recovery_notice:
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.RECOVERY,
                    thread_id=thread_id,
                    workspace=self.workspace,
                    detail=truncate(recovery_notice),
                )
            )

        inputs = {
            # 占位 ToolMessage 必须排在新用户消息之前，配对才不会错位。
            "messages": [
                *recovered_tool_messages,
                HumanMessage(content=prompt),
            ],
            # 独立 orchestrator 子图只读这一项，避免把整段消息历史复制进子图。
            "request": prompt,
            # 只展示计划：graph 在 planner 后直接收尾，不执行 act。
            "plan_only": plan_only,
            # 上一轮及更早的会话约束（有界摘要）；checkpoint 尚未写入本轮 prompt。
            "session_brief": session_brief,
            # 上一轮未正常收尾时的恢复提示；正常情况为空。
            "recovery_notice": recovery_notice,
            "run_status": "running",
            "cwd": self.workspace,
            "tool_rounds": 0,
            "transient_retries": 0,
            "budget_exhausted": False,
            "dirty": False,
            "run_dirty": False,
            # 编排合并补丁后由 scheduler 置位；新一轮必须复位。
            "pending_verification": False,
            # 本轮改过的文件会累积到这里，act 据此提醒「旧读取已过期」
            "stale_paths": [],
            "approvals": {},
            # 每轮都从文件重新读：会话中途 /remember 加的事实应当立刻生效
            "memories": memories,
            "project_instructions": instructions,
            "repo_profile": {
                "file_count": profile.file_count,
                "test_file_count": profile.test_file_count,
            },
            "delegation_allowed": policy.allowed,
            "delegation_budget": policy.max_calls,
            # 每次 run 重置编排字段，避免同一 thread 的上一轮结果串味
            "execution_mode": execution_mode,
            "workflow": "",
            "workflow_reason": "",
            "repo_summary": {},
            "tasks": [],
            "task_status": {},
            "task_attempts": {},
            "worker_results": [],
            "orchestrator_round": 0,
            "orchestrator_degraded": False,
            "orchestrator_summary": "",
            "orchestrator_completed": 0,
            "orchestrator_failed": 0,
            "worker_context": "",
            "orchestration_handoff": {},
            "task_extensions_seen": 0,
            "task_extensions_applied": [],
            "task_extension_errors": [],
            # 每次 run 复位 steering；指令队列本身也清空（上一轮的迟到命令
            # 不该影响新一轮）。
            "steering_cancelled": False,
            "steering_replan": False,
            "steering_replans": 0,
            "steering_applied": [],
            "workflow_revised_reason": "",
        }
        async for event in self._stream(
            inputs, thread_id=thread_id, fresh_run=True, prompt=prompt
        ):
            yield event

    def _resume_limit_error(self, thread_id: str) -> str:
        """恢复次数护栏；返回空串表示还可以恢复。

        `recursion_limit` 每次图调用都会重置，所以「挂起-恢复」必须在编排层
        封顶，否则前端可以无限续命。
        """
        count = self._resume_counts.get(thread_id, 0) + 1
        self._resume_counts[thread_id] = count
        if count <= self.settings.max_resumes:
            return ""
        return (
            f"会话 {thread_id} 的中断/恢复次数已超过上限 "
            f"{self.settings.max_resumes}，已停止。"
        )

    def _fail_resume(self, thread_id: str, detail: str) -> None:
        try:
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.RUN_ERROR,
                    thread_id=thread_id,
                    detail=detail,
                )
            )
        except Exception as exc:  # noqa: BLE001 - 已在错误路径上，不再二次抛出
            _swallow_audit("audit_run_error", exc)
        self._forget_thread(thread_id)

    async def resume(self, thread_id: str, decisions: dict[str, bool]) -> AsyncIterator[Event]:
        """带着审批结果继续被挂起的图（持有工作区单写者锁）。"""
        if self._stream_active:
            yield RunFailed(message=_STREAM_BUSY_MESSAGE)
            return
        if not self._acquire_workspace_lock():
            yield RunFailed(message=self._workspace_lock_busy)
            return
        # 持有单写者锁时清理旧快照，避免多个 runtime 并发删除。
        self.prune_snapshots()
        self._stream_active = True
        try:
            async for event in self._resume_unlocked(thread_id, decisions):
                yield event
        finally:
            self._stream_active = False
            self._release_workspace_lock()

    async def _resume_unlocked(
        self, thread_id: str, decisions: dict[str, bool]
    ) -> AsyncIterator[Event]:
        """带着审批结果继续被挂起的图。

        decisions: call_id -> 是否批准。缺失的按拒绝处理（fail closed）。
        """
        detail = self._resume_limit_error(thread_id)
        if detail:
            self._fail_resume(thread_id, detail)
            yield RunFailed(message=detail)
            return

        command = Command(resume=dict(decisions))
        async for event in self._stream(command, thread_id=thread_id, fresh_run=False):
            yield event

    def steer(
        self,
        thread_id: str,
        action: str,
        *,
        text: str = "",
        role: str = "explore",
        source: str = "user",
    ) -> None:
        """提交一条运行中干预指令（TUI 的 /pause /cancel /add /replan）。

        指令在**节点边界**消费；pause/resume 通过 checkpoint 挂起与恢复。
        审计在**提交时**落账（来源/时间/动作），即使进程随后重启也能追溯。
        """
        command = SteeringCommand(
            action=action,
            text=text,
            role=role,
            source=source,
            ts=now_iso(),
        )
        detail = action if not text else f"{action}: {text}"
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.STEERING,
                thread_id=thread_id,
                detail=truncate(detail),
                source=source,
            )
        )
        self.steering.submit(thread_id, command)
        if action == "cancel":
            # 同步工具（wsl.exe / 本机 bash）不会检查 steering 标志；
            # 直接杀掉在飞进程，让工具线程尽快返回。
            cancel_active = getattr(self._sandbox, "cancel_active", None)
            if callable(cancel_active):
                try:
                    cancel_active()
                except Exception as exc:  # noqa: BLE001 - 取消失败不阻断命令入队
                    _swallow_audit("cancel_active", exc)

    def is_paused(self, thread_id: str) -> bool:
        """图是否停在 steering 的 pause 上。

        暂停标记会落盘，因此进程重启后（例如 CLI `agent resume`）也能读到。
        """
        return self.steering.is_paused(thread_id)

    async def pending_approvals(
        self, thread_id: str
    ) -> list[ApprovalRequested]:
        """从 checkpoint 读取挂起的审批请求，供跨进程 resume 使用。"""
        try:
            graph = await self._ensure_graph()
            snapshot = await graph.aget_state(
                {"configurable": {"thread_id": thread_id}}
            )
        except Exception:  # noqa: BLE001 - 读取失败按“无可恢复状态”处理
            return []
        suspensions: list[Any] = []
        for task in getattr(snapshot, "tasks", ()) or ():
            suspensions.extend(getattr(task, "interrupts", ()) or ())
        return self._interrupt_requests(suspensions)

    async def resume_steering(self, thread_id: str) -> AsyncIterator[Event]:
        """从 steering pause 恢复（持有工作区单写者锁）。"""
        if self._stream_active:
            yield RunFailed(message=_STREAM_BUSY_MESSAGE)
            return
        if not self._acquire_workspace_lock():
            yield RunFailed(message=self._workspace_lock_busy)
            return
        # 持有单写者锁时清理旧快照，避免多个 runtime 并发删除。
        self.prune_snapshots()
        self._stream_active = True
        try:
            async for event in self._resume_steering_unlocked(thread_id):
                yield event
        finally:
            self._stream_active = False
            self._release_workspace_lock()

    async def _resume_steering_unlocked(
        self, thread_id: str
    ) -> AsyncIterator[Event]:
        """从 steering pause 恢复。审批恢复请走 `resume()`。"""
        detail = self._resume_limit_error(thread_id)
        if detail:
            self._fail_resume(thread_id, detail)
            yield RunFailed(message=detail)
            return

        yield SteeringApplied(action="resume", detail="从暂停处继续")
        command = Command(resume={"steering": "resume"})
        async for event in self._stream(command, thread_id=thread_id, fresh_run=False):
            yield event

    # ------------------------------------------------------------------
    # 事件翻译：图更新 → 领域事件
    # ------------------------------------------------------------------

    _NODE_HANDLERS: dict[str, str] = {
        "planner": "_on_planner",
        "select_workflow": "_on_select_workflow",
        "plan_tasks": "_on_plan_tasks",
        "scheduler": "_on_scheduler",
        "synthesize": "_on_synthesize",
        "advance": "_on_advance",
        "replan": "_on_replan",
        "act": "_on_act",
        "tools": "_on_tools",
        "verify": "_on_verify",
        "review": "_on_review",
        "nudge": "_on_nudge",
        "repair": "_on_repair",
        "respond": "_on_respond",
    }

    def _open_step(
        self, ctx: _StreamCtx, index: int, total: int, text: str
    ) -> Iterator[Event]:
        """发 StepStarted。上一步若还开着，先补一个收口再开新的。"""
        if ctx.step_open:
            ctx.step_open = False
            yield StepFinished(index=ctx.step_idx, cancelled=True, text="")
        ctx.step_open = True
        ctx.progress["step_open"] = True
        yield StepStarted(index=index, total=total, text=text)

    def _close_step(
        self, ctx: _StreamCtx, *, cancelled: bool = False
    ) -> Iterator[Event]:
        """收口当前步骤。已经关了就是空操作，不会多发一个事件。"""
        if not ctx.step_open:
            return
        ctx.step_open = False
        ctx.progress["step_open"] = False
        yield StepFinished(index=ctx.step_idx, cancelled=cancelled, text="")

    def _update_events(
        self, ctx: _StreamCtx, node: str, update: dict[str, Any]
    ) -> Iterator[Event]:
        """把单个节点的 update 翻成事件；未知节点原样忽略。"""
        retries = int(update.get("transient_retries") or 0)
        if retries:
            ctx.progress["transient_retries"] = (
                int(ctx.progress.get("transient_retries", 0)) + retries
            )

        trim = update.get("context_trim")
        if isinstance(trim, dict):
            yield ContextTrimmed(
                node=str(trim.get("node") or node),
                trimmed=int(trim.get("trimmed", 0) or 0),
                before_chars=int(trim.get("before_chars", 0) or 0),
                after_chars=int(trim.get("after_chars", 0) or 0),
                before_tokens=int(trim.get("before_tokens", 0) or 0),
                after_tokens=int(trim.get("after_tokens", 0) or 0),
                escalated=bool(trim.get("escalated")),
            )

        # Phase 3D：四个 steering 消费点共用同一套事件载荷。
        if node.startswith("steering"):
            yield from self._on_steering(ctx, update)
            return

        handler = self._NODE_HANDLERS.get(node)
        if handler is not None:
            events = getattr(self, handler)(ctx, update)
            if events is not None:
                yield from events

    def _on_steering(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        for applied in update.get("steering_applied") or []:
            yield SteeringApplied(
                action=str(applied.get("action", "")),
                task_id=str(applied.get("task_id", "")),
                detail=str(applied.get("detail", "")),
            )
        if update.get("steering_cancelled"):
            yield from self._close_step(ctx, cancelled=True)

    def _on_planner(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        ctx.plan = [s for s in (update.get("plan") or []) if s]
        ctx.plan_kinds = [str(k) for k in (update.get("plan_kinds") or [])]
        ctx.step_idx = 0
        ctx.progress["plan"] = ctx.plan
        ctx.progress["plan_kinds"] = ctx.plan_kinds
        ctx.progress["step_idx"] = 0
        if ctx.plan:
            # 降级必须同时进事件与审计：只在事件里说的话，
            # 事后复盘仍然查不到「这次规划其实是失败的」。
            degraded = bool(update.get("plan_degraded"))
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.PLAN,
                    thread_id=ctx.thread_id,
                    steps=ctx.plan,
                    kinds=ctx.plan_kinds or None,
                    detail=_PLAN_DEGRADED_DETAIL if degraded else "",
                )
            )
            yield PlanCreated(
                steps=ctx.plan, kinds=ctx.plan_kinds, degraded=degraded
            )
        yield from self._open_step(
            ctx, 0, max(len(ctx.plan), 1), ctx.plan[0] if ctx.plan else ""
        )

    def _on_select_workflow(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        if "workflow" not in update:
            return
        workflow = str(update.get("workflow", ""))
        reason = str(update.get("workflow_reason", ""))
        ctx.progress["workflow"] = workflow
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.WORKFLOW,
                thread_id=ctx.thread_id,
                workflow=workflow,
                detail=truncate(reason),
            )
        )
        yield WorkflowSelected(workflow=workflow, reason=reason)

    def _on_plan_tasks(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        tasks = update.get("tasks") or []
        if "tasks" not in update:
            return
        degraded = bool(update.get("orchestrator_degraded"))
        degradation_note = str(update.get("orchestrator_summary", "")).strip()
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.TASK_GRAPH,
                thread_id=ctx.thread_id,
                workflow=ctx.progress.get("workflow"),
                steps=[str(task.get("goal", "")) for task in tasks],
                detail=(
                    "任务图已降级"
                    + (f"：{degradation_note}" if degradation_note else "")
                    if degraded
                    else ""
                ),
            )
        )
        yield TaskGraphCreated(tasks=tasks, degraded=degraded)
        revised_reason = str(update.get("workflow_revised_reason") or "")
        if revised_reason:
            yield WorkflowRevised(reason=revised_reason, tasks=tasks)

    def _on_scheduler(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        results = update.get("worker_results") or []
        emitted = int(ctx.progress.get("workers_emitted", 0))
        fresh = results[emitted:]
        # custom 流已经实时发过的事件不再重复；直接调用节点
        # （单测）没有 custom writer 时，这里负责补齐。
        started_seen = ctx.progress.setdefault("worker_started_keys", set())
        finished_seen = ctx.progress.setdefault("worker_finished_keys", set())
        for result in fresh:
            key = _worker_key(result)
            if key in started_seen:
                continue
            started_seen.add(key)
            yield WorkerStarted(
                worker_id=str(result.get("worker_id", "")),
                task_id=str(result.get("task_id", "")),
                role=str(result.get("role", "")),
                goal=str(result.get("goal", "")),
                attempt=int(result.get("attempt", 1)),
                queue_wait_ms=int(result.get("queue_wait_ms", 0)),
            )
        for result in fresh:
            key = _worker_key(result)
            if key in finished_seen:
                continue
            finished_seen.add(key)
            yield from self._worker_events(result, ctx.thread_id, ctx.progress)
        ctx.progress["workers_emitted"] = len(results)
        # 事件驱动 scheduler 在同一个节点里完成动态扩展，
        # 没有独立的 extend_tasks 更新可翻译。
        added = update.get("task_extensions_applied") or []
        rejected = update.get("task_extension_errors") or []
        if added or rejected:
            if added:
                self._audit(
                    AuditRecord(
                        ts=now_iso(),
                        kind=audit_models.TASK_GRAPH,
                        thread_id=ctx.thread_id,
                        workflow=ctx.progress.get("workflow"),
                        steps=[str(task.get("goal", "")) for task in added],
                        detail=(
                            f"运行中扩展 {len(added)} 个只读任务"
                            + (f"；拒绝 {len(rejected)} 个" if rejected else "")
                        ),
                    )
                )
            yield TaskGraphExtended(added=added, rejected=rejected)

    def _on_synthesize(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        completed = int(update.get("orchestrator_completed", 0))
        failed = int(update.get("orchestrator_failed", 0))
        summary = str(update.get("orchestrator_summary", ""))
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.ORCHESTRATION,
                thread_id=ctx.thread_id,
                workflow=ctx.progress.get("workflow"),
                ok=failed == 0,
                detail=truncate(summary),
            )
        )
        yield OrchestrationFinished(
            completed=completed, failed=failed, summary=summary
        )

    def _on_advance(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        # 先收口再推进：补发的 StepFinished 必须带**旧**的 index，
        # 而 step_idx 下一行就要被改掉
        yield from self._close_step(ctx)
        ctx.step_idx = int(update.get("step_idx", ctx.step_idx + 1))
        ctx.progress["step_idx"] = ctx.step_idx
        yield from self._open_step(
            ctx,
            ctx.step_idx,
            len(ctx.plan),
            ctx.plan[ctx.step_idx] if ctx.step_idx < len(ctx.plan) else "",
        )

    def _on_replan(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        # 节点不修订时返回空更新，只有真改了才发事件
        if "plan" not in update:
            return
        revised = [s for s in (update.get("plan") or []) if s]
        plan_kinds = [str(k) for k in (update.get("plan_kinds") or [])]
        detail = (
            f"剩余 {max(len(ctx.plan) - ctx.step_idx, 0)} 步"
            f" → {max(len(revised) - ctx.step_idx, 0)} 步"
        )
        ctx.plan = revised
        ctx.progress["plan"] = ctx.plan
        ctx.progress["plan_kinds"] = plan_kinds
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.REPLAN,
                thread_id=ctx.thread_id,
                steps=ctx.plan,
                kinds=plan_kinds or None,
                detail=detail,
            )
        )
        yield PlanRevised(
            steps=ctx.plan, kinds=plan_kinds, step_idx=ctx.step_idx
        )
        # 计划被砍到当前这步之前（含当前这步）时，路由会直接去收尾，
        # act 再也不跑 —— 这一步必须在这里收口，否则前端的进度条
        # 永远停在「进行中」
        if ctx.step_idx >= len(ctx.plan):
            yield from self._close_step(ctx, cancelled=True)

    def _on_act(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        last = _last_message(update)
        ctx.input_tokens, ctx.output_tokens = _add_usage(
            last, ctx.input_tokens, ctx.output_tokens
        )
        ctx.progress["input_tokens"] = ctx.input_tokens
        ctx.progress["output_tokens"] = ctx.output_tokens
        calls = getattr(last, "tool_calls", None) or []
        for call in calls:
            started = self._tool_started(
                str(call.get("name", "")),
                dict(call.get("args") or {}),
                str(call.get("id", "")),
            )
            ctx.pending[started.call_id] = started
            yield started
            # 与 ToolCallStarted 并列：delegate 本身也是一次工具调用，
            # 但前端需要知道「这是一次委派、目标是什么」。
            if started.name == DELEGATE_EXPLORE:
                yield SubAgentStarted(
                    agent_id=started.call_id,
                    parent_call_id=started.call_id,
                    role="explore",
                    goal=str(dict(call.get("args") or {}).get("goal", "")),
                )
        if not calls:
            ctx.step_open = False
            ctx.progress["step_open"] = False
            yield StepFinished(
                index=ctx.step_idx,
                budget_exhausted=bool(update.get("budget_exhausted")),
                text=text_of(last),
            )

    def _on_tools(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        for message in update.get("messages") or []:
            finished = self._tool_finished(message)
            started = ctx.pending.pop(finished.call_id, None)
            self._audit_tool_call(started, finished, ctx.thread_id)
            yield finished

            if started is not None and started.name == DELEGATE_EXPLORE:
                artifact = parse_artifact(getattr(message, "artifact", None))
                if isinstance(artifact, SubAgentArtifact):
                    # 子智能体的 token 也算进本轮总量：不记的话，
                    # 开启委派后成本会系统性偏低。
                    ctx.input_tokens += artifact.input_tokens
                    ctx.output_tokens += artifact.output_tokens
                    ctx.progress["input_tokens"] = ctx.input_tokens
                    ctx.progress["output_tokens"] = ctx.output_tokens
                    self._audit_subagent(artifact, ctx.thread_id)
                    yield self._subagent_finished(artifact)
                else:
                    # 被拒 / 预算耗尽 / 子智能体异常：没有结构化产物，
                    # 但 started 已经发过，必须配对收口。
                    # `rejected` 与 `failed` 必须分开：前者是宿主
                    # 没让它跑（预算/审批），后者是跑了但没成。
                    yield SubAgentFinished(
                        agent_id=started.call_id,
                        status=(
                            "rejected" if finished.rejected else "failed"
                        ),
                        summary=_preview(
                            str(getattr(message, "content", ""))
                        ),
                    )

            for changed in self._file_changes(message):
                self._audit_file_change(changed, ctx.thread_id)
                yield changed

    def _on_verify(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        raw = update.get("verification") or {}
        # 新的验证结果让上一次的审查结论作废：审查总是紧跟在验证
        # 之后，所以"当前卡在哪一关"只需要看最新的一对。
        ctx.last_review = {}
        ctx.progress["last_review"] = {}
        if raw:
            ctx.last_verification = raw
            ctx.progress["last_verification"] = raw
            event = self._verification(raw)
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.VERIFY,
                    thread_id=ctx.thread_id,
                    # 没做验证时记 None（未验证），不记 True（通过）
                    ok=_verification_verdict(raw),
                    detail=truncate(
                        f"[{raw.get('kind', 'none')}] "
                        f"{raw.get('command', '')} → {raw.get('summary', '')}"
                    ),
                )
            )
            yield event

    def _on_review(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        raw = update.get("review") or {}
        if raw:
            ctx.last_review = raw
            ctx.progress["last_review"] = raw
            blocked = _review_blocked(raw)
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.REVIEW,
                    thread_id=ctx.thread_id,
                    ok=not blocked,
                    detail=truncate(
                        f"{raw.get('checked_files', 0)} 个改动文件 → "
                        f"{raw.get('summary', '')}"
                    ),
                )
            )
            yield self._review(raw)

    def _on_nudge(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> None:
        # 一步没产生任何改动被打回重做。**刻意不发事件**：
        # 它不是给用户看的结果，而是"这一步还没做完"的内部重试 ——
        # 前端时间线上会看到这一步继续在动。但审计必须留痕：
        # 事后要能回答"这次运行里有几步是被打回来的"。
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.NUDGE,
                thread_id=ctx.thread_id,
                detail=truncate(
                    f"第 {ctx.step_idx + 1} 步未产生改动，已要求重做"
                ),
            )
        )

    def _on_repair(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> Iterator[Event]:
        attempt = int(update.get("retry", 0))
        limit = self.settings.max_repair_rounds
        # 摘要按**阻断来源**取：修的是谁就说什么。一味看
        # last_verification，在"审查阻断"时会带出验证的 ok 摘要。
        if _review_blocked(ctx.last_review):
            summary = str(ctx.last_review.get("summary", ""))
            issues = self._review(ctx.last_review).findings
        else:
            summary = str(ctx.last_verification.get("summary", ""))
            issues = self._verification(ctx.last_verification).issues
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.REPAIR,
                thread_id=ctx.thread_id,
                detail=truncate(f"第 {attempt}/{limit} 次修复（{summary}）"),
            )
        )
        yield RepairStarted(
            attempt=attempt,
            limit=limit,
            summary=summary,
            issues=issues,
        )

    def _on_respond(
        self, ctx: _StreamCtx, update: dict[str, Any]
    ) -> None:
        last = _last_message(update)
        ctx.input_tokens, ctx.output_tokens = _add_usage(
            last, ctx.input_tokens, ctx.output_tokens
        )
        ctx.progress["input_tokens"] = ctx.input_tokens
        ctx.progress["output_tokens"] = ctx.output_tokens
        ctx.answer = text_of(last)

    def _custom_events(
        self, ctx: _StreamCtx, data: Any
    ) -> Iterator[Event]:
        # Phase 4：dispatch 通过 get_stream_writer 实时推送 worker
        # 开始/结束；updates 分支只补齐没被流式发过的事件。
        payload_data = data if isinstance(data, dict) else {}
        kind = str(payload_data.get("type", ""))
        if kind == "worker_started":
            key = _worker_key(payload_data)
            seen = ctx.progress.setdefault("worker_started_keys", set())
            if key not in seen:
                seen.add(key)
                yield WorkerStarted(
                    worker_id=str(payload_data.get("worker_id", "")),
                    task_id=str(payload_data.get("task_id", "")),
                    role=str(payload_data.get("role", "")),
                    goal=str(payload_data.get("goal", "")),
                    attempt=int(payload_data.get("attempt", 1)),
                    queue_wait_ms=int(payload_data.get("queue_wait_ms") or 0),
                )
        elif kind == "worker_finished":
            result = payload_data.get("result") or {}
            key = _worker_key(result)
            seen = ctx.progress.setdefault("worker_finished_keys", set())
            if key not in seen:
                seen.add(key)
                yield from self._worker_events(
                    result, ctx.thread_id, ctx.progress
                )

    def _message_events(
        self, ctx: _StreamCtx, data: Any
    ) -> Iterator[Event]:
        chunk, meta = data
        node = meta.get("langgraph_node", "")
        if node not in STREAMING_NODES:
            return
        reasoning = reasoning_of(chunk)
        if reasoning:
            yield ReasoningToken(node=node, text=reasoning)
        text = text_of(chunk)
        if not text:
            return
        if node == "respond":
            ctx.streamed.append(text)
        yield AssistantToken(node=node, text=text)

    def _interrupt_events(
        self, suspension: Any, seen: set[str]
    ) -> Iterator[Event]:
        for item in suspension:
            key = str(getattr(item, "id", "") or "")
            if key:
                if key in seen:
                    continue
                seen.add(key)
            payload = getattr(item, "value", None) or {}
            if payload.get("steering") == "pause":
                yield SteeringApplied(
                    action="pause",
                    detail=str(payload.get("message", "")),
                )
            else:
                yield from self._interrupt_requests([item])

    async def _cost_summary(
        self, thread_id: str, run_started_at: float
    ) -> _CostSummary:
        settings = self.settings
        usage = GLOBAL_LIMITER.run_usage(thread_id)
        budget = float(settings.run_cost_budget_usd or 0.0)
        ratio = (usage["cost_usd"] / budget) if budget > 0 else 0.0
        alerts: list[str] = []
        if budget > 0:
            if ratio >= 1.0:
                alerts.append("已超出本次运行成本预算")
            elif ratio >= float(settings.cost_alert_ratio):
                alerts.append("接近本次运行成本预算")

        billed_usd = 0.0
        billing_error = ""
        billing_providers: dict[str, dict[str, float]] = {}
        if settings.billing_enabled:
            started = float(usage.get("started_at") or run_started_at)
            try:
                billing = await asyncio.to_thread(
                    fetch_billing, settings, started, time.time()
                )
                for item in billing:
                    billed_usd += float(item.cost_usd)
                    billing_providers[item.provider] = {
                        "cost_usd": float(item.cost_usd),
                        "input_tokens": float(item.input_tokens),
                        "output_tokens": float(item.output_tokens),
                    }
                    if item.error:
                        billing_error = (
                            f"{item.provider}: {item.error}"
                            if not billing_error
                            else f"{billing_error}; {item.provider}: {item.error}"
                        )
            except Exception as exc:  # noqa: BLE001 - 对账失败不拖垮 run
                billing_error = f"{type(exc).__name__}: {exc}"
        billing_delta = billed_usd - float(usage["cost_usd"])
        if billed_usd > 0 and billing_delta > max(
            0.01, float(usage["cost_usd"]) * 0.2
        ):
            alerts.append("provider 账单金额明显高于本地估算")
        return _CostSummary(
            usage=usage,
            budget=budget,
            ratio=ratio,
            alerts=alerts,
            billed_usd=billed_usd,
            billing_delta=billing_delta,
            billing_error=billing_error,
            billing_providers=billing_providers,
        )

    async def _finish_events(
        self, ctx: _StreamCtx, run_started_at: float
    ) -> AsyncIterator[Event]:
        final = ctx.answer or "".join(ctx.streamed)
        summary = await self._cost_summary(ctx.thread_id, run_started_at)
        usage = summary.usage
        retry_count = int(ctx.progress.get("transient_retries", 0))
        run_detail = final
        if retry_count:
            run_detail += f"\n（本次运行因瞬时错误重试 {retry_count} 次）"
        concurrency = self.concurrency.snapshot()
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.CONCURRENCY,
                thread_id=ctx.thread_id,
                detail=truncate(json.dumps(concurrency, ensure_ascii=False)),
            )
        )
        worker_in = (
            int(ctx.progress.get("worker_input_tokens", 0))
            if ctx.progress.get("worker_tokens_reported")
            else None
        )
        worker_out = (
            int(ctx.progress.get("worker_output_tokens", 0))
            if ctx.progress.get("worker_tokens_reported")
            else None
        )
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.RUN_END,
                thread_id=ctx.thread_id,
                run_epoch=ctx.run_epoch,
                provider=self.provider_name,
                model=self.model_override,
                detail=truncate(run_detail),
                # provider 未回报用量时保持 None（exclude_none 会省略该字段）
                input_tokens=ctx.input_tokens or None,
                output_tokens=ctx.output_tokens or None,
                worker_input_tokens=worker_in,
                worker_output_tokens=worker_out,
                cost_usd=usage["cost_usd"] or None,
                billed_usd=summary.billed_usd or None,
                cache_hit_tokens=int(usage.get("cache_hit_tokens", 0)) or None,
                cache_miss_tokens=int(usage.get("cache_miss_tokens", 0)) or None,
            )
        )
        # 收尾兜底：正常路径下 act 已经收口（空操作），但若某条路径让 act 没能
        # 跑（计划被砍、异常恢复等），这里补上，保证 StepStarted 一定有配对
        for event in self._close_step(ctx, cancelled=True):
            yield event
        yield CostReport(
            run_id=ctx.thread_id,
            input_tokens=int(usage["input_tokens"]),
            output_tokens=int(usage["output_tokens"]),
            cost_usd=float(usage["cost_usd"]),
            budget_usd=summary.budget,
            ratio=summary.ratio,
            billed_usd=summary.billed_usd,
            billing_delta_usd=summary.billing_delta,
            billing_error=summary.billing_error,
            alerts=summary.alerts,
            cache_hit_tokens=int(usage.get("cache_hit_tokens", 0)),
            cache_miss_tokens=int(usage.get("cache_miss_tokens", 0)),
            billing_providers=summary.billing_providers,
            providers={
                str(key): {
                    "input_tokens": float(value.get("input_tokens", 0) or 0),
                    "output_tokens": float(value.get("output_tokens", 0) or 0),
                    "cost_usd": float(value.get("cost_usd", 0) or 0),
                    "cache_hit_tokens": float(
                        value.get("cache_hit_tokens", 0) or 0
                    ),
                    "cache_miss_tokens": float(
                        value.get("cache_miss_tokens", 0) or 0
                    ),
                }
                for key, value in (usage.get("providers") or {}).items()
            },
        )
        await self._prune_checkpoints(ctx.thread_id)
        self._forget_thread(ctx.thread_id)
        yield RunFinished(
            thread_id=ctx.thread_id,
            answer=final,
            input_tokens=ctx.input_tokens or None,
            output_tokens=ctx.output_tokens or None,
            worker_input_tokens=worker_in,
            worker_output_tokens=worker_out,
            concurrency=concurrency,
        )

    async def _error_events(
        self,
        ctx: _StreamCtx,
        exc: Exception,
        *,
        deadline_at: float,
        deadline_seconds: int,
    ) -> AsyncIterator[Event]:
        deadline_hit = (
            isinstance(exc, TimeoutError)
            and deadline_at > 0
            and time.monotonic() >= deadline_at - 1.0
        )
        detail = (
            f"运行超过总时限 {deadline_seconds}s，已停止。"
            "已完成的状态在 checkpoint 里，可用同一 thread 继续。"
            if deadline_hit
            else f"{type(exc).__name__}: {exc}"
        )
        # 尽力记一笔，但**不能让它顶掉真正的错误**：审计本身坏了的时候
        # （比如磁盘满、目录被占），这里再抛一次就把原因盖掉了。
        try:
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.RUN_ERROR,
                    thread_id=ctx.thread_id,
                    run_epoch=ctx.run_epoch,
                    provider=self.provider_name,
                    model=self.model_override,
                    detail=detail,
                    input_tokens=ctx.input_tokens or None,
                    output_tokens=ctx.output_tokens or None,
                    worker_input_tokens=(
                        int(ctx.progress.get("worker_input_tokens", 0))
                        if ctx.progress.get("worker_tokens_reported")
                        else None
                    ),
                    worker_output_tokens=(
                        int(ctx.progress.get("worker_output_tokens", 0))
                        if ctx.progress.get("worker_tokens_reported")
                        else None
                    ),
                )
            )
        except Exception as exc:  # noqa: BLE001 - 已在错误路径上，不再二次抛出
            _swallow_audit("audit_error_path", exc)
        # 异常可能发生在任何节点上：此刻还开着的步骤再也不会被 act 收口，
        # 补发一个，免得前端把它显示成「仍在运行」
        for event in self._close_step(ctx, cancelled=True):
            yield event
        worker_in = (
            int(ctx.progress.get("worker_input_tokens", 0))
            if ctx.progress.get("worker_tokens_reported")
            else None
        )
        worker_out = (
            int(ctx.progress.get("worker_output_tokens", 0))
            if ctx.progress.get("worker_tokens_reported")
            else None
        )
        await self._prune_checkpoints(ctx.thread_id)
        self._forget_thread(ctx.thread_id)
        yield RunFailed(
            message=detail,
            input_tokens=ctx.input_tokens or None,
            output_tokens=ctx.output_tokens or None,
            worker_input_tokens=worker_in,
            worker_output_tokens=worker_out,
        )

    async def _stream(
        self, payload: Any, *, thread_id: str, fresh_run: bool, prompt: str = ""
    ) -> AsyncIterator[Event]:
        settings = self.settings
        run_started_at = time.time()
        # 同一 thread 会被多轮 run 复用：全新 run 必须清零，resume 必须保留
        # 挂起前那一段的用量。
        GLOBAL_LIMITER.start_run(thread_id, reset=fresh_run)
        config = {
            "configurable": {"thread_id": thread_id},
            # 按图拓扑推导，别再把系数算歪（见 estimate_recursion_limit）
            "recursion_limit": estimate_recursion_limit(
                settings.max_plan_steps,
                settings.max_tool_rounds,
                settings.max_repair_rounds,
                settings.max_replans,
                orchestrator_enabled=settings.orchestrator_enabled,
                orchestrator_max_tasks=settings.orchestrator_max_tasks,
                orchestrator_checkpoint_every=(
                    settings.orchestrator_checkpoint_every
                ),
            ),
            # 这些会随 trace 一起上报，LangSmith 里可按会话/工作区/权限筛选
            "run_name": f"agent:{thread_id}",
            "tags": ["coding-agent", f"mode:{self.policy.approval_mode}"],
            "metadata": self._trace_metadata(thread_id),
        }

        # 跨 run/resume 的状态；每次全新 run 清空，resume 时保留（见 __init__ 说明）
        pending = self._pending_calls.setdefault(thread_id, {})
        progress = self._progress.setdefault(thread_id, {})
        if fresh_run:
            pending.clear()
            progress.clear()
            self._resume_counts[thread_id] = 0
        else:
            # 进程重启后 `_pending_calls` 是空的：从 checkpoint 的尾部
            # tool_calls 恢复调用参数，否则恢复后的审计会丢掉 L2/L3 的参数。
            # 同进程 resume 时 pending 已有记录，这里不会重复补发。
            prior = await self._prior_state(thread_id)
            for call in unanswered_tool_calls(prior.get("messages") or []):
                call_id = str(call.get("id", ""))
                if not call_id or call_id in pending:
                    continue
                started = self._tool_started(
                    str(call.get("name", "")),
                    dict(call.get("args") or {}),
                    call_id,
                )
                pending[call_id] = started
                yield started
        # 模型回报的用量、计划、验证/审查结果与 step 配对状态都存 progress：
        # 挂起与恢复是两次 `_stream`，存局部会把挂起前的那一段整个丢掉。
        # provider 未提供用量时保持 0，收尾落成 None（0 与「未回报」必须能区分）。
        if fresh_run:
            # 计量幂等键：resume 复用同一 epoch，新的用户消息生成新 epoch。
            progress["run_epoch"] = uuid4().hex
        ctx = _StreamCtx(
            runtime=self,
            thread_id=thread_id,
            progress=progress,
            pending=pending,
            streamed=[],
            plan=progress.setdefault("plan", []),
            plan_kinds=progress.setdefault("plan_kinds", []),
            step_idx=int(progress.get("step_idx", 0)),
            input_tokens=int(progress.get("input_tokens", 0)),
            output_tokens=int(progress.get("output_tokens", 0)),
            run_epoch=str(progress.get("run_epoch") or ""),
            last_verification=progress.setdefault("last_verification", {}),
            last_review=progress.setdefault("last_review", {}),
            step_open=bool(progress.get("step_open", False)),
        )
        interrupted = False

        # 总墙钟时限：跨 resume 累计，不能用“每次 _stream 重新计时”。
        deadline_seconds = int(settings.run_deadline_s or 0)
        if fresh_run:
            progress["deadline_at"] = (
                time.monotonic() + deadline_seconds
                if deadline_seconds > 0
                else 0.0
            )
        deadline_at = float(progress.get("deadline_at") or 0.0)

        def _remaining_seconds() -> float | None:
            if deadline_at <= 0:
                return None
            return max(0.0, deadline_at - time.monotonic())

        async def _timed_chunks(graph: Any):
            """把图的 astream 包进总时限；超时抛 TimeoutError。"""
            async with asyncio.timeout(_remaining_seconds()):
                async for item in graph.astream(
                    payload,
                    config,
                    stream_mode=["messages", "updates", "custom"],
                    # 独立子图的内层节点更新/自定义事件只有打开它才可见。
                    subgraphs=True,
                ):
                    yield item

        try:
            if fresh_run:
                self._audit(
                    AuditRecord(
                        ts=now_iso(),
                        kind=audit_models.RUN_START,
                        thread_id=thread_id,
                        run_epoch=ctx.run_epoch,
                        provider=self.provider_name,
                        model=self.model_override,
                        workspace=self.workspace,
                        detail=truncate(prompt),
                    )
                )
                # 事件流起点。所有前端都能据此复位本轮状态（TUI 就靠它重置
                # 「最终答复」标记与流式缓冲）。只有全新 run 算新的一轮，
                # resume 不重复发 —— 那是同一次运行的延续。
                yield RunStarted(thread_id=thread_id)

            graph = await self._ensure_graph()
            # 只在一个 stream 调用内去重：子图中断会同时在带 namespace 的
            # 更新和父图更新里出现一次，但跨 resume 的重复暂停不能被吞掉。
            seen_interrupts: set[str] = set()
            async for chunk in _timed_chunks(graph):
                if len(chunk) == 3:
                    _namespace, mode, data = chunk
                else:
                    mode, data = chunk
                if mode == "custom":
                    for event in self._custom_events(ctx, data):
                        yield event
                    continue

                if mode == "messages":
                    for event in self._message_events(ctx, data):
                        yield event
                    continue

                suspension = data.get("__interrupt__")
                if suspension:
                    interrupted = True
                    for event in self._interrupt_events(
                        suspension, seen_interrupts
                    ):
                        yield event
                    continue

                for node, update in data.items():
                    if not isinstance(update, dict):
                        continue
                    for event in self._update_events(ctx, node, update):
                        yield event


        except Exception as exc:  # noqa: BLE001 - 编排层异常也要以事件形式报给前端
            async for event in self._error_events(
                ctx,
                exc,
                deadline_at=deadline_at,
                deadline_seconds=deadline_seconds,
            ):
                yield event
            return

        if interrupted:
            # 图已挂起：不发 RunFinished，让前端明确知道要 resume 而不是收工
            return

        async for event in self._finish_events(ctx, run_started_at):
            yield event

    # ------------------------------------------------------------------
    # 回滚（用户主动发起，不走审批；审批管的是模型发起的动作）
    # ------------------------------------------------------------------

    # 快照/回滚/差异/上传留底已移到 `runtime_snapshots.SnapshotMixin`，
    # 本类通过继承拿到它们（`class AgentRuntime(SnapshotMixin)`）。

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------

    def _audit(self, record: AuditRecord) -> None:
        """写一条审计记录。

        写失败会抛 AuditError 并被上层转成 RunFailed —— 审计有缺口是这个项目
        不能接受的失败模式，宁可让运行显式失败，也不静默丢记录。
        """
        if not record.workspace:
            # run_end / tool_call 等记录默认没带 workspace；补齐后多工作区
            # 的对账才不会靠猜。解析失败不阻断审计本身。
            try:
                record.workspace = self.workspace
            except Exception:  # noqa: BLE001
                pass
        self.audit.write(record)

    def _audit_tool_call(
        self,
        started: ToolCallStarted | None,
        finished: ToolCallFinished,
        thread_id: str,
    ) -> None:
        if finished.decision == DENIED:
            decision = audit_models.DECISION_DENIED
        elif finished.decision == APPROVED:
            decision = audit_models.DECISION_APPROVED
        elif finished.rejected:
            decision = audit_models.DECISION_REJECTED
        else:
            decision = audit_models.DECISION_AUTO
        # `skipped` / `interrupted` 都是「没得出成功或失败」：审计必须记 None，
        # 不能把“没验证/结果未知”混进成功或失败率。
        audit_ok = (
            None
            if finished.interrupted or finished.skipped
            else finished.ok
        )
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.TOOL_CALL,
                thread_id=thread_id,
                call_id=finished.call_id,
                tool=finished.name,
                args=sanitize_args(dict(started.args) if started else {}),
                level=finished.level or (started.level if started else "") or "",
                decision=decision,
                ok=audit_ok,
                exit_code=finished.exit_code,
                duration_ms=finished.duration_ms,
            )
        )

    def _audit_subagent(self, artifact: SubAgentArtifact, thread_id: str) -> None:
        """把一次子智能体运行记进审计。

        - 一条 `subagent` 汇总记录（目标、状态、摘要、token）；
        - 子智能体实际执行的每个只读工具一条 `tool_call` 记录。
          没有后者的话，主审计里只能看到一次 delegate，子智能体读了什么完全不可见。
        """
        verdict = (
            True
            if artifact.status == "success"
            else False if artifact.status == "failed" else None
        )
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.SUBAGENT,
                thread_id=thread_id,
                agent_id=artifact.agent_id,
                parent_call_id=artifact.parent_call_id or None,
                task=truncate(artifact.goal),
                ok=verdict,
                detail=truncate(artifact.summary),
                input_tokens=artifact.input_tokens or None,
                output_tokens=artifact.output_tokens or None,
                duration_ms=artifact.duration_ms,
            )
        )
        for call in artifact.tool_calls:
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.TOOL_CALL,
                    thread_id=thread_id,
                    call_id=str(call.get("call_id", "")),
                    tool=str(call.get("name", "")),
                    args=sanitize_args(dict(call.get("args") or {})),
                    level="L0 只读",
                    decision=audit_models.DECISION_AUTO,
                    ok=bool(call.get("ok")),
                    duration_ms=call.get("duration_ms"),
                    agent_id=artifact.agent_id,
                    parent_call_id=artifact.parent_call_id or None,
                )
            )

    def _audit_file_change(self, changed: FileChanged, thread_id: str) -> None:
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.FILE_CHANGE,
                thread_id=thread_id,
                path=changed.path,
                action=changed.action,
                added=changed.added,
                removed=changed.removed,
                snapshot_id=changed.snapshot_id,
            )
        )

    # ------------------------------------------------------------------
    # 单条更新 → 事件
    # ------------------------------------------------------------------

    @staticmethod
    def _interrupt_requests(suspension: Any) -> list[ApprovalRequested]:
        """把 approval_gate 挂起时抛出的 payload 翻译成审批事件。"""
        requests: list[ApprovalRequested] = []
        for item in suspension or ():
            payload = getattr(item, "value", None) or {}
            for raw in payload.get("requests", []) or []:
                requests.append(
                    ApprovalRequested(
                        request_id=str(raw.get("call_id", "")),
                        tool=str(raw.get("tool", "")),
                        command=str(raw.get("command", "")),
                        level=str(raw.get("level", "")),
                        level_int=int(raw.get("level_int", 0) or 0),
                        reason=str(raw.get("reason", "")),
                    )
                )
        return requests

    def _worker_events(
        self,
        result: dict[str, Any],
        thread_id: str,
        progress: dict[str, Any],
    ) -> Iterator[Event]:
        """一个 worker 结果 → WorkerFinished + 审计（custom/updates 共用）。

        只在结果首次被消费时调用；调用方用 `_worker_key` 去重。
        """
        status = str(result.get("status", ""))
        input_tokens = int(result.get("input_tokens") or 0)
        output_tokens = int(result.get("output_tokens") or 0)
        if input_tokens or output_tokens:
            progress["worker_input_tokens"] = (
                int(progress.get("worker_input_tokens", 0)) + input_tokens
            )
            progress["worker_output_tokens"] = (
                int(progress.get("worker_output_tokens", 0)) + output_tokens
            )
            progress["worker_tokens_reported"] = True

        yield WorkerFinished(
            worker_id=str(result.get("worker_id", "")),
            task_id=str(result.get("task_id", "")),
            role=str(result.get("role", "")),
            status=status,
            summary=str(result.get("summary", "")),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_ms=int(result.get("duration_ms") or 0),
            attempt=int(result.get("attempt", 1)),
            merged=bool(result.get("merged")),
            merge_error=str(result.get("merge_error", "")),
            tool_calls=len(result.get("tool_calls") or []),
            queue_wait_ms=int(result.get("queue_wait_ms") or 0),
            llm_ms=int(result.get("llm_ms") or 0),
            tool_ms=int(result.get("tool_ms") or 0),
            timed_out=bool(result.get("timed_out")),
            artifact_id=str(result.get("artifact_id", "")),
            depth=int(result.get("depth") or 0),
            priority=int(result.get("priority") or 0),
            scope_violations=[
                str(item) for item in (result.get("scope_violations") or [])
            ],
        )
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.WORKER,
                thread_id=thread_id,
                task_id=str(result.get("task_id", "")),
                role=str(result.get("role", "")),
                worker_id=str(result.get("worker_id", "")),
                attempt=int(result.get("attempt", 1)),
                merged=bool(result.get("merged")),
                artifact_id=str(result.get("artifact_id") or "") or None,
                timed_out=bool(result.get("timed_out")),
                queue_wait_ms=int(result.get("queue_wait_ms") or 0) or None,
                llm_ms=int(result.get("llm_ms") or 0) or None,
                tool_ms=int(result.get("tool_ms") or 0) or None,
                depth=int(result.get("depth") or 0) or None,
                priority=int(result.get("priority") or 0) or None,
                scope_violations=[
                    str(item)
                    for item in (result.get("scope_violations") or [])
                ],
                ok=(
                    True
                    if status == "success"
                    else False
                    if status == "failed"
                    else None
                ),
                detail=truncate(
                    str(result.get("summary", ""))
                    + (
                        f" | error={result.get('error')}"
                        if result.get("error")
                        else ""
                    )
                    + (
                        f" | merge_error={result.get('merge_error')}"
                        if result.get("merge_error")
                        else ""
                    )
                    + (
                        " | snapshots="
                        + ",".join(
                            str(item)
                            for item in (result.get("snapshot_ids") or [])
                        )
                        if result.get("snapshot_ids")
                        else ""
                    )
                ),
                input_tokens=input_tokens or None,
                output_tokens=output_tokens or None,
                duration_ms=int(result.get("duration_ms") or 0),
            )
        )
        # worker 内部的工具调用逐条落账；否则子智能体改了哪些文件只存在于
        # 摘要里，审计无法追溯。
        for call in result.get("tool_calls") or []:
            call_name = str(call.get("name", ""))
            call_args = dict(call.get("args") or {})
            level = tool_level(call_name, call_args)
            self._audit(
                AuditRecord(
                    ts=now_iso(),
                    kind=audit_models.TOOL_CALL,
                    thread_id=thread_id,
                    call_id=str(call.get("call_id", "")),
                    tool=call_name,
                    args=sanitize_args(call_args),
                    level=level.label,
                    decision=audit_models.DECISION_AUTO,
                    ok=bool(call.get("ok")),
                    duration_ms=call.get("duration_ms"),
                    agent_id=str(result.get("worker_id", "")) or None,
                    task_id=str(result.get("task_id", "")) or None,
                )
            )

    def _tool_started(self, name: str, args: dict[str, Any], call_id: str = "") -> ToolCallStarted:
        # 这里重新判一次等级纯粹是为了给前端着色；真正的放行/拒绝在工具内部，
        # 不存在"UI 判定通过就执行"的路径。
        level = None
        if name == SHELL_TOOL_NAME:
            level = classify(
                str(args.get("command", "")),
                read_deny_globs=self.settings.read_deny_globs,
            ).level.label
        return ToolCallStarted(
            call_id=call_id,
            name=name,
            args=args,
            summary=_summarize_args(args),
            level=level,
        )

    def _tool_finished(self, message: Any) -> ToolCallFinished:
        name = str(getattr(message, "name", "") or "")
        call_id = str(getattr(message, "tool_call_id", "") or "")
        content = str(getattr(message, "content", ""))
        preview = _preview(content)
        artifact = parse_artifact(getattr(message, "artifact", None))

        if isinstance(artifact, ShellArtifact):
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=artifact.ok,
                rejected=artifact.rejected,
                decision=artifact.decision,
                exit_code=artifact.exit_code,
                duration_ms=artifact.duration_ms,
                level=artifact.level_label or None,
                preview=preview,
            )

        if isinstance(artifact, FileArtifact):
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=artifact.ok,
                rejected=artifact.rejected,
                decision=artifact.decision,
                duration_ms=artifact.duration_ms,
                level="文件工具",
                preview=preview,
            )

        if isinstance(artifact, InterruptedArtifact):
            # 副作用执行到一半中断：既不能记成功，也不能断言失败。
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=False,
                interrupted=True,
                decision=artifact.decision,
                duration_ms=artifact.duration_ms,
                preview=preview,
            )

        if isinstance(artifact, CallArtifact):
            # 非 shell / 非文件的工具（git_commit、deps_install、run_tests…）。
            # 被拒时也走这里，level 用工具自身的等级，而不是「文件工具」。
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                ok=artifact.ok,
                rejected=artifact.rejected,
                skipped=artifact.skipped,
                decision=artifact.decision,
                duration_ms=artifact.duration_ms,
                level=artifact.level_label or None,
                preview=preview,
            )

        if isinstance(artifact, SubAgentArtifact):
            return ToolCallFinished(
                call_id=call_id,
                name=name,
                # 只有 success 才算真正完成；partial 仍会把摘要给模型，
                # 但审计与事件都不该把它记成「通过」。
                ok=artifact.status == "success",
                rejected=False,
                decision=artifact.decision,
                duration_ms=artifact.duration_ms,
                level="探索子智能体",
                preview=preview,
            )

        # 未提供 artifact 的工具：只能从文本粗判成败。新工具都应带 artifact。
        return ToolCallFinished(
            call_id=call_id,
            name=name,
            ok=not content.startswith(_FAILURE_MARKERS),
            preview=preview,
        )

    @staticmethod
    def _subagent_finished(artifact: SubAgentArtifact) -> SubAgentFinished:
        return SubAgentFinished(
            agent_id=artifact.agent_id,
            status=artifact.status,
            summary=artifact.summary,
            relevant_files=list(artifact.relevant_files),
            findings=list(artifact.findings),
            input_tokens=artifact.input_tokens,
            output_tokens=artifact.output_tokens,
            duration_ms=artifact.duration_ms,
        )

    @staticmethod
    def _review(raw: dict[str, Any]) -> ReviewFinished:
        findings = [
            " ".join(
                f"[{f.get('severity', '')}] {f.get('location', '')} {f.get('message', '')}".split()
            )
            for f in (raw.get("findings") or [])
        ]
        return ReviewFinished(
            status=str(raw.get("status", "skipped")),
            blocked=_review_blocked(raw),
            summary=str(raw.get("summary", "")),
            findings=findings,
        )

    @staticmethod
    def _verification(raw: dict[str, Any]) -> Verification:
        issues = [
            " ".join(f"{i.get('location', '')} {i.get('message', '')}".split())
            for i in (raw.get("issues") or [])
        ]
        return Verification(
            status=str(raw.get("status", "skipped")),
            command=str(raw.get("command", "")),
            # 与审计同源：都用三态口径，两边的结论必须一致
            ok=_verification_verdict(raw),
            summary=str(raw.get("summary", "")),
            issues=issues,
            failed_tests=[str(t) for t in (raw.get("failed_tests") or [])],
        )

    @staticmethod
    def _file_changed(message: Any) -> FileChanged | None:
        artifact = parse_artifact(getattr(message, "artifact", None))
        if not isinstance(artifact, FileArtifact):
            return None
        if not artifact.ok or artifact.action not in MUTATING_FILE_ACTIONS:
            return None
        return FileChanged(
            path=artifact.path,
            action=artifact.action,
            added=artifact.added,
            removed=artifact.removed,
            diff=artifact.diff,
            snapshot_id=artifact.snapshot_id,
        )

    def _file_changes(self, message: Any) -> list[FileChanged]:
        """一个工具结果涉及的所有文件改动。

        单文件工具返回 FileArtifact；`file_edit_many` 返回带 `file_changes`
        的 CallArtifact —— 否则批量编辑只有一条 tool_call，前端看不到逐文件
        的 ✎ 行，审计里也没有逐文件的 file_change。
        """
        artifact = parse_artifact(getattr(message, "artifact", None))
        if isinstance(artifact, FileArtifact):
            changed = self._file_changed(message)
            return [changed] if changed is not None else []
        if isinstance(artifact, CallArtifact) and artifact.ok:
            changes: list[FileChanged] = []
            for raw in artifact.file_changes:
                if not isinstance(raw, dict):
                    continue
                action = str(raw.get("action", ""))
                path = str(raw.get("path", ""))
                if not path or action not in MUTATING_FILE_ACTIONS:
                    continue
                changes.append(
                    FileChanged(
                        path=path,
                        action=action,
                        added=int(raw.get("added", 0) or 0),
                        removed=int(raw.get("removed", 0) or 0),
                        diff=str(raw.get("diff", "") or ""),
                        snapshot_id=str(raw.get("snapshot_id") or "") or None,
                    )
                )
            return changes
        return []
