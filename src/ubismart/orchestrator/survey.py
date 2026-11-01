"""编排前的仓库画像：顶层目录/文件、测试命令、git 隔离条件。"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from typing import Any

from langchain_core.runnables import RunnableConfig

from ubismart.codeintel.python_index import (
    build_index,
    build_repo_map,
    collect_python_sources,
)
from ubismart.execution.host_ops import HOST_ORIGIN, host_run
from ubismart.graph.state import AgentState
from ubismart.knowledge.architecture import (
    arch_brief,
    build_arch_index,
    build_change_contract,
    impact_analysis,
)
from ubismart.knowledge.index_cache import (
    IndexCache,
    build_index_incremental,
)
from ubismart.knowledge.lsp_calls import (
    make_lsp_client,
    resolve_dynamic_calls_with_lsp,
)
from ubismart.knowledge.polyglot import collect_arch_sources
from ubismart.knowledge.store import KnowledgeStore
from ubismart.orchestrator.artifacts import make_run_id
from ubismart.orchestrator.implement import stage_args
from ubismart.protocol.messages import last_human_text
from ubismart.sandbox.wsl_exec import WslSandbox
from ubismart.tools.testrun import detect_test_command


def compact_repo_summary(
    summary: dict[str, Any], *, limit: int = 20
) -> dict[str, Any]:
    """给决策/规划提示用的压缩画像。

    repo_map 可能有几千字符，但决策只需要目录/文件量级；完整 repo_map
    仍随任务下发给 worker。
    """
    return {
        "file_count": summary.get("file_count"),
        "test_file_count": summary.get("test_file_count"),
        "python_files": summary.get("python_files"),
        "top_dirs": list(summary.get("top_dirs") or [])[:limit],
        "top_files": list(summary.get("top_files") or [])[:limit],
        "document_count": len(summary.get("documents") or []),
        "image_count": len(summary.get("images") or []),
        "upload_count": len(summary.get("uploads") or []),
        "git": bool(summary.get("git")),
        "git_root": bool(summary.get("git_root")),
        "git_clean": bool(summary.get("git_clean")),
        "test_command": summary.get("test_command") or "",
        "arch_brief": str(summary.get("arch_brief") or "")[:3_000],
        "arch_impact": _compact_impact(summary.get("arch_impact")),
        "change_contract": _compact_contract(
            summary.get("change_contract")
        ),
    }


def _compact_contract(contract: Any) -> dict[str, Any]:
    if not isinstance(contract, dict):
        return {}
    return {
        "seed_files": list(contract.get("seed_files") or [])[:10],
        "seed_modules": list(contract.get("seed_modules") or [])[:10],
        "affected_files": list(contract.get("affected_files") or [])[:10],
        "affected_modules": list(contract.get("affected_modules") or [])[:10],
        "affected_tests": list(contract.get("affected_tests") or [])[:10],
        "interfaces": [
            {
                "name": str(item.get("name", "")),
                "file": str(item.get("file", "")),
                "callers": list(item.get("callers") or [])[:5],
            }
            for item in (contract.get("interfaces") or [])[:8]
            if isinstance(item, dict)
        ],
        "waves": [
            {
                "stage": int(item.get("stage", 0) or 0),
                "files": list(item.get("files") or [])[:10],
                "modules": list(item.get("modules") or [])[:10],
            }
            for item in (contract.get("waves") or [])[:6]
            if isinstance(item, dict)
        ],
        "fallback": bool(contract.get("fallback")),
        "reason": str(contract.get("reason") or "")[:300],
    }


def _compact_impact(impact: Any) -> dict[str, Any]:
    if not isinstance(impact, dict):
        return {}
    return {
        "seed_files": list(impact.get("seed_files") or [])[:10],
        "affected_files": list(impact.get("affected_files") or [])[:10],
        "affected_modules": list(impact.get("affected_modules") or [])[:10],
        "affected_tests": list(impact.get("affected_tests") or [])[:10],
        "fallback": bool(impact.get("fallback")),
        "reason": str(impact.get("reason") or "")[:300],
    }


def _collect_repo_sources(
    sandbox: WslSandbox, root: str, settings: Any, deep: bool
) -> tuple[dict[str, str], dict[str, Any]]:
    if deep:
        return collect_arch_sources(sandbox, root, settings)
    return collect_python_sources(sandbox, root), {}


def _python_sources_only(sources: dict[str, str]) -> dict[str, str]:
    return {
        path: text
        for path, text in sources.items()
        if str(path).lower().endswith((".py", ".pyi"))
    }


def _build_code_index(
    python_sources: dict[str, str],
    settings: Any,
    root: str,
    deep: bool,
) -> tuple[Any, dict[str, Any]]:
    if not python_sources:
        return None, {}
    if deep and bool(getattr(settings, "arch_incremental_index", True)):
        cache = IndexCache.from_settings(settings, root)
        return build_index_incremental(python_sources, cache)
    return build_index(python_sources), {}


def _resolve_dynamic_calls(
    settings: Any,
    root: str,
    sources: dict[str, str],
    arch: Any,
) -> None:
    if not (
        bool(getattr(settings, "arch_lsp_resolution_enabled", False))
        and arch.dynamic_calls
    ):
        return
    client = make_lsp_client(settings, root)
    if client is None:
        return
    try:
        edges = resolve_dynamic_calls_with_lsp(
            client,
            sources,
            arch.dynamic_calls,
            root=root,
            max_calls=int(
                getattr(settings, "arch_lsp_max_calls", 50) or 50
            ),
        )
    finally:
        client.close()
    if not edges:
        return
    arch.calls.extend(edges)
    resolved = {
        (str(edge["src_file"]), int(edge["line"])) for edge in edges
    }
    for item in arch.dynamic_calls:
        if (
            str(item.get("file", "")),
            int(item.get("line", 0) or 0),
        ) in resolved:
            item["resolved"] = True


def _save_arch_knowledge(
    knowledge_store: KnowledgeStore,
    knowledge_run_id: str,
    arch: Any,
    brief: str,
    contract: dict[str, Any],
) -> bool:
    saved = knowledge_store.save(
        knowledge_run_id, "arch", "arch", arch.as_dict()
    )
    knowledge_store.save(
        knowledge_run_id, "brief", "arch", {"text": brief}
    )
    knowledge_store.save(
        knowledge_run_id, "contract", "change", contract
    )
    return saved


def _populate_arch(
    result: dict[str, Any],
    *,
    sources: dict[str, str],
    index: Any,
    sample_stats: dict[str, Any],
    budget: int,
    settings: Any,
    root: str,
    request: str,
    knowledge_store: KnowledgeStore | None,
    knowledge_run_id: str,
) -> None:
    arch = build_arch_index(sources, index=index, sample=sample_stats)
    _resolve_dynamic_calls(settings, root, sources, arch)
    brief = arch_brief(arch, max_chars=budget)
    result["arch_brief"] = brief
    result["arch_sample"] = sample_stats
    max_modules = int(
        getattr(settings, "arch_impact_max_modules", 40) or 40
    )
    result["arch_impact"] = impact_analysis(
        arch, request, max_modules=max_modules
    )
    contract = build_change_contract(
        arch, request, max_modules=max_modules
    )
    result["change_contract"] = contract
    if knowledge_store is not None and knowledge_run_id:
        saved = _save_arch_knowledge(
            knowledge_store, knowledge_run_id, arch, brief, contract
        )
        if saved:
            result["arch_index_id"] = "arch"


def build_repo_map_summary(
    sandbox: WslSandbox,
    root: str,
    settings: Any = None,
    *,
    request: str = "",
    knowledge_store: KnowledgeStore | None = None,
    knowledge_run_id: str = "",
) -> dict[str, Any]:
    """昂贵的 repo_map / 架构索引构建；失败返回空 dict，不阻断编排。"""
    budget = int(getattr(settings, "repo_map_chars", 4_000) or 0)
    if budget <= 0:
        return {}
    try:
        deep = bool(
            getattr(settings, "deep_workflows_enabled", False)
            and getattr(settings, "arch_workflow_enabled", False)
        )
        sources, sample_stats = _collect_repo_sources(
            sandbox, root, settings, deep
        )
        if not sources:
            return {}
        python_sources = _python_sources_only(sources)
        index, index_stats = _build_code_index(
            python_sources, settings, root, deep
        )
        result: dict[str, Any] = {
            "python_files": len(python_sources),
            "repo_map": (
                build_repo_map(index, max_chars=budget)
                if index is not None
                else ""
            ),
        }
        if index_stats:
            result["index_stats"] = index_stats
        if deep:
            _populate_arch(
                result,
                sources=sources,
                index=index,
                sample_stats=sample_stats,
                budget=budget,
                settings=settings,
                root=root,
                request=request,
                knowledge_store=knowledge_store,
                knowledge_run_id=knowledge_run_id,
            )
        return result
    except Exception:  # noqa: BLE001 - 索引失败不影响 survey
        return {}


def _survey_script(thread: str) -> str:
    upload_scan = (
        f"find .agent/uploads/{thread} -maxdepth 2 -type f 2>/dev/null "
        "| sort | head -50"
        if thread
        else "true"
    )
    # 一次 wsl.exe 拿完顶层目录/文件与 git 状态；原先这里是 6 次进程启动，
    # 4 个 worker 并发时这部分开销会被放大。
    return f"""\
echo "AGENT_DIRS"
find . -maxdepth 1 -mindepth 1 -type d -not -name '.*' | sort | head -20
echo "AGENT_FILES"
find . -maxdepth 1 -mindepth 1 -type f -not -name '.*' | sort | head -20
echo "AGENT_DOCUMENTS"
find . -maxdepth 5 -type f \\( -iname '*.pdf' -o -iname '*.docx' \\
  -o -iname '*.xlsx' -o -iname '*.xls' -o -iname '*.pptx' \\) \\
  -not -path './.agent/*' -not -path './.git/*' -not -path './node_modules/*' \\
  | sort | head -50
echo "AGENT_IMAGES"
find . -maxdepth 5 -type f \\( -iname '*.png' -o -iname '*.jpg' \\
  -o -iname '*.jpeg' -o -iname '*.webp' -o -iname '*.gif' -o -iname '*.bmp' \\) \\
  -not -path './.agent/*' -not -path './.git/*' -not -path './node_modules/*' \\
  | sort | head -50
echo "AGENT_UPLOADS"
{upload_scan}
echo "AGENT_GIT"
if git rev-parse --is-inside-work-tree 2>/dev/null | grep -q true; then
  echo "INSIDE=1"
  echo "HEAD=$(git rev-parse HEAD 2>/dev/null)"
  top=$(git rev-parse --show-toplevel 2>/dev/null)
  physical=$(pwd -P)
  if [ "$top" = "$physical" ]; then echo "ROOT=1"; else echo "ROOT=0"; fi
  dirty=$(git status --porcelain -- . {stage_args()} 2>/dev/null)
  if [ -z "$dirty" ]; then echo "CLEAN=1"; else echo "CLEAN=0"; fi
else
  echo "INSIDE=0"; echo "ROOT=0"; echo "CLEAN=0"; echo "HEAD="
fi
"""


def _parse_survey_output(
    stdout: str,
    top_entries: dict[str, list[str]],
    documents: list[str],
    images: list[str],
    uploads: list[str],
    flags: dict[str, str],
) -> None:
    section = ""
    markers = {
        "AGENT_DIRS",
        "AGENT_FILES",
        "AGENT_DOCUMENTS",
        "AGENT_IMAGES",
        "AGENT_UPLOADS",
        "AGENT_GIT",
    }
    for line in stdout.splitlines():
        text = line.strip()
        if text in markers:
            section = text
            continue
        if section == "AGENT_DIRS" and text:
            top_entries["dirs"].append(text)
        elif section == "AGENT_FILES" and text:
            top_entries["files"].append(text)
        elif section == "AGENT_DOCUMENTS" and text:
            documents.append(text.removeprefix("./"))
        elif section == "AGENT_IMAGES" and text:
            images.append(text.removeprefix("./"))
        elif section == "AGENT_UPLOADS" and text:
            uploads.append(text.removeprefix("./"))
        elif section == "AGENT_GIT" and "=" in text:
            key, _, value = text.partition("=")
            flags[key] = value


def _detect_test_command(sandbox: WslSandbox, root: str) -> str:
    try:
        return detect_test_command(sandbox, root, allow_manifest=False)
    except Exception:  # noqa: BLE001
        return ""


def _setup_knowledge(
    state: AgentState,
    settings: Any,
    sandbox: WslSandbox,
    root: str,
    thread: str,
) -> tuple[str, KnowledgeStore | None]:
    deep = bool(getattr(settings, "deep_workflows_enabled", False))
    if not (
        deep and bool(getattr(settings, "knowledge_artifacts_enabled", False))
    ):
        return "", None
    knowledge_run_id = (
        str(state.get("knowledge_run_id") or "").strip()
        or make_run_id(thread or "run", time.strftime("%Y%m%dT%H%M%S"))
    )
    return knowledge_run_id, KnowledgeStore(sandbox, root)


def _maybe_build_repo_map(
    summary: dict[str, Any],
    state: AgentState,
    settings: Any,
    sandbox: WslSandbox,
    root: str,
    knowledge_run_id: str,
    knowledge_store: KnowledgeStore | None,
) -> None:
    # 默认立即构建（行为不变）；开启 lazy 后由 plan_tasks 在确认要编排时
    # 再付全仓 AST 扫描的成本。
    if bool(getattr(settings, "orchestrator_repo_map_lazy", False)):
        return
    request = str(state.get("request") or "").strip()
    if not request:
        request = last_human_text(state.get("messages") or [])
    summary.update(
        build_repo_map_summary(
            sandbox,
            root,
            settings,
            request=request,
            knowledge_store=knowledge_store,
            knowledge_run_id=knowledge_run_id,
        )
    )


def make_survey_node(
    sandbox: WslSandbox, root: str, settings: Any = None
) -> Callable[[AgentState, RunnableConfig], dict[str, Any]]:
    def survey(state: AgentState, config: RunnableConfig) -> dict[str, Any]:
        summary = dict(state.get("repo_profile") or {})
        top_entries: dict[str, list[str]] = {"dirs": [], "files": []}
        documents: list[str] = []
        images: list[str] = []
        uploads: list[str] = []
        thread = re.sub(
            r"[^A-Za-z0-9_-]", "", str(state.get("thread_id") or "")
        )[:64]
        script = _survey_script(thread)
        flags: dict[str, str] = {}
        try:
            result = host_run(
                sandbox,
                script,
                origin=HOST_ORIGIN,
                kind="repo_survey",
                cwd=root,
            )
            _parse_survey_output(
                result.stdout,
                top_entries,
                documents,
                images,
                uploads,
                flags,
            )
        except Exception:  # noqa: BLE001 - survey 失败不能拖垮 run
            pass

        test_command = _detect_test_command(sandbox, root)

        summary["top_dirs"] = top_entries["dirs"]
        summary["top_files"] = top_entries["files"]
        summary["documents"] = documents
        summary["images"] = images
        summary["uploads"] = uploads
        summary["test_command"] = test_command
        summary["git"] = flags.get("INSIDE") == "1"
        summary["git_root"] = flags.get("ROOT") == "1"
        summary["git_clean"] = flags.get("CLEAN") == "1"
        summary["git_head"] = flags.get("HEAD", "")

        knowledge_run_id, knowledge_store = _setup_knowledge(
            state, settings, sandbox, root, thread
        )
        if knowledge_run_id:
            summary["knowledge_run_id"] = knowledge_run_id
        _maybe_build_repo_map(
            summary,
            state,
            settings,
            sandbox,
            root,
            knowledge_run_id,
            knowledge_store,
        )
        return {
            "repo_summary": summary,
            "knowledge_run_id": knowledge_run_id,
            "change_contract": summary.get("change_contract") or {},
        }

    return survey
