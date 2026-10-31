"""知识产物检索工具（RFC-0009）。

只读、零副作用：模型只能传 `kind / artifact_id / selector / query`，
路径由宿主根据当前 run 拼装。没有运行上下文时直接返回说明，不做任何扫描。
"""

from __future__ import annotations

import json
from typing import Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from ubismart.config import Settings
from ubismart.knowledge.embedding import make_knowledge_embedder
from ubismart.knowledge.store import (
    KnowledgeStore,
    current_knowledge_run_id,
)
from ubismart.sandbox.wsl_exec import WslSandbox, resolve_workspace
from ubismart.tools.artifacts import CallArtifact, pack

KNOWLEDGE_READ = "knowledge_read"
KNOWLEDGE_SEARCH = "knowledge_search"


class KnowledgeReadInput(BaseModel):
    kind: Literal["doc", "claims", "arch", "brief", "contract"] = Field(
        default="doc", description="产物类型"
    )
    artifact_id: str = Field(
        default="",
        description="产物 id；留空时列出该类型下所有产物（arch/brief 默认 arch）",
    )
    selector: str = Field(
        default="", description="可选：按文本片段/anchor 过滤"
    )
    limit: int = Field(default=20, description="最多返回多少条")


class KnowledgeSearchInput(BaseModel):
    query: str = Field(description="要检索的问题/关键词")
    kind: Literal[
        "doc", "claims", "arch", "brief", "contract", "all"
    ] = Field(default="all", description="限定产物类型")
    top_k: int = Field(default=8, description="返回条数")
    mode: Literal["", "lexical", "vector", "hybrid"] = Field(
        default="", description="留空使用会话配置的检索模式"
    )


def _no_context(tool: str) -> str:
    return pack(
        "当前运行没有知识产物上下文（未开启 deep workflows / knowledge artifacts，"
        "或不在编排运行中）。",
        CallArtifact(tool=tool, ok=False, skipped=True),
    )


def build_knowledge_tools(
    settings: Settings, sandbox: WslSandbox
) -> list[BaseTool]:
    root = resolve_workspace(settings, sandbox)
    store = KnowledgeStore(sandbox, root)
    embedder, _embedding_error = make_knowledge_embedder(settings)
    default_mode = str(
        getattr(settings, "knowledge_search_mode", "lexical") or "lexical"
    )

    def read(
        kind: str = "doc",
        artifact_id: str = "",
        selector: str = "",
        limit: int = 20,
    ) -> str:
        run_id = current_knowledge_run_id()
        if not run_id:
            return _no_context(KNOWLEDGE_READ)
        try:
            payload = store.read(
                run_id,
                kind,
                artifact_id,
                selector=selector,
                limit=limit,
            )
            text = json.dumps(payload, ensure_ascii=False, default=str)
            return pack(
                text,
                CallArtifact(tool=KNOWLEDGE_READ, ok=True),
            )
        except Exception as exc:  # noqa: BLE001 - 检索失败回灌给模型
            return pack(
                f"知识产物读取失败：{type(exc).__name__}: {exc}",
                CallArtifact(tool=KNOWLEDGE_READ, ok=False),
            )

    def search(
        query: str,
        kind: str = "all",
        top_k: int = 8,
        mode: str = "",
    ) -> str:
        run_id = current_knowledge_run_id()
        if not run_id:
            return _no_context(KNOWLEDGE_SEARCH)
        try:
            results = store.search(
                run_id,
                query,
                top_k=top_k,
                kind=kind,
                embedder=embedder,
                mode=mode or default_mode,
            )
            return pack(
                store.render_search(run_id, results),
                CallArtifact(tool=KNOWLEDGE_SEARCH, ok=True),
            )
        except Exception as exc:  # noqa: BLE001
            return pack(
                f"知识产物检索失败：{type(exc).__name__}: {exc}",
                CallArtifact(tool=KNOWLEDGE_SEARCH, ok=False),
            )

    return [
        StructuredTool.from_function(
            func=read,
            name=KNOWLEDGE_READ,
            description=(
                "读取当前运行的知识产物（文档 chunk / claim 证据 / 架构索引）。"
                "只接受产物 id 与可选 selector，不能传文件路径。"
            ),
            args_schema=KnowledgeReadInput,
        ),
        StructuredTool.from_function(
            func=search,
            name=KNOWLEDGE_SEARCH,
            description=(
                "在当前运行的知识产物里检索最相关的 chunk/claim/架构条目"
                "（含 path#anchor）。默认词法；配置后可走 vector/hybrid。"
            ),
            args_schema=KnowledgeSearchInput,
        ),
    ]
