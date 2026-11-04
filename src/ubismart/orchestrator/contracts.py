"""编排器的数据契约（Phase 3A）。

`TaskSpec` 是 orchestrator 交给 worker 的任务描述；`WorkerResult` 是 worker
回传给 fan-in 的结构化结果。两者都只包含只读能力 —— 并发写 worker 留到
Phase 3C。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

TaskRole = Literal[
    "explore",
    "test",
    "review",
    "implement",
    "document",
    "vision",
    "author",
    "sub_orchestrator",
]
WorkerStatus = Literal["success", "partial", "failed", "blocked", "cancelled"]
Branching = Literal["none", "bounded", "unbounded"]


class AcceptanceRule(BaseModel):
    """一条验收规则。

    `auto=True` 表示宿主可以自动检查（测试退出码、文件存在、schema 校验等）；
    `auto=False` 只能由人或 reviewer 判断。契约至少需要一条 auto 规则。
    """

    rule: str = Field(description="怎样算做对；尽量带可判定的谓词")
    auto: bool = Field(default=True, description="宿主是否能自动检查")


class SideEffect(BaseModel):
    """外部写动作登记。代码 agent 目前只需登记文件与 git 副作用。"""

    kind: Literal["file", "git", "external"] = "file"
    target: str = ""
    idempotency_key: str = ""
    compensation: str = ""


_AUTO_HINTS = (
    "测试",
    "test",
    "退出码",
    "exit",
    "通过",
    "pass",
    "存在",
    "包含",
    "生成",
    "路径",
    "文件",
    "file",
    "line",
    "schema",
    "格式",
    "行数",
    "覆盖",
    "url",
    "锚点",
    "引用",
    "证据",
    "断言",
    "构建",
    "编译",
)


def _looks_auto(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in _AUTO_HINTS)


def _coerce_acceptance(value: Any) -> Any:
    """模型常把 acceptance 写成纯字符串；按常见可判定词推断 auto。"""
    if not isinstance(value, list):
        return value
    result: list[Any] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                result.append({"rule": text, "auto": _looks_auto(text)})
        else:
            result.append(item)
    return result


def _coerce_side_effects(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    result: list[Any] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                result.append({"kind": "external", "target": text})
        else:
            result.append(item)
    return result


def _coerce_known(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        return {"note": value}
    if isinstance(value, list):
        return {str(index): item for index, item in enumerate(value)}
    return {}


def _coerce_string_list(value: Any) -> Any:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return value


class TaskContract(BaseModel):
    """执行前定下的验收契约（S1/S2）。

    未知项非空时宿主不进入 scheduler，而是把问题交回用户澄清；这是把
    “方向性错误整链作废”压缩成一次低成本问答的地方。
    """

    goal: str = Field(default="", description="一句话可判定的最终结果")
    constraints: list[str] = Field(
        default_factory=list, description="边界、禁止项、格式与范围"
    )
    acceptance: list[AcceptanceRule] = Field(
        default_factory=list, description="至少一条 auto=True 的规则"
    )
    known: dict[str, Any] = Field(
        default_factory=dict, description="已确认的事实与输入"
    )
    unknown: list[str] = Field(
        default_factory=list, description="缺失且不许猜的项；非空则阻断执行"
    )
    side_effects: list[SideEffect] = Field(
        default_factory=list, description="外部写动作及补偿方式"
    )

    @field_validator("acceptance", mode="before")
    @classmethod
    def _valid_acceptance(cls, value: Any) -> Any:
        return _coerce_acceptance(value)

    @field_validator("side_effects", mode="before")
    @classmethod
    def _valid_side_effects(cls, value: Any) -> Any:
        return _coerce_side_effects(value)

    @field_validator("constraints", "unknown", mode="before")
    @classmethod
    def _valid_string_lists(cls, value: Any) -> Any:
        return _coerce_string_list(value)

    @field_validator("known", mode="before")
    @classmethod
    def _valid_known(cls, value: Any) -> Any:
        return _coerce_known(value)


class TaskSpec(BaseModel):
    id: str = Field(description="任务唯一 id，例如 t1")
    role: TaskRole = Field(default="explore", description="explore 或 test")
    goal: str = Field(description="这个任务要回答/完成什么")
    inputs: list[str] = Field(
        default_factory=list, description="优先查看的文件、目录或符号"
    )
    acceptance: list[str] = Field(
        default_factory=list, description="怎样算完成"
    )
    # 契约映射：任务引用 TaskContract.acceptance 的下标。计划评审会检查
    # 每个任务至少映射到一条验收规则；模型未填时退化为按任务自身 acceptance 检查。
    acceptance_refs: list[int] = Field(
        default_factory=list, description="引用的契约验收规则下标"
    )
    # 任务级约束/已知/未知项：允许子任务补充父契约，但 unknown 非空同样阻断。
    constraints: list[str] = Field(default_factory=list)
    known: dict[str, Any] = Field(default_factory=dict)
    unknown: list[str] = Field(default_factory=list)
    side_effects: list[SideEffect] = Field(default_factory=list)
    # 不可验证的任务必须拆分；宿主不会执行 verifiable=false 的计划。
    verifiable: bool = Field(
        default=True, description="产出是否可被自动或 reviewer 判定"
    )
    # 分支上界：unbounded 会让预算失去意义，计划评审直接拒绝。
    branching: Branching = Field(default="none")
    # 子编排器（MAX_DEPTH=1）：只有确实需要独立契约/检查点的子任务才用。
    sub_contract: TaskContract | None = None
    # 父分配的预算切片；子无权自行放大。键：max_tokens/max_wall_ms。
    budget_slice: dict[str, int] = Field(default_factory=dict)
    depends_on: list[str] = Field(
        default_factory=list, description="必须先行完成的任务 id"
    )
    # full：依赖必须 success（写任务与实现相关分析的默认值）；
    # any：依赖 success 或 partial 即可开始（review/test 只需要产出，不需要
    # 依赖方通过完整验证）。
    depends_mode: Literal["full", "any"] = Field(
        default="full", description="依赖满足口径：full=必须 success；any=success/partial"
    )
    # Phase 3C：implement 任务必须声明允许修改的文件/目录；只读任务必须为空。
    write_scope: list[str] = Field(default_factory=list)
    # 该任务在 worktree 中允许的修复轮次（宿主默认值兜底）。
    max_repairs: int | None = None
    # 任务级预算。None 表示使用全局配置兜底。
    max_rounds: int | None = None
    timeout: int | None = None
    # 0/None 表示不限制；scheduler 按 provider 回报的 usage 累计判断。
    max_tokens: int | None = None
    # 宿主可覆盖的调度优先级（越大越先启动）；模型给出的值会被 clamp。
    priority: int | None = None
    # 阶段屏障：高 stage 的汇总/撰写任务必须等低 stage 任务收敛。
    stage: int = Field(default=0, ge=0)
    # 报告交付：md/docx/pdf；仅 author 使用。
    output_format: str = ""
    output_path: str = ""
    # 可选报告模板：工作区相对路径或内联模板。
    template: str = ""
    # author/review 的必需章节；report_check 会校验缺失。
    required_sections: list[str] = Field(default_factory=list)
    # document/vision 任务的证据来源（path + anchor）。
    sources: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("side_effects", mode="before")
    @classmethod
    def _valid_side_effects(cls, value: Any) -> Any:
        return _coerce_side_effects(value)

    @field_validator(
        "acceptance",
        "inputs",
        "depends_on",
        "constraints",
        "unknown",
        mode="before",
    )
    @classmethod
    def _valid_string_lists(cls, value: Any) -> Any:
        return _coerce_string_list(value)

    @field_validator("known", mode="before")
    @classmethod
    def _valid_known(cls, value: Any) -> Any:
        return _coerce_known(value)


class TaskGraph(BaseModel):
    # S1/S2：契约先于任务图。旧调用方/旧模型可以不返回，宿主会补齐。
    contract: TaskContract | None = None
    tasks: list[TaskSpec] = Field(default_factory=list)
    # 模型没给出可用任务图时为 True；调用方会退化为单 explore 任务。
    degraded: bool = False


class SubTask(BaseModel):
    """父 → 子编排器的内部扩展消息；不出站。"""

    sub_id: str
    parent_task_id: str = ""
    depth: int = 1
    contract_slice: TaskContract
    budget_slice: dict[str, int] = Field(default_factory=dict)
    plan_version: int = 0
    max_depth: int = 1
    max_fanout: int = 1
    expected_schema: str = ""
    cancel_scope: str = ""


class SubResult(BaseModel):
    """子 → 父的单个结构化结果；不写父状态。"""

    sub_id: str
    parent_task_id: str = ""
    depth: int = 1
    status: Literal["success", "partial", "failed", "blocked", "cancelled"] = (
        "failed"
    )
    artifact: dict[str, Any] = Field(default_factory=dict)
    local_ckpt_digest: str = ""
    actual_cost: dict[str, int] = Field(default_factory=dict)
    rollback_level: str = ""
    fault_reason: str = ""
    suggested_action: str = ""


class SubSignal(BaseModel):
    """子 → 父的进度/故障信号；只到 Root Projector，不直接出站。"""

    sub_id: str
    parent_task_id: str = ""
    depth: int = 1
    kind: Literal["progress", "fault", "stage_done"] = "progress"
    severity: Literal["info", "warn", "error"] = "info"
    progress_digest: dict[str, Any] = Field(default_factory=dict)
    dedupe_key: str = ""


class WorkflowDecision(BaseModel):
    workflow: Literal[
        "direct",
        "parallel_readonly",
        "orchestrated_implement",
        "document_pipeline",
        "architecture_change",
    ] = "direct"
    reason: str = ""


class WorkerResult(BaseModel):
    task_id: str
    role: str = "explore"
    worker_id: str = ""
    goal: str = ""
    status: WorkerStatus = "failed"
    summary: str = ""
    files: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    # 只读 worker 提供的 file:line 证据；宿主已校验路径存在。
    citations: list[dict[str, Any]] = Field(default_factory=list)
    # 只读 worker 建议追加的任务；由 extend_tasks 统一校验后进入 pending。
    proposed_tasks: list[dict[str, Any]] = Field(default_factory=list)
    # 文档/图片 worker 产出的证据锚点，供 fan-in 与 author 使用。
    sources: list[dict[str, Any]] = Field(default_factory=list)
    # 文档覆盖报告：{status, documents:[{path,pages,read_ranges,unread,reason}]}。
    coverage: dict[str, Any] = Field(default_factory=dict)
    # 每篇文档的固定槽位矩阵：problem/method/algorithm/experiments/...
    matrix: list[dict[str, Any]] = Field(default_factory=list)
    # 跨文档归属校验：{mentioned:[path], cited:[path], text:"..."}
    attribution_issues: list[dict[str, Any]] = Field(default_factory=list)
    output_format: str = ""
    output_path: str = ""
    verification: dict[str, Any] = Field(default_factory=dict)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    # 这是该任务的第几次尝试（1 = 首次）
    attempt: int = 1
    # Phase 3C：implement worker 生成的补丁路径与合并结果
    patch_path: str = ""
    merged: bool = False
    merge_error: str = ""
    # 合并前为主工作区里被补丁改动的文件留下的快照 id（失败为空）。
    # 没有它，`agent undo` 无法回滚编排写 worker 合并出来的改动。
    snapshot_ids: list[str] = Field(default_factory=list)
    error: str = ""
    # worker 内部执行的工具调用（供审计层逐条落账）
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    # ---- Phase 5：调度 / 成本 / 审计指标 ----
    # 从进入 ready 队列到真正拿到并发槽位的等待时间。
    queue_wait_ms: int = 0
    # 从 worker 完成到补丁真正合并进主工作区的等待时间。
    merge_wait_ms: int = 0
    # 顺序写/修复轮注入 worktree 的基线文件与字节数（审计用）。
    seed_files: list[str] = Field(default_factory=list)
    seed_bytes: int = 0
    # LLM 与工具分别占用的时间；用于定位成本/延迟来源。
    llm_ms: int = 0
    tool_ms: int = 0
    # 是否因为 deadline/任务 timeout 被取消。
    timed_out: bool = False
    # 写 worker 在运行中试图越界但被写时守卫拦下的路径。
    scope_violations: list[str] = Field(default_factory=list)
    # fan-in 的结构化结果落盘位置。
    artifact_id: str = ""
    # 任务在依赖图中的深度，fan-in 排序用。
    depth: int = 0
    # 调度优先级（越大越先启动）。
    priority: int = 0
    # 本任务实际消耗的预算摘要（tokens / rounds / seconds）。
    budget_used: dict[str, int] = Field(default_factory=dict)
    # 结果来自 worker journal（上次运行已完成），本次没有重新执行。
    reused: bool = False
    # 子编排器的结构化结果与上浮信号（只内部使用，不出站）。
    sub_result: dict[str, Any] = Field(default_factory=dict)
    sub_signals: list[dict[str, Any]] = Field(default_factory=list)
    parent_sub_id: str = ""
    fanout_index: int = 0
    sub_spawn_mode: str = ""
    sub_budget_used: int = 0
