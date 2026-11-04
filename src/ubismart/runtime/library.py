"""`AgentRuntime` 的资料库管操（mixin）。

资料库是每用户跨工作区的：根目录不随 workspace 变化。A2 负责认证与配额
入口，A3 在这里完成解析、切块、落盘与审计，A4 的 `SandboxFs` 负责字节读写。
"""

from __future__ import annotations

from typing import Any

from ubismart.audit import AuditRecord
from ubismart.audit import models as audit_models
from ubismart.audit.logger import now_iso, truncate
from ubismart.library.embedding import make_library_embedder
from ubismart.library.models import LibraryError, LibraryNotFound
from ubismart.library.root import ensure_library_root
from ubismart.library.store import LibraryStore
from ubismart.sandbox.fs import SandboxFs


class LibraryMixin:
    """资料库方法集；只被 `AgentRuntime` 继承。"""

    def _library_store(self) -> LibraryStore:
        root = ensure_library_root(
            self.settings, self._sandbox, self.workspace
        )
        embedder, _embedding_error = make_library_embedder(self.settings)
        return LibraryStore(
            SandboxFs(self._sandbox, root),
            root,
            max_items=self.settings.library_max_items,
            max_item_bytes=self.settings.library_max_item_bytes,
            max_total_bytes=self.settings.library_max_total_bytes,
            chunk_chars=self.settings.library_chunk_chars,
            chunk_overlap=self.settings.library_chunk_overlap,
            search_mode=self.settings.library_search_mode,
            embedder=embedder,
            job_stale_s=self.settings.library_job_stale_s,
            mmr_lambda=self.settings.library_mmr_lambda,
            rerank_enabled=self.settings.library_rerank_enabled,
            rerank_boost=self.settings.library_rerank_boost,
            vector_backend=self.settings.library_vector_backend,
            vector_candidates=self.settings.library_vector_candidates,
        )

    def _audit_library(
        self,
        action: str,
        item_id: str,
        *,
        ok: bool,
        detail: str = "",
        added: int = 0,
    ) -> None:
        self._audit(
            AuditRecord(
                ts=now_iso(),
                kind=audit_models.LIBRARY,
                workspace=self.workspace,
                path=item_id,
                action=action,
                ok=ok,
                added=added,
                detail=truncate(detail),
            )
        )

    def _audit_library_job(self, job: dict[str, Any]) -> None:
        action = str(job.get("action", "reindex"))
        item_id = str(job.get("item_id") or job.get("job_id") or "")
        status = str(job.get("status", ""))
        detail = (
            f"job={job.get('job_id', '')} status={status}"
            f" stage={job.get('stage', '')}"
        )
        error = str(job.get("error", ""))
        if error:
            detail += f" error={error}"
        self._audit_library(
            action,
            item_id,
            ok=status == "ready",
            detail=detail,
        )

    def library_start_ingest(
        self,
        name: str,
        data: bytes,
        *,
        content_type: str = "",
    ) -> dict[str, Any]:
        try:
            started = self._library_store().start_ingest(
                name, data, content_type=content_type
            )
        except LibraryError as exc:
            self._audit_library("ingest", "", ok=False, detail=str(exc))
            raise
        item = started.get("item") or {}
        job = started.get("job")
        self._audit_library(
            "ingest",
            str(item.get("item_id", "")),
            ok=bool(started.get("duplicate")) or isinstance(job, dict),
            detail=(
                "duplicate"
                if started.get("duplicate")
                else f"queued job={job.get('job_id', '')}"
                if isinstance(job, dict)
                else "queued"
            ),
            added=int(item.get("size", 0) or 0),
        )
        return started

    def library_process_job(self, job_id: str) -> dict[str, Any]:
        job = self._library_store().process_job(job_id)
        self._audit_library_job(job)
        return job

    def library_fail_job(self, job_id: str, error: str) -> dict[str, Any]:
        job = self._library_store().fail_job(job_id, error)
        self._audit_library_job(job)
        return job

    def library_get_job(self, job_id: str) -> dict[str, Any]:
        job = self._library_store().get_job(job_id)
        if job is None:
            raise LibraryNotFound("资料任务不存在")
        return job

    def library_list_jobs(self, *, limit: int = 50) -> dict[str, Any]:
        return {"jobs": self._library_store().list_jobs(limit=limit)}

    def library_cancel_job(self, job_id: str) -> dict[str, Any]:
        job = self._library_store().cancel_job(job_id)
        self._audit_library(
            "cancel",
            str(job.get("item_id") or job_id),
            ok=True,
            detail=f"job={job_id}",
        )
        return job

    def library_start_reindex(self, item_id: str) -> dict[str, Any]:
        started = self._library_store().start_reindex(item_id)
        job = started.get("job") or {}
        self._audit_library(
            "reindex",
            item_id,
            ok=True,
            detail=f"queued job={job.get('job_id', '')}",
        )
        return started

    def library_start_reindex_all(self) -> dict[str, Any]:
        started = self._library_store().start_reindex_all()
        job = started.get("job") or {}
        self._audit_library(
            "reindex_all",
            str(job.get("job_id", "")),
            ok=True,
            detail=f"queued {job.get('total', 0)} items",
        )
        return started

    def library_reindex_all(self) -> dict[str, Any]:
        job = self._library_store().reindex_all()
        self._audit_library_job(job)
        return job

    def library_ingest(
        self,
        name: str,
        data: bytes,
        *,
        content_type: str = "",
    ) -> dict[str, Any]:
        try:
            item = self._library_store().ingest(
                name, data, content_type=content_type
            )
        except LibraryError as exc:
            self._audit_library(
                "ingest", "", ok=False, detail=str(exc)
            )
            raise
        self._audit_library(
            "ingest",
            str(item.get("item_id", "")),
            ok=str(item.get("status", "")) == "ready",
            detail=str(item.get("error", ""))
            or str(item.get("status", "")),
            added=int(item.get("size", 0) or 0),
        )
        return item

    def library_list(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str = "",
        tag: str = "",
        q: str = "",
    ) -> dict[str, Any]:
        store = self._library_store()
        page = store.list_page(
            limit=limit,
            offset=offset,
            status=status,
            tag=tag,
            query=q,
        )
        return {**page, "usage": store.stats()}

    def library_get(self, item_id: str) -> dict[str, Any]:
        item = self._library_store().get(item_id)
        if item is None:
            raise LibraryNotFound("资料条目不存在")
        return item

    def library_detail(
        self, item_id: str, *, chunk_limit: int = 20
    ) -> dict[str, Any]:
        detail = self._library_store().get_detail(
            item_id, chunk_limit=chunk_limit
        )
        if detail is None:
            raise LibraryNotFound("资料条目不存在")
        return detail

    def library_set_tags(
        self, item_id: str, tags: list[str]
    ) -> dict[str, Any]:
        item = self._library_store().set_tags(item_id, tags)
        self._audit_library(
            "tags",
            item_id,
            ok=True,
            detail=",".join(str(tag) for tag in item.get("tags") or []),
        )
        return item

    def library_delete(
        self, item_id: str, *, purge: bool = False
    ) -> dict[str, Any]:
        item = self._library_store().delete(item_id, purge=purge)
        self._audit_library(
            "delete",
            item_id,
            ok=True,
            detail="purge" if purge else "soft",
        )
        return item

    def library_reindex(self, item_id: str) -> dict[str, Any]:
        item = self._library_store().reindex(item_id)
        self._audit_library(
            "reindex",
            item_id,
            ok=str(item.get("status", "")) == "ready",
            detail=str(item.get("error", ""))
            or str(item.get("status", "")),
        )
        return item

    def library_search(
        self,
        query: str,
        *,
        top_k: int = 8,
        mode: str = "",
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        return self._library_store().search(
            query, top_k=top_k, mode=mode, tags=tags
        )
