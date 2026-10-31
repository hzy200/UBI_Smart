"""编排器的数据契约（Phase 3A）。

`TaskSpec` 是 orchestrator 交给 worker 的任务描述；`WorkerResult` 是 worker
回传给 fan-in 的结构化结果。两者都只包含只读能力 —— 并发写 worker 留到
Phase 3C。
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

TaskRole = Literal[
    "explore", "test", "review", "implement", "document", "vision", "author"
]
WorkerStatus = Literal["success", "partial", "failed", "blocked", "cancelled"]


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


class TaskGraph(BaseModel):
    tasks: list[TaskSpec] = Field(default_factory=list)
    # 模型没给出可用任务图时为 True；调用方会退化为单 explore 任务。
    degraded: bool = False


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
