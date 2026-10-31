"""AgentRuntime —— 唯一编排入口。

所有前端（CLI / TUI / Web）都只跟这一层打交道：
它持有图、沙箱与安全策略，把 LangGraph 的原始流翻译成语义化的领域事件。
前端拿不到工具，也就无法绕过命令分级审批。

    async for event in AgentRuntime(settings).run("列出文件", thread_id="x"):
        match event.type: ...
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import uuid4

from langchain_core.messages import HumanMessage
from langgraph.types import Command

from ubismart.agents.policy import (
    RepoProfile,
    delegation_policy,
    profile_workspace,
    should_orchestrate,
)
from ubismart.audit import AuditLogger, AuditRecord
from ubismart.audit import models as audit_models
from ubismart.audit.logger import (
    now_iso,
    truncate,
)
from ubismart.config import (
    PRODUCTION_PROFILES,
    Settings,
    apply_security_profile,
    get_settings,
)
from ubismart.events import (
    ApprovalRequested,
    Event,
    RunFailed,
    SteeringApplied,
    ToolCallStarted,
)
from ubismart.execution import (
    ExecutionSandbox,
    ExecutionService,
    SandboxExecutionBackend,
)
from ubismart.graph.build import build_graph
from ubismart.identity.access import (
    ACCESS_READ_ONLY,
    ACCESS_WRITE_ASK,
    ACCESS_WRITE_AUTO,
    normalize_access_mode,
)
from ubismart.llm.ratelimit import GLOBAL_LIMITER
from ubismart.memory.checkpointer import CheckpointStore
from ubismart.memory.semantic import SemanticMemory
from ubismart.platform.concurrency import ConcurrencyGovernor
from ubismart.platform.secrets_store import SecretStore
from ubismart.protocol.messages import (
    recover_dangling_tool_calls,
)
from ubismart.runtime.audit_bridge import AuditMixin
from ubismart.runtime.common import _swallow_audit
from ubismart.runtime.event_map import EventMapMixin
from ubismart.runtime.memory import MemoryMixin
from ubismart.runtime.sessions import SessionsMixin
from ubismart.runtime.stream import StreamMixin
from ubismart.runtime_snapshots import SnapshotMixin
from ubismart.sandbox.backend import build_sandbox
from ubismart.sandbox.fs import SandboxFs
from ubismart.sandbox.pathguard import normalize_root
from ubismart.sandbox.policy import (
    SessionPolicy,
    parse_allow_rules,
)
from ubismart.sandbox.snapshots import (
    SnapshotStore,
)
from ubismart.sandbox.wsl_exec import resolve_workspace
from ubismart.session.session_brief import build_session_brief
from ubismart.session.steering import SteeringCommand, SteeringController
from ubismart.session.workspace_lock import WorkspaceLock
from ubismart.workspace.model import normalize_workspace_kind

# 只有这几个 action 才真正改动了文件，read 与空回滚不发 FileChanged

# 只有这两个节点的模型输出是给用户看的文本；planner 的结构化调用不流式渲染

# 同一 runtime 同时只允许一个事件流。工作区锁是可重入的，不能替代这层保护：
# Web 端并发 resume 会同时驱动同一个 LangGraph/checkpointer。
_STREAM_BUSY_MESSAGE = (
    "该会话已有正在执行的事件流；请等待当前运行结束，"
    "或先消费挂起/审批事件。"
)
# planner 没解析出步骤、退化成单步时记进审计的 detail，供事后复盘
# 验证是否「放行」的唯一口径。与路由判定同源（graph/routing.route_after_verify
# 只在 failed 时拦下任务）：skipped / not_configured 表示**没验证**，不是**验证
# 失败**。曾经事件层按前者记、审计层按后者记，于是同一次 skipped 在两边结论相反，
# 事后对账对不上 —— 审计本该是可信来源，所以两处都必须走这个函数。


# 审查是否「拦下了这一步」的唯一口径。与路由同源：只有 blocked 才改控制流，
# warned / clean / skipped 都只是记录。审查没能执行（status=warned 且带
# review-unavailable）**不算阻断** —— 工具坏了不该把用户的任务卡死。


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


class AgentRuntime(
    SnapshotMixin,
    EventMapMixin,
    StreamMixin,
    AuditMixin,
    SessionsMixin,
    MemoryMixin,
):
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

    def _attachment_inventory(self, thread_id: str) -> str:
        """本会话上传文件的元数据清单；不读正文、不跨会话。"""
        from ubismart.execution.file_access import (
            LocalFileAccess,
            SandboxFileAccess,
        )
        from ubismart.sandbox.fs import SandboxFs
        from ubismart.workspace.file_preview import list_session_uploads

        try:
            if self.workspace_kind == "host":
                access = self.file_access or LocalFileAccess(self.workspace)
            else:
                access = SandboxFileAccess(
                    SandboxFs(self._sandbox, self.workspace),
                    self.workspace,
                )
            rows = list_session_uploads(access, thread_id, limit=20)
        except Exception:  # noqa: BLE001 - 附件清单是增强信息
            return ""
        if not rows:
            return ""

        def _size(value: int) -> str:
            size = float(value or 0)
            for unit in ("B", "KB", "MB", "GB"):
                if size < 1024 or unit == "GB":
                    return (
                        f"{size:.0f}{unit}"
                        if unit == "B"
                        else f"{size:.1f}{unit}"
                    )
                size /= 1024
            return f"{size:.1f}GB"

        lines = [
            "本会话上传的文件（只读；文档用 document_read，图片用 image_inspect）："
        ]
        used = 0
        for row in rows:
            suffix = str(row.get("suffix") or "").lstrip(".").upper() or "文件"
            line = (
                f"- {row['path']}（{suffix}，"
                f"{_size(int(row.get('size') or 0))}）"
            )
            if used + len(line) > 2_000:
                lines.append("…（其余附件已省略）")
                break
            lines.append(line)
            used += len(line)
        return "\n".join(lines)

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
                from ubismart.identity.instructions import load_project_instructions

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
            and should_orchestrate(prompt, profile, self.settings)
        ):
            execution_mode = "orchestrated"
        # 编排模式下探索已经由 worker 覆盖；再把 delegate_explore 绑给主模型
        # 会让同一片代码被探索两次，且两套预算互不相干。direct 路径不变。
        inline_delegation = bool(
            policy.allowed and execution_mode != "orchestrated"
        )

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

        attachments = self._attachment_inventory(thread_id)
        if attachments:
            prompt = f"{prompt}\n\n{attachments}"

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
            # 修复轮（RFC-0008）：每轮 run 从零开始。
            "orchestrator_fix_eligible": False,
            "orchestrator_fix_mode": False,
            "orchestrator_fix_rounds_used": 0,
            "orchestrator_fix_request": "",
            "force_workflow": "",
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
            "delegation_allowed": inline_delegation,
            "delegation_budget": policy.max_calls if inline_delegation else 0,
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
            # 孤儿 worktree 每个 run 只清理一次；scheduler 边界会重入。
            "worktrees_pruned": False,
            # 顺序写的基线：本 run 已合并到主工作区的文件。
            "merged_paths": [],
            "merge_batches": [],
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
    # 回滚（用户主动发起，不走审批；审批管的是模型发起的动作）
    # ------------------------------------------------------------------

    # 快照/回滚/差异/上传留底已移到 `runtime_snapshots.SnapshotMixin`，
    # 事件翻译在 `event_map.EventMapMixin`，流式与成本汇总在
    # `stream.StreamMixin`；本类通过多继承拿到它们。

    # ------------------------------------------------------------------
    # 审计
    # ------------------------------------------------------------------


    # ------------------------------------------------------------------
    # 单条更新 → 事件
    # ------------------------------------------------------------------


