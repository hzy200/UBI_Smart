"""任务图生成与重规划。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig

from ubismart.config import Settings
from ubismart.graph.state import AgentState
from ubismart.knowledge.architecture import apply_change_contract
from ubismart.llm.deepseek import with_json_output
from ubismart.orchestrator.contracts import TaskGraph
from ubismart.orchestrator.graph import validate_tasks
from ubismart.orchestrator.parallelism import (
    dedupe_readonly_tasks,
    parallel_stats,
)
from ubismart.orchestrator.state import request_of
from ubismart.orchestrator.survey import compact_repo_summary
from ubismart.session.recovery import retry_async


def _default_write_task(
    request: str, summary: dict[str, Any]
) -> dict[str, Any] | None:
    """写模式下的保守兜底：单个 implement，覆盖源码顶层目录。"""
    dirs = [
        str(item).strip().strip("/")
        for item in (summary.get("top_dirs") or [])
        if str(item).strip()
        and str(item).strip() not in {"tests", "test", ".agent"}
    ]
    files = [
        str(item).strip().strip("/")
        for item in (summary.get("top_files") or [])
        if str(item).strip() and not str(item).startswith(".")
    ]
    scope = [f"{directory}/" for directory in dirs] if dirs else files
    if not scope:
        return None
    return {
        "id": "implement-1",
        "role": "implement",
        "goal": request or "完成用户请求",
        "inputs": [],
        "acceptance": [],
        "depends_on": [],
        "write_scope": scope,
    }


PLAN_TASKS_PROMPT = """\
你是编码智能体的编排器。把任务拆成 1–{max_tasks} 个子任务，交给并发 worker：

{role_notes}

{write_rules}

硬性要求：
- 每个任务必须有一个清晰、可独立完成的目标；
- 只有确实需要前序结果时才写 depends_on；
- `review` 通常应独立于 `test`（除非要专门审查测试结果），否则测试失败会把审查一起阻塞；
- 不要把一条连贯的功能线拆成多个重复的 explore；
- review/test 若只需要依赖任务的产出（即使它最终是 partial），把 depends_mode
  设为 "any"；其余任务保持 "full"（默认）。
- 只输出 JSON：{{"tasks": [{{"id": "t1", "role": "explore", "goal": "...",
  "inputs": [], "acceptance": [], "depends_on": [], "depends_mode": "full",
  "write_scope": []}}]}}
"""

_READ_ONLY_ROLE_NOTES = """\
- `explore`：定位/理解代码。给出 goal、inputs（优先看的文件/目录）、acceptance。
- `test`：运行项目现有测试并总结失败项。无需 inputs。
- `review`：对 inputs 中的文件做只读代码审查，返回风险/遗漏。不要修改文件。
- `test`/`review` 依赖实现任务时可用 depends_mode="any"：实现任务的产出
  （文件/摘要）已经可用即可，不要求它通过完整验证。"""

_IMPLEMENT_ROLE_NOTES = """\
- `implement`：修改 `write_scope` 内的文件完成任务。必须给出 write_scope
  （文件或目录，例如 "app/reporting.py" 或 "app/handlers/"）。
- `explore` / `test` / `review`：只读辅助任务，不能修改文件。
- `test`/`review` 依赖 implement 时可用 depends_mode="any" 消费 partial 产出。"""

_READ_ONLY_RULES = "- 只允许 explore / test / review，**不要生成实现/修改任务**。"

_IMPLEMENT_RULES = """\
- 允许 explore / test / review / implement。
- 每个 implement 必须写 write_scope；同一 stage 的不同 implement 的
  write_scope **不能重叠**。
- 如果用户请求点名了具体文件（例如「在 app/alpha.py 实现…」），write_scope
  必须**精确到这些文件**，不要用整个目录。
- implement 只改源码，不要改测试文件（除非任务明确要求）。"""

_NO_SEQUENTIAL_WRITE_RULES = """\
- implement 任务之间**不能有 depends_on**（它们基于同一个 HEAD 并发修改）。"""

_SEQUENTIAL_WRITE_RULES = """\
- 默认仍让 implement 互相独立；只有确实需要"后一步基于前一步产出"时，
  才给后一个 implement 更大的 `stage`，并在 `depends_on` 里引用前一个
  implement。依赖的 implement 的 stage **必须严格更小**。
- 同一 stage 的 implement 仍然不能互相依赖，write_scope 也不能重叠；
  跨 stage 允许写范围重叠。跨 stage 依赖使用 `depends_mode="any"`：
  前一波补丁合并后即可继续，即使它自己的内部验证是 partial。"""

_DOCUMENT_ROLE_NOTES = """\
- `document`：读取一份文档（inputs 里放路径），提取大纲、关键点、表格与引用锚点。
  没有 inputs 时表示“汇总其他任务的结果”。
- `vision`：理解一张图片（inputs 里放路径），提取图中文字、表格、图表或界面信息。
- `review`：校验报告的引用锚点、章节与格式；inputs 必须放报告路径，
  不修改文件。
- 汇总任务 depends_mode 用 "any"：单份文档失败不应阻塞整体，但要在结果里说明缺口。"""

_AUTHOR_ROLE_NOTE = """\
- `author`：根据依赖任务的摘要/引用撰写报告。必须给出 write_scope（默认报告目录）、
  output_format（md/docx/pdf）与 output_path；一个 author 只产出一个交付文件。
  用户提供报告模板时填 `template`（工作区相对路径）。
  用户明确要求多种格式时，为每种格式各建一个 author 任务（最多 3 个），
  它们会串行执行。"""

_DOCUMENT_RULES = """\
- 允许 document / vision / review{author}。
- 每份文档或图片单独一个任务，inputs 只放该文件的相对路径。
- 先并行提取（stage 0），再用一个 document 汇总任务（stage 1），
  汇总 depends_on 所有提取任务且 depends_mode="any"。
- 不要生成 implement 任务；不要修改源码。"""

_DOCUMENT_AUTHOR_RULES = """\
- author 依赖汇总任务（depends_on=["summary"]，depends_mode="full"）。
- author 的 write_scope 只能是报告目录；output_format 按用户指定，
  未指定时按上下文二选一，无法判断用 md；只产出一个文件。
- 用户明确要求多个格式时才生成多个 author 任务。"""


def _default_document_tasks(
    request: str,
    summary: dict[str, Any],
    settings: Settings,
    *,
    allow_author: bool,
) -> list[dict[str, Any]] | None:
    """规划降级时的确定性文档任务图。"""
    docs = [
        str(item)
        for item in (
            list(summary.get("uploads") or [])
            + list(summary.get("documents") or [])
        )
        if str(item).strip()
    ]
    images = [
        str(item) for item in (summary.get("images") or []) if str(item).strip()
    ]
    limit = max(1, int(getattr(settings, "orchestrator_max_tasks", 6)))
    # 留三个位置给 summary/author/review。
    slots = max(limit - 3, 1)
    max_docs = min(
        slots,
        int(getattr(settings, "orchestrator_document_max_docs", 10)),
    )
    max_images = min(
        slots,
        int(getattr(settings, "orchestrator_document_max_images", 20)),
    )
    docs = list(dict.fromkeys(docs))[:max_docs]
    images = list(dict.fromkeys(images))[
        : max(min(slots - len(docs), max_images), 0)
    ]
    tasks: list[dict[str, Any]] = []
    for index, path in enumerate(docs, 1):
        tasks.append(
            {
                "id": f"doc-{index}",
                "role": "document",
                "goal": f"读取并提炼 {path} 的大纲与关键点",
                "inputs": [path],
                "acceptance": ["给出带页码/工作表/幻灯片锚点的关键点"],
                "depends_on": [],
                "stage": 0,
            }
        )
    for index, path in enumerate(images, 1):
        tasks.append(
            {
                "id": f"img-{index}",
                "role": "vision",
                "goal": f"理解图片 {path} 的内容并提取关键信息",
                "inputs": [path],
                "acceptance": ["给出描述与图中文字/图表信息"],
                "depends_on": [],
                "stage": 0,
            }
        )
    if not tasks:
        return None
    ids = [str(task["id"]) for task in tasks]
    tasks.append(
        {
            "id": "summary",
            "role": "document",
            "goal": f"汇总以上文档/图片的关键点，去重并建立引用索引：{request}",
            "inputs": [],
            "acceptance": ["每条结论带来源路径与锚点"],
            "depends_on": ids,
            "depends_mode": "any",
            "stage": 1,
        }
    )
    if allow_author:
        from ubismart.tools.report import (
            resolve_output_formats,
            resolve_required_sections,
        )

        formats = resolve_output_formats(request)
        required_sections = resolve_required_sections(request)
        report_dir = str(
            getattr(settings, "report_output_dir", ".agent/artifacts/reports")
        ).strip("/")
        author_ids: list[str] = []
        author_paths: list[str] = []
        for index, fmt in enumerate(formats, 1):
            author_id = "author" if len(formats) == 1 else f"author-{index}"
            output_path = (
                f"{report_dir}/report.{fmt}"
                if len(formats) == 1
                else f"{report_dir}/report-{index}.{fmt}"
            )
            author_ids.append(author_id)
            author_paths.append(output_path)
            tasks.append(
                {
                    "id": author_id,
                    "role": "author",
                    "goal": f"根据汇总结果撰写 {fmt.upper()} 报告：{request}",
                    "inputs": [],
                    "acceptance": [f"生成 {output_path}", "引用可追溯"],
                    "depends_on": ["summary"],
                    "stage": 2,
                    "write_scope": [report_dir],
                    "output_format": fmt,
                    "output_path": output_path,
                    "required_sections": required_sections,
                }
            )
        tasks.append(
            {
                "id": "review",
                "role": "review",
                "goal": "校验报告引用、章节与格式",
                "inputs": author_paths,
                "acceptance": ["引用锚点存在", "文件可打开"],
                "depends_on": author_ids,
                "depends_mode": "any",
                "stage": 3,
                "required_sections": required_sections,
            }
        )
    return tasks


def _completed_view(
    tasks: list[dict[str, Any]],
    status: dict[str, str],
    results: list[dict[str, Any]],
) -> str:
    """给重规划模型看的「已完成，不要重复」清单。"""
    latest: dict[str, dict[str, Any]] = {}
    for result in results:
        task_id = str(result.get("task_id", ""))
        previous = latest.get(task_id)
        if previous is None or int(result.get("attempt", 1)) >= int(
            previous.get("attempt", 1)
        ):
            latest[task_id] = result
    lines: list[str] = []
    for task in tasks:
        task_id = str(task.get("id", ""))
        item = latest.get(task_id) or {}
        summary = str(item.get("summary", "")).strip()[:160]
        merged = "，已合并" if item.get("merged") else ""
        lines.append(
            f"- {task_id} ({task.get('role', '')}/{status.get(task_id, '')})"
            f"{merged}: {summary}"
        )
    return "\n".join(lines) or "（无）"


def make_plan_tasks_node(
    llm: BaseChatModel,
    settings: Settings,
    *,
    allow_write: bool = False,
    repo_map_builder: Callable[[], dict[str, Any]] | None = None,
) -> Callable[[AgentState, RunnableConfig], Awaitable[dict[str, Any]]]:
    # 外层 retry_async 已经负责重试；不要再叠一层。
    structured = with_json_output(llm, TaskGraph)

    async def plan_tasks(
        state: AgentState, config: RunnableConfig
    ) -> dict[str, Any]:
        request = request_of(state)
        summary = dict(state.get("repo_summary") or {})
        contract = dict(
            state.get("change_contract")
            or summary.get("change_contract")
            or {}
        )
        contract_waves = contract.get("waves") or []
        effective_sequential = bool(
            settings.orchestrator_sequential_writes
            or len(contract_waves) > 1
        )
        effective_max_stages = max(
            int(settings.orchestrator_max_write_stages or 2),
            int(settings.deep_max_write_stages or 4)
            if contract_waves
            else 0,
            len(contract_waves),
        )

        def _prepare(raw: list[Any]) -> list[dict[str, Any]]:
            """转成 dict，并在有契约时按波次分 stage。"""
            data = [
                item.model_dump()
                if hasattr(item, "model_dump")
                else dict(item)
                for item in raw
            ]
            if contract_waves:
                apply_change_contract(
                    data,
                    contract,
                    max_stages=effective_max_stages,
                )
            return data
        # lazy 模式下 survey 没有构建 repo_map；确认要编排后在这里补一次，
        # 失败按空串继续（worker 仍可从零搜索）。
        if repo_map_builder is not None and not summary.get("repo_map"):
            try:
                summary.update(repo_map_builder() or {})
            except Exception:  # noqa: BLE001 - 索引失败不阻断规划
                pass
        session_brief = str(state.get("session_brief") or "").strip()
        recovery_notice = str(state.get("recovery_notice") or "").strip()
        replan_mode = bool(state.get("steering_replan"))
        fix_mode = bool(state.get("orchestrator_fix_mode"))
        existing_tasks = [dict(task) for task in (state.get("tasks") or [])]
        existing_status = dict(state.get("task_status") or {})
        results = state.get("worker_results") or []
        completed_tasks = [
            task
            for task in existing_tasks
            if existing_status.get(task["id"]) in {"success", "partial"}
        ]
        merged_any = any(
            result.get("role") == "implement" and result.get("merged")
            for result in results
        )
        write_mode = bool(
            allow_write
            and settings.orchestrator_write_enabled
            and state.get("workflow")
            in {"orchestrated_implement", "architecture_change"}
            and (not merged_any or fix_mode)
        )
        document_mode = state.get("workflow") == "document_pipeline"
        author_mode = bool(document_mode and allow_write and not merged_any)
        # 修复轮只修失败项：给模型的计划上限压到 3，避免重新铺开整个任务。
        plan_max_tasks = (
            min(settings.orchestrator_max_tasks, 3)
            if fix_mode
            else settings.orchestrator_max_tasks
        )
        if document_mode:
            role_notes = _DOCUMENT_ROLE_NOTES
            if author_mode:
                role_notes += "\n" + _AUTHOR_ROLE_NOTE
            write_rules = _DOCUMENT_RULES.format(
                author=" / author" if author_mode else ""
            )
            if author_mode:
                write_rules += "\n" + _DOCUMENT_AUTHOR_RULES
        elif write_mode:
            role_notes = _IMPLEMENT_ROLE_NOTES
            write_rules = _IMPLEMENT_RULES + "\n" + (
                _SEQUENTIAL_WRITE_RULES
                if settings.orchestrator_sequential_writes
                else _NO_SEQUENTIAL_WRITE_RULES
            )
            if settings.orchestrator_allow_overlap:
                write_rules += (
                    "\n- 允许不同 implement 修改同一文件；"
                    "宿主会用 3-way 合并；"
                    "冲突时只回滚冲突文件所在分量。"
                )
        else:
            role_notes = _READ_ONLY_ROLE_NOTES
            write_rules = _READ_ONLY_RULES
        body = (
            f"用户请求：{request}\n\n"
            + (
                "之前轮次的会话约束（继续遵守）：\n"
                f"{session_brief}\n\n"
                if session_brief
                else ""
            )
            + (
                f"上次运行中断的恢复提示：\n{recovery_notice}\n\n"
                if recovery_notice
                else ""
            )
            + f"仓库画像：{compact_repo_summary(summary)}\n\n"
            f"工作流：{state.get('workflow', '')}\n\n"
        )
        if replan_mode:
            body += (
                "用户要求重新规划剩余工作。以下任务已经完成，不要重复：\n"
                f"{_completed_view(completed_tasks, existing_status, results)}\n\n"
            )
            if merged_any:
                body += "注意：已有实现补丁合并；新增任务只能使用 explore/test/review。\n\n"
        if fix_mode:
            body += (
                "这是自动验证失败后的修复轮：只生成针对失败项的 implement "
                "任务，不要重做已经完成的功能，也不要重新规划整个请求。\n\n"
            )
        body += "请生成任务图。"

        tasks: list[dict[str, Any]] = []
        degraded = False
        retry_events: list[dict[str, Any]] = []
        errors: list[str] = []
        raw_tasks: list[dict[str, Any]] = []
        prefetched = [
            dict(task) for task in (state.get("prefetched_tasks") or [])
        ]
        if prefetched and not replan_mode:
            # select_workflow 已经把任务图一并返回；不要再付一次 LLM 往返。
            raw_tasks = prefetched
            tasks, degraded, errors = validate_tasks(
                _prepare(prefetched),
                request=request,
                max_tasks=settings.orchestrator_max_tasks,
                allow_write=write_mode,
                allow_overlap=settings.orchestrator_allow_overlap,
                allow_author=author_mode,
                max_documents=settings.orchestrator_document_max_docs,
                max_images=settings.orchestrator_document_max_images,
                allow_sequential_write=effective_sequential,
                max_write_stages=effective_max_stages,
            )
            degraded = degraded or bool(state.get("prefetched_degraded"))
        else:
            try:
                graph = await retry_async(
                    lambda: structured.ainvoke(
                        [
                            SystemMessage(
                                content=PLAN_TASKS_PROMPT.format(
                                    max_tasks=plan_max_tasks,
                                    role_notes=role_notes,
                                    write_rules=write_rules,
                                )
                            ),
                            HumanMessage(content=body),
                        ],
                        config,
                    ),
                    attempts=settings.retry_attempts,
                    base_delay=settings.retry_base_delay_s,
                    max_delay=settings.retry_max_delay_s,
                    max_total_wait=settings.retry_max_total_wait_s,
                    on_retry=retry_events.append,
                    label="plan_tasks",
                )
                raw_tasks = [task.model_dump() for task in graph.tasks]
                tasks, degraded, errors = validate_tasks(
                    _prepare(raw_tasks),
                    request=request,
                    max_tasks=settings.orchestrator_max_tasks,
                    allow_write=write_mode,
                    allow_overlap=settings.orchestrator_allow_overlap,
                    allow_author=author_mode,
                    max_documents=settings.orchestrator_document_max_docs,
                    max_images=settings.orchestrator_document_max_images,
                    allow_sequential_write=effective_sequential,
                    max_write_stages=effective_max_stages,
                )
                degraded = degraded or bool(graph.degraded)
            except Exception:  # noqa: BLE001 - 规划失败降级
                tasks, degraded, errors = validate_tasks(
                    [],
                    request=request,
                    max_tasks=settings.orchestrator_max_tasks,
                    allow_write=write_mode,
                    allow_overlap=settings.orchestrator_allow_overlap,
                    allow_author=author_mode,
                    max_documents=settings.orchestrator_document_max_docs,
                    max_images=settings.orchestrator_document_max_images,
                    allow_sequential_write=effective_sequential,
                    max_write_stages=effective_max_stages,
                )

        # 校验失败时先给模型一次带错误反馈的修复机会；仍然失败才走
        # 确定性降级（write/document 的单任务兜底）。所有修复结果仍需
        # 重新经过 host 校验，安全边界不变。
        if (
            degraded
            and raw_tasks
            and not replan_mode
            and settings.orchestrator_plan_repair
        ):
            repair_note = (
                "\n\n上次任务图未通过宿主校验，请修正后重新输出**完整**任务图。\n"
                "校验错误：\n"
                + "\n".join(f"- {item}" for item in errors[:10])
                + "\n\n上次任务图：\n"
                + json.dumps(raw_tasks, ensure_ascii=False, default=str)[:4_000]
            )
            repaired_graph: TaskGraph | None = None
            try:
                repaired_graph = await retry_async(
                    lambda: structured.ainvoke(
                        [
                            SystemMessage(
                                content=PLAN_TASKS_PROMPT.format(
                                    max_tasks=plan_max_tasks,
                                    role_notes=role_notes,
                                    write_rules=write_rules,
                                )
                                + repair_note
                            ),
                            HumanMessage(content=body),
                        ],
                        config,
                    ),
                    attempts=settings.retry_attempts,
                    base_delay=settings.retry_base_delay_s,
                    max_delay=settings.retry_max_delay_s,
                    max_total_wait=settings.retry_max_total_wait_s,
                    on_retry=retry_events.append,
                    label="plan_tasks_repair",
                )
            except Exception:  # noqa: BLE001 - 修复失败按原降级处理
                repaired_graph = None
            if repaired_graph is not None:
                repaired_tasks, repaired_degraded, repaired_errors = (
                    validate_tasks(
                        _prepare(
                            [task.model_dump() for task in repaired_graph.tasks]
                        ),
                        request=request,
                        max_tasks=settings.orchestrator_max_tasks,
                        allow_write=write_mode,
                        allow_overlap=settings.orchestrator_allow_overlap,
                        allow_author=author_mode,
                        max_documents=settings.orchestrator_document_max_docs,
                        max_images=settings.orchestrator_document_max_images,
                        allow_sequential_write=effective_sequential,
                        max_write_stages=effective_max_stages,
                    )
                )
                repaired_degraded = repaired_degraded or bool(
                    repaired_graph.degraded
                )
                if not repaired_degraded:
                    tasks = repaired_tasks
                    degraded = False
                    errors = [
                        f"初次校验失败（{len(errors)} 项），修复后通过"
                    ]
                else:
                    errors = errors + [
                        f"修复仍失败：{item}" for item in repaired_errors[:5]
                    ]

        if degraded and write_mode:
            fallback = _default_write_task(request, summary)
            if fallback is not None:
                tasks, fallback_degraded, fallback_errors = validate_tasks(
                    [fallback],
                    request=request,
                    max_tasks=settings.orchestrator_max_tasks,
                    allow_write=True,
                )
                if not fallback_degraded:
                    degraded = True
                else:
                    errors = errors + fallback_errors
            else:
                errors.append(
                    "写编排降级：工作区没有可用的顶层目录或文件作为 write_scope"
                )

        if degraded and document_mode:
            fallback_tasks = _default_document_tasks(
                request, summary, settings, allow_author=author_mode
            )
            if fallback_tasks:
                tasks, fallback_degraded, fallback_errors = validate_tasks(
                    fallback_tasks,
                    request=request,
                    max_tasks=settings.orchestrator_max_tasks,
                    allow_write=False,
                    allow_author=author_mode,
                )
                if not fallback_degraded:
                    degraded = True
                else:
                    errors = errors + fallback_errors
                available_docs = len(
                    set(
                        list(summary.get("uploads") or [])
                        + list(summary.get("documents") or [])
                    )
                )
                available_images = len(set(summary.get("images") or []))
                kept_docs = sum(
                    1 for task in fallback_tasks if task["role"] == "document"
                )
                kept_images = sum(
                    1 for task in fallback_tasks if task["role"] == "vision"
                )
                if available_docs > kept_docs:
                    errors.append(
                        f"文档数 {available_docs} 超过上限，"
                        f"已保留前 {kept_docs} 个；其余未进入本轮"
                    )
                if available_images > kept_images:
                    errors.append(
                        f"图片数 {available_images} 超过上限，"
                        f"已保留前 {kept_images} 个；其余未进入本轮"
                    )
            else:
                errors.append(
                    "文档管线降级：没有找到可读取的文档/图片清单"
                )

        if (
            not degraded
            and not write_mode
            and not document_mode
            and len(tasks) == 1
            and settings.orchestrator_add_explore_tests
        ):
            used = {task["id"] for task in tasks}
            extra_id = "explore-tests"
            while extra_id in used:
                extra_id += "-2"
            tasks.append(
                {
                    "id": extra_id,
                    "role": "explore",
                    "goal": "定位与用户请求相关的测试文件，并总结现有断言/期望行为",
                    "inputs": [],
                    "acceptance": ["列出相关测试文件与关键断言"],
                    "depends_on": [],
                }
            )

        revised_reason = ""
        if replan_mode:
            prefix = f"r{int(state.get('steering_replans', 1))}-"
            mapping = {
                str(task["id"]): f"{prefix}{task['id']}" for task in tasks
            }
            for task in tasks:
                old_id = str(task["id"])
                task["id"] = mapping[old_id]
                task["depends_on"] = [
                    mapping.get(str(dep), str(dep))
                    for dep in task.get("depends_on") or []
                ]
            cap = max(settings.orchestrator_max_tasks - len(completed_tasks), 1)
            tasks = completed_tasks + tasks[:cap]
            revised_reason = (
                f"用户要求重规划：保留 {len(completed_tasks)} 个已完成任务，"
                f"新增 {len(tasks) - len(completed_tasks)} 个"
            )

        if fix_mode and not replan_mode:
            # 修复轮复用同一条会话：给任务 id 加前缀，避免与上一轮的
            # worker_results 撞 id 导致 fan-in 只看到旧结果。
            prefix = f"fx{max(1, int(state.get('orchestrator_fix_rounds_used', 1)))}-"
            mapping = {
                str(task["id"]): f"{prefix}{task['id']}" for task in tasks
            }
            for task in tasks:
                old_id = str(task["id"])
                task["id"] = mapping[old_id]
                task["depends_on"] = [
                    mapping.get(str(dep), str(dep))
                    for dep in task.get("depends_on") or []
                ]

        # 完全重复的只读任务只保留一个；只删没有依赖方的任务，不改 depends_on。
        tasks, deduped_ids = dedupe_readonly_tasks(tasks)
        if deduped_ids:
            errors.append(
                "已去重重复只读任务：" + "、".join(deduped_ids[:5])
            )
        parallel = parallel_stats(tasks)

        # repo map / 知识产物 run id 随任务下发。
        repo_map = str(summary.get("repo_map") or "").strip()
        knowledge_run_id = str(
            state.get("knowledge_run_id")
            or summary.get("knowledge_run_id")
            or ""
        ).strip()
        if repo_map:
            for task in tasks:
                task["repo_map"] = repo_map
        if knowledge_run_id:
            for task in tasks:
                task["knowledge_run_id"] = knowledge_run_id

        keep_ids = {task["id"] for task in completed_tasks}
        status = {
            task["id"]: (
                existing_status.get(task["id"], "pending")
                if task["id"] in keep_ids
                else "pending"
            )
            for task in tasks
        }
        attempts = dict(state.get("task_attempts") or {})
        return {
            "tasks": tasks,
            "task_status": status,
            "task_attempts": attempts,
            "prefetched_tasks": [],
            "prefetched_degraded": False,
            "orchestrator_degraded": degraded,
            "orchestrator_round": 0,
            "orchestrator_parallel_width": int(parallel["width"]),
            "orchestrator_parallel_gain": float(parallel["gain"]),
            "change_contract": (
                contract if write_mode and contract_waves else {}
            ),
            "wave_gates": [],
            "transient_retries": len(retry_events),
            "orchestrator_summary": "；".join(errors),
            "steering_replan": False,
            "workflow_revised_reason": revised_reason,
        }

    return plan_tasks
