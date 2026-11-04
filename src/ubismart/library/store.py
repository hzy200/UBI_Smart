"""资料库的持久化、异步摄取与检索。

目录约定：

    <library_root>/
      catalog.json
      jobs.json
      items/<item_id>/
        source.bin
        chunks.jsonl
        outline.json
        vectors.json

`catalog.json` 是条目元数据事实源；`jobs.json` 记录摄取/重建任务的状态与
阶段进度。解析、切块与 embedding 都在 catalog 锁之外完成，只在创建条目、
更新阶段和最终落账时短暂持锁。
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import posixpath
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ubismart.library import metrics
from ubismart.library.ann import build_vector_index
from ubismart.library.chunking import chunk_document
from ubismart.library.embedding import (
    embed_texts,
    normalize_mode,
    provider_header,
)
from ubismart.library.models import (
    LIBRARY_SUFFIXES,
    STATUS_DELETED,
    STATUS_FAILED,
    STATUS_NEEDS_OCR,
    STATUS_READY,
    LibraryError,
    LibraryNotFound,
    LibraryQuotaExceeded,
    LibraryTooLarge,
    SupportedFormatError,
)
from ubismart.library.rerank import rerank_hits
from ubismart.library.search import (
    RRF_K,
    bm25_scores,
    mmr_select,
    rrf_scores,
    token_counts,
    tokens,
)
from ubismart.sandbox.fs_scripts import SandboxFsError

CATALOG_VERSION = 2
JOB_VERSION = 1
CATALOG_MAX_BYTES = 4_000_000
JOBS_MAX_BYTES = 4_000_000
CHUNKS_MAX_BYTES = 64_000_000
VECTOR_VERSION = 1
VECTOR_MAX_BYTES = 64_000_000
LOCK_STALE_SECONDS = 60

STATUS_QUEUED = "queued"
STATUS_PROCESSING = "processing"
STATUS_CANCELLED = "cancelled"

JOB_QUEUED = "queued"
JOB_PROCESSING = "processing"
JOB_READY = "ready"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"
JOB_TERMINAL = {JOB_READY, JOB_FAILED, JOB_CANCELLED}

_ITEM_ID_RE = re.compile(r"^lib_[A-Za-z0-9]{8,40}$")
_JOB_ID_RE = re.compile(r"^job_[A-Za-z0-9]{8,40}$")
_SEARCHABLE_STATUSES = {STATUS_READY}
_REINDEXABLE_STATUSES = {STATUS_READY, STATUS_FAILED, STATUS_NEEDS_OCR}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


def _safe_suffix(name: str) -> str:
    return Path(str(name or "")).suffix.lower()


def _is_missing_error(exc: Exception) -> bool:
    """区分“文件第一次创建前不存在”和真正的读取失败。"""
    if isinstance(exc, FileNotFoundError):
        return True
    # SandboxFs 用 SandboxFsError("文件不存在：...") 表达缺失。
    text = str(exc)
    return isinstance(exc, SandboxFsError) and (
        "文件不存在" in text or "目录不存在" in text
    )


def _safe_item_id(value: str) -> str:
    text = str(value or "").strip()
    if not _ITEM_ID_RE.fullmatch(text):
        raise LibraryNotFound("资料条目不存在")
    return text


def _safe_job_id(value: str) -> str:
    text = str(value or "").strip()
    if not _JOB_ID_RE.fullmatch(text):
        raise LibraryNotFound("资料任务不存在")
    return text


def _is_stale(updated_at: str, stale_seconds: float) -> bool:
    if stale_seconds <= 0:
        return False
    try:
        parsed = datetime.fromisoformat(str(updated_at))
    except (TypeError, ValueError):
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    age = (datetime.now(UTC) - parsed).total_seconds()
    return age > stale_seconds


class LibraryStore:
    def __init__(
        self,
        fs: Any,
        root: str,
        *,
        max_items: int = 100,
        max_item_bytes: int = 20_971_520,
        max_total_bytes: int = 268_435_456,
        chunk_chars: int = 4_000,
        chunk_overlap: int = 200,
        search_mode: str = "lexical",
        embedder: Any | None = None,
        job_stale_s: int = 3_600,
        mmr_lambda: float = 0.7,
        rerank_enabled: bool = True,
        rerank_boost: float = 0.25,
        vector_backend: str = "linear",
        vector_candidates: int = 50,
    ) -> None:
        self._fs = fs
        self.root = root
        self.max_items = max(1, int(max_items))
        self.max_item_bytes = max(1, int(max_item_bytes))
        self.max_total_bytes = max(1, int(max_total_bytes))
        self.chunk_chars = max(200, int(chunk_chars))
        self.chunk_overlap = max(0, int(chunk_overlap))
        self.search_mode = normalize_mode(search_mode)
        self._embedder = embedder
        self.job_stale_s = max(0, int(job_stale_s))
        self.mmr_lambda = max(0.0, min(1.0, float(mmr_lambda)))
        self.rerank_enabled = bool(rerank_enabled)
        self.rerank_boost = max(0.0, min(2.0, float(rerank_boost)))
        self.vector_backend = str(vector_backend or "linear").strip().lower()
        self.vector_candidates = max(1, int(vector_candidates))
        self._index_key: tuple[Any, ...] | None = None
        self._index: list[dict[str, Any]] = []
        self._ann_key: tuple[Any, ...] | None = None
        self._ann_index: Any = None

    # ------------------------------------------------------------------
    # 路径与锁
    # ------------------------------------------------------------------

    @property
    def catalog_path(self) -> str:
        return posixpath.join(self.root, "catalog.json")

    @property
    def jobs_path(self) -> str:
        return posixpath.join(self.root, "jobs.json")

    @property
    def items_dir(self) -> str:
        return posixpath.join(self.root, "items")

    def _item_dir(self, item_id: str) -> str:
        return posixpath.join(self.items_dir, item_id)

    def _source_path(self, item_id: str) -> str:
        return posixpath.join(self._item_dir(item_id), "source.bin")

    def _chunks_path(self, item_id: str) -> str:
        return posixpath.join(self._item_dir(item_id), "chunks.jsonl")

    def _outline_path(self, item_id: str) -> str:
        return posixpath.join(self._item_dir(item_id), "outline.json")

    def _vectors_path(self, item_id: str) -> str:
        return posixpath.join(self._item_dir(item_id), "vectors.json")

    def _lock_path(self) -> str:
        return posixpath.join(self.root, ".library.lock.d")

    def _clear_stale_lock(self) -> None:
        try:
            info = self._fs.stat(self._lock_path())
        except Exception:  # noqa: BLE001 - 锁探测失败按未过期处理
            return
        if not getattr(info, "exists", False):
            return
        age = time.time() - float(getattr(info, "mtime", 0.0) or 0.0)
        if age <= LOCK_STALE_SECONDS:
            return
        try:
            self._fs.rmdir(self._lock_path())
        except Exception:  # noqa: BLE001 - 清理失败就继续等
            pass

    @contextmanager
    def _locked(self, timeout_s: float = 5.0) -> Iterator[None]:
        # 根目录通常由 resolve/ensure 创建；这里只做兜底。
        try:
            self._fs.mkdir(self.root)
        except Exception:  # noqa: BLE001 - 根不存在时由后续锁操作暴露
            pass
        deadline = time.monotonic() + max(0.1, timeout_s)
        acquired = False
        while time.monotonic() < deadline:
            try:
                acquired = bool(
                    self._fs.mkdir_exclusive(self._lock_path())
                )
            except Exception:  # noqa: BLE001
                acquired = False
            if acquired:
                break
            self._clear_stale_lock()
            time.sleep(0.05)
        if not acquired:
            raise LibraryError("资料库正忙，请稍后重试")
        try:
            yield
        finally:
            try:
                self._fs.rmdir(self._lock_path())
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------
    # catalog
    # ------------------------------------------------------------------

    def _load_catalog(self) -> dict[str, Any]:
        try:
            raw = self._fs.read_text(
                self.catalog_path, max_bytes=CATALOG_MAX_BYTES
            )
        except Exception as exc:  # noqa: BLE001
            if _is_missing_error(exc):
                # 第一次使用：还没有 catalog 是合法的空库。
                return {"version": CATALOG_VERSION, "items": {}}
            # 读失败（WSL 抖动、权限、超过 4MB）不能当成空库：任何后续
            # 写入都会只带回新记录，把原有条目整体覆盖掉。
            raise LibraryError(
                "资料库目录读取失败，已停止写入以避免覆盖现有数据："
                f"{type(exc).__name__}: {exc}"
            ) from exc
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise LibraryError(
                "资料库目录格式损坏，已停止写入以避免覆盖现有数据"
            ) from exc
        if not isinstance(parsed, dict):
            raise LibraryError("资料库目录格式损坏：顶层不是对象")
        items = parsed.get("items")
        if not isinstance(items, dict):
            raise LibraryError("资料库目录格式损坏：items 不是对象")
        try:
            version = int(parsed.get("version", 1) or 1)
        except (TypeError, ValueError) as exc:
            raise LibraryError("资料库目录格式损坏：version 非法") from exc
        if version < 2:
            # v1 → v2：补齐 tags/job_id；下一次成功写入会持久化 v2。
            for item in items.values():
                if not isinstance(item, dict):
                    continue
                item.setdefault("tags", [])
                item.setdefault("job_id", "")
        return {
            "version": CATALOG_VERSION,
            "updated_at": str(parsed.get("updated_at", "") or ""),
            "items": items,
        }

    def _save_catalog(self, catalog: dict[str, Any]) -> None:
        payload = {
            "version": CATALOG_VERSION,
            "updated_at": _now_iso(),
            "items": catalog.get("items", {}),
        }
        try:
            self._fs.write_text(
                self.catalog_path,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except Exception as exc:  # noqa: BLE001
            raise LibraryError(f"资料库目录写入失败：{exc}") from exc

    @staticmethod
    def _active_items(catalog: dict[str, Any]) -> list[dict[str, Any]]:
        items = catalog.get("items") or {}
        return [
            value
            for value in items.values()
            if isinstance(value, dict)
            and str(value.get("status", "")) != STATUS_DELETED
        ]

    # ------------------------------------------------------------------
    # jobs
    # ------------------------------------------------------------------

    def _load_jobs(self) -> dict[str, Any]:
        try:
            raw = self._fs.read_text(
                self.jobs_path, max_bytes=JOBS_MAX_BYTES
            )
        except Exception as exc:  # noqa: BLE001
            if _is_missing_error(exc):
                return {"version": JOB_VERSION, "jobs": {}}
            raise LibraryError(
                "资料任务文件读取失败，已停止写入以避免覆盖现有任务："
                f"{type(exc).__name__}: {exc}"
            ) from exc
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise LibraryError(
                "资料任务文件格式损坏，已停止写入以避免覆盖现有任务"
            ) from exc
        if not isinstance(parsed, dict):
            raise LibraryError("资料任务文件格式损坏：顶层不是对象")
        jobs = parsed.get("jobs")
        if not isinstance(jobs, dict):
            raise LibraryError("资料任务文件格式损坏：jobs 不是对象")
        return {"version": JOB_VERSION, "jobs": jobs}

    def _save_jobs(self, jobs: dict[str, Any]) -> None:
        payload = {
            "version": JOB_VERSION,
            "updated_at": _now_iso(),
            "jobs": jobs.get("jobs", {}),
        }
        try:
            self._fs.write_text(
                self.jobs_path,
                json.dumps(payload, ensure_ascii=False, indent=2),
            )
        except Exception as exc:  # noqa: BLE001
            raise LibraryError(f"资料任务写入失败：{exc}") from exc
        metrics.set_pending(
            sum(
                1
                for job in (jobs.get("jobs") or {}).values()
                if isinstance(job, dict)
                and job.get("status") in {JOB_QUEUED, JOB_PROCESSING}
            )
        )

    def _update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        with self._locked():
            jobs = self._load_jobs()
            job = (jobs.get("jobs") or {}).get(job_id)
            if not isinstance(job, dict):
                raise LibraryNotFound("资料任务不存在")
            if job.get("status") in JOB_TERMINAL:
                # 终态是最终事实：迟到的进度/成功写回不能把它改回 ready，
                # 否则取消会被 worker 的收尾覆盖，取消与审计显示不一致。
                return dict(job)
            job.update(fields)
            job["updated_at"] = _now_iso()
            jobs["jobs"][job_id] = job
            self._save_jobs(jobs)
            return dict(job)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        try:
            safe = _safe_job_id(job_id)
        except LibraryNotFound:
            return None
        job = (self._load_jobs().get("jobs") or {}).get(safe)
        return dict(job) if isinstance(job, dict) else None

    def list_jobs(self, *, limit: int = 50) -> list[dict[str, Any]]:
        jobs = [
            dict(job)
            for job in (self._load_jobs().get("jobs") or {}).values()
            if isinstance(job, dict)
        ]
        jobs.sort(
            key=lambda job: str(job.get("updated_at", "")),
            reverse=True,
        )
        return jobs[: max(1, int(limit))]

    def recover_jobs(self, *, stale_seconds: int | None = None) -> int:
        """把崩溃/重启后遗留的 queued/processing 任务标成 interrupted。"""
        stale = self.job_stale_s if stale_seconds is None else stale_seconds
        if stale <= 0:
            return 0
        with self._locked():
            jobs = self._load_jobs()
            catalog = self._load_catalog()
            changed = 0
            now = _now_iso()
            for _job_id, job in (jobs.get("jobs") or {}).items():
                if not isinstance(job, dict):
                    continue
                if job.get("status") not in {JOB_QUEUED, JOB_PROCESSING}:
                    continue
                if not _is_stale(str(job.get("updated_at", "")), stale):
                    continue
                job["status"] = JOB_FAILED
                job["stage"] = "interrupted"
                job["progress"] = 100
                job["error"] = "任务中断（服务重启或超时）"
                job["updated_at"] = now
                item_id = str(job.get("item_id") or "")
                item = (catalog.get("items") or {}).get(item_id)
                if isinstance(item, dict) and item.get("status") in {
                    STATUS_QUEUED,
                    STATUS_PROCESSING,
                }:
                    item["status"] = STATUS_FAILED
                    item["error"] = job["error"]
                    item["updated_at"] = now
                changed += 1
            active_ids = {
                str(job_id)
                for job_id, job in (jobs.get("jobs") or {}).items()
                if isinstance(job, dict)
                and job.get("status") not in JOB_TERMINAL
            }
            for item in (catalog.get("items") or {}).values():
                if not isinstance(item, dict):
                    continue
                if item.get("status") not in {
                    STATUS_QUEUED,
                    STATUS_PROCESSING,
                }:
                    continue
                if str(item.get("job_id") or "") in active_ids:
                    continue
                item["status"] = STATUS_FAILED
                item["error"] = "任务中断（缺少运行中的 job）"
                item["updated_at"] = now
                changed += 1
            if changed:
                self._save_jobs(jobs)
                self._save_catalog(catalog)
                self._index_key = None
            to_purge = {
                str(item_id)
                for item_id, item in (catalog.get("items") or {}).items()
                if isinstance(item, dict)
                and item.get("status") == STATUS_DELETED
                and item.get("purge_requested")
            }
        # stale 任务不会有 worker 再收尾；锁外补做被推迟的物理删除。
        for item_id in to_purge:
            try:
                self._purge_if_requested(item_id)
            except Exception:  # noqa: BLE001 - 恢复流程不因清理失败中断
                pass
        return changed

    def _cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._locked():
            jobs = self._load_jobs()
            catalog = self._load_catalog()
            job = (jobs.get("jobs") or {}).get(job_id)
            if not isinstance(job, dict):
                raise LibraryNotFound("资料任务不存在")
            if job.get("status") in JOB_TERMINAL:
                return dict(job)
            job.update(
                {
                    "status": JOB_CANCELLED,
                    "stage": "cancelled",
                    "progress": 100,
                    "cancel_requested": True,
                    "error": "已取消",
                    "updated_at": _now_iso(),
                }
            )
            item_id = str(job.get("item_id") or "")
            item = (catalog.get("items") or {}).get(item_id)
            if isinstance(item, dict) and item.get("status") in {
                STATUS_QUEUED,
                STATUS_PROCESSING,
            }:
                item["status"] = STATUS_CANCELLED
                item["error"] = "已取消"
                item["updated_at"] = job["updated_at"]
            jobs["jobs"][job_id] = job
            self._save_jobs(jobs)
            self._save_catalog(catalog)
            self._index_key = None
            return dict(job)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        safe = _safe_job_id(job_id)
        return self._cancel_job(safe)

    def fail_job(self, job_id: str, error: str) -> dict[str, Any]:
        """任务在外层异常退出时显式落成 failed，避免永远停在 processing。"""
        safe = _safe_job_id(job_id)
        with self._locked():
            jobs = self._load_jobs()
            catalog = self._load_catalog()
            job = (jobs.get("jobs") or {}).get(safe)
            if not isinstance(job, dict):
                raise LibraryNotFound("资料任务不存在")
            if job.get("status") in JOB_TERMINAL:
                return dict(job)
            message = str(error or "任务失败")[:1_000]
            job.update(
                {
                    "status": JOB_FAILED,
                    "stage": "failed",
                    "progress": 100,
                    "error": message,
                    "updated_at": _now_iso(),
                }
            )
            item_id = str(job.get("item_id") or "")
            item = (catalog.get("items") or {}).get(item_id)
            if isinstance(item, dict) and item.get("status") in {
                STATUS_QUEUED,
                STATUS_PROCESSING,
            }:
                item["status"] = STATUS_FAILED
                item["error"] = message
                item["updated_at"] = job["updated_at"]
            jobs["jobs"][safe] = job
            self._save_jobs(jobs)
            self._save_catalog(catalog)
            self._index_key = None
            return dict(job)

    def _cancel_requested(self, job_id: str) -> bool:
        if not job_id:
            return False
        job = self.get_job(job_id)
        return bool(job and job.get("cancel_requested"))

    # ------------------------------------------------------------------
    # item 元数据
    # ------------------------------------------------------------------

    def _attach_job(
        self, item: dict[str, Any], jobs: dict[str, Any]
    ) -> dict[str, Any]:
        job_id = str(item.get("job_id") or "")
        job = (jobs.get("jobs") or {}).get(job_id) if job_id else None
        return {**item, "job": dict(job)} if isinstance(job, dict) else dict(item)

    def list_items(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        return self.list_page(
            include_deleted=include_deleted, limit=None
        )["items"]

    def list_page(
        self,
        *,
        include_deleted: bool = False,
        limit: int | None = 50,
        offset: int = 0,
        status: str = "",
        tag: str = "",
        query: str = "",
    ) -> dict[str, Any]:
        try:
            self.recover_jobs()
        except LibraryError:
            # 列表是读取路径；恢复失败不应让页面整体 503。
            pass
        catalog = self._load_catalog()
        jobs = self._load_jobs()
        wanted_status = str(status or "").strip()
        wanted_tag = str(tag or "").strip().lower()
        needle = str(query or "").strip().lower()
        rows: list[dict[str, Any]] = []
        for value in (catalog.get("items") or {}).values():
            if not isinstance(value, dict):
                continue
            item_status = str(value.get("status", ""))
            if wanted_status:
                if item_status != wanted_status:
                    continue
            elif not include_deleted and item_status == STATUS_DELETED:
                continue
            tags = [str(item) for item in (value.get("tags") or [])]
            if wanted_tag and wanted_tag not in {
                tag_item.lower() for tag_item in tags
            }:
                continue
            if needle:
                haystack = " ".join(
                    [
                        str(value.get("name", "")),
                        str(value.get("title", "")),
                        *tags,
                    ]
                ).lower()
                if needle not in haystack:
                    continue
            rows.append(self._attach_job(value, jobs))
        rows.sort(
            key=lambda item: str(item.get("updated_at", "")),
            reverse=True,
        )
        total = len(rows)
        start = max(0, int(offset))
        if limit is None:
            page = rows[start:]
            page_limit = total
        else:
            page_limit = max(1, min(int(limit), 200))
            page = rows[start : start + page_limit]
        return {
            "items": page,
            "total": total,
            "limit": page_limit,
            "offset": start,
        }

    def migrate(self) -> dict[str, Any]:
        """把 catalog 写成当前版本（v1 → v2 时补齐 tags/job_id）。"""
        with self._locked():
            catalog = self._load_catalog()
            self._save_catalog(catalog)
            return {
                "version": CATALOG_VERSION,
                "items": len(catalog.get("items") or {}),
            }

    def stats(self) -> dict[str, Any]:
        items = self.list_items()
        return {
            "items": len(items),
            "bytes": sum(int(item.get("size", 0) or 0) for item in items),
            "max_items": self.max_items,
            "max_item_bytes": self.max_item_bytes,
            "max_total_bytes": self.max_total_bytes,
        }

    def get(
        self, item_id: str, *, include_deleted: bool = True
    ) -> dict[str, Any] | None:
        try:
            safe = _safe_item_id(item_id)
        except LibraryNotFound:
            return None
        item = (self._load_catalog().get("items") or {}).get(safe)
        if not isinstance(item, dict):
            return None
        if not include_deleted and item.get("status") == STATUS_DELETED:
            return None
        return self._attach_job(item, self._load_jobs())

    def get_detail(
        self, item_id: str, *, chunk_limit: int = 20
    ) -> dict[str, Any] | None:
        item = self.get(item_id)
        if item is None:
            return None
        safe = str(item["item_id"])
        outline: list[dict[str, Any]] = []
        try:
            raw = self._fs.read_text(
                self._outline_path(safe), max_bytes=1_000_000
            )
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                outline = [
                    part for part in parsed if isinstance(part, dict)
                ][:100]
        except Exception:  # noqa: BLE001
            outline = []
        chunks: list[dict[str, Any]] = []
        total = 0
        try:
            raw = self._fs.read_text(
                self._chunks_path(safe), max_bytes=CHUNKS_MAX_BYTES
            )
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                total += 1
                if len(chunks) >= max(1, int(chunk_limit)):
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                if isinstance(chunk, dict):
                    chunks.append(
                        {
                            "anchor": str(chunk.get("anchor", "")),
                            "heading": str(chunk.get("heading", "")),
                            "kind": str(chunk.get("kind", "text")),
                            "text": str(chunk.get("text", ""))[:2_000],
                        }
                    )
        except Exception:  # noqa: BLE001
            pass
        return {
            "item": item,
            "outline": outline,
            "chunks": chunks,
            "chunk_total": total,
        }

    def set_tags(self, item_id: str, tags: list[str]) -> dict[str, Any]:
        safe = _safe_item_id(item_id)
        cleaned: list[str] = []
        for raw in tags or []:
            value = " ".join(str(raw).split())[:30]
            if not value or value in cleaned:
                continue
            cleaned.append(value)
            if len(cleaned) >= 20:
                break
        with self._locked():
            catalog = self._load_catalog()
            item = (catalog.get("items") or {}).get(safe)
            if not isinstance(item, dict) or item.get("status") == STATUS_DELETED:
                raise LibraryNotFound("资料条目不存在")
            item["tags"] = cleaned
            item["updated_at"] = _now_iso()
            self._save_catalog(catalog)
            return dict(item)

    # ------------------------------------------------------------------
    # 任务创建
    # ------------------------------------------------------------------

    def start_ingest(
        self,
        name: str,
        data: bytes,
        *,
        content_type: str = "",
    ) -> dict[str, Any]:
        suffix = _safe_suffix(name)
        if suffix not in LIBRARY_SUFFIXES:
            raise SupportedFormatError(
                f"不支持 {suffix or '无扩展名'}；"
                "仅支持 pdf/docx/md/txt/xlsx/xlsm/pptx/html/epub/csv/tsv"
            )
        if len(data) > self.max_item_bytes:
            raise LibraryTooLarge(
                f"文件超过单条上限 {self.max_item_bytes} 字节"
            )
        sha256 = hashlib.sha256(data).hexdigest()

        with self._locked():
            catalog = self._load_catalog()
            items = catalog.get("items") or {}
            for existing in items.values():
                if (
                    isinstance(existing, dict)
                    and existing.get("status") != STATUS_DELETED
                    and existing.get("sha256") == sha256
                ):
                    return {
                        "item": dict(existing),
                        "job": None,
                        "duplicate": True,
                    }

            active = self._active_items(catalog)
            if len(active) >= self.max_items:
                raise LibraryQuotaExceeded(
                    f"资料库条目已达上限 {self.max_items}"
                )
            used = sum(int(item.get("size", 0) or 0) for item in active)
            if used + len(data) > self.max_total_bytes:
                raise LibraryTooLarge(
                    f"资料库总容量超过上限 {self.max_total_bytes} 字节"
                )

            item_id = f"lib_{secrets.token_hex(8)}"
            job_id = f"job_{secrets.token_hex(8)}"
            now = _now_iso()
            mime = (
                str(content_type or "").strip()
                or mimetypes.guess_type(name)[0]
                or "application/octet-stream"
            )
            item: dict[str, Any] = {
                "item_id": item_id,
                "name": Path(str(name or "upload.bin")).name[:180],
                "suffix": suffix,
                "mime": mime,
                "size": len(data),
                "sha256": sha256,
                "status": STATUS_QUEUED,
                "chunk_count": 0,
                "title": Path(str(name or "")).stem[:200],
                "parser_version": 1,
                "created_at": now,
                "updated_at": now,
                "error": "",
                "warnings": [],
                "tags": [],
                "job_id": job_id,
            }
            # 先确认 jobs 可读，再写源文件：jobs 损坏时不能留下孤儿
            # source.bin，也不能继续覆盖任何元数据。
            item_jobs = self._load_jobs()
            try:
                self._fs.write_bytes(self._source_path(item_id), data)
            except Exception as exc:  # noqa: BLE001
                raise LibraryError(
                    f"资料源文件写入失败：{type(exc).__name__}: {exc}"
                ) from exc
            job = {
                "job_id": job_id,
                "action": "ingest",
                "item_id": item_id,
                "item_ids": [],
                "status": JOB_QUEUED,
                "stage": "queued",
                "progress": 0,
                "done": 0,
                "total": 1,
                "error": "",
                "errors": {},
                "cancel_requested": False,
                "created_at": now,
                "updated_at": now,
            }
            items[item_id] = item
            catalog["items"] = items
            item_jobs["jobs"][job_id] = job
            self._save_catalog(catalog)
            try:
                self._save_jobs(item_jobs)
            except Exception:
                item["status"] = STATUS_FAILED
                item["error"] = "任务创建失败"
                self._save_catalog(catalog)
                raise
            self._index_key = None
            return {"item": dict(item), "job": dict(job), "duplicate": False}

    def start_reindex(self, item_id: str) -> dict[str, Any]:
        safe = _safe_item_id(item_id)
        with self._locked():
            catalog = self._load_catalog()
            items = catalog.get("items") or {}
            item = items.get(safe)
            if not isinstance(item, dict) or item.get("status") == STATUS_DELETED:
                raise LibraryNotFound("资料条目不存在")
            jobs = self._load_jobs()
            current = (jobs.get("jobs") or {}).get(
                str(item.get("job_id") or "")
            )
            if (
                isinstance(current, dict)
                and current.get("status") not in JOB_TERMINAL
            ):
                raise LibraryError("该条目已有运行中的任务")
            job_id = f"job_{secrets.token_hex(8)}"
            now = _now_iso()
            item["job_id"] = job_id
            item["updated_at"] = now
            items[safe] = item
            catalog["items"] = items
            job = {
                "job_id": job_id,
                "action": "reindex",
                "item_id": safe,
                "item_ids": [],
                "status": JOB_QUEUED,
                "stage": "queued",
                "progress": 0,
                "done": 0,
                "total": 1,
                "error": "",
                "errors": {},
                "cancel_requested": False,
                "created_at": now,
                "updated_at": now,
            }
            jobs["jobs"][job_id] = job
            self._save_catalog(catalog)
            self._save_jobs(jobs)
            self._index_key = None
            return {"item": dict(item), "job": dict(job)}

    def start_reindex_all(self) -> dict[str, Any]:
        with self._locked():
            catalog = self._load_catalog()
            jobs = self._load_jobs()
            all_jobs = jobs.get("jobs") or {}
            active_job_ids = {
                str(job_id)
                for job_id, job in all_jobs.items()
                if isinstance(job, dict)
                and job.get("status") not in JOB_TERMINAL
            }
            targets = [
                str(item.get("item_id", ""))
                for item in self._active_items(catalog)
                if item.get("status") in _REINDEXABLE_STATUSES
                and str(item.get("job_id") or "") not in active_job_ids
            ]
            if not targets:
                raise LibraryError("没有可重建的资料条目")
            job_id = f"job_{secrets.token_hex(8)}"
            now = _now_iso()
            job = {
                "job_id": job_id,
                "action": "reindex_all",
                "item_id": "",
                "item_ids": targets,
                "status": JOB_QUEUED,
                "stage": "queued",
                "progress": 0,
                "done": 0,
                "total": len(targets),
                "error": "",
                "errors": {},
                "cancel_requested": False,
                "created_at": now,
                "updated_at": now,
            }
            jobs["jobs"][job_id] = job
            for item in self._active_items(catalog):
                if str(item.get("item_id", "")) in targets:
                    item["job_id"] = job_id
            self._save_catalog(catalog)
            self._save_jobs(jobs)
            return {"job": dict(job), "count": len(targets)}

    # ------------------------------------------------------------------
    # 任务执行（解析/embedding 在 catalog 锁之外）
    # ------------------------------------------------------------------

    def process_job(self, job_id: str) -> dict[str, Any]:
        started = time.perf_counter()
        metrics.start_job()
        try:
            job = self._process_job_inner(job_id)
        except Exception:
            current = self.get_job(job_id) or {}
            metrics.record_job(
                str(current.get("action", "unknown")),
                "failed",
                (time.perf_counter() - started) * 1_000,
            )
            raise
        finally:
            metrics.finish_job()
            # 删除请求可能是在任务运行中提交的；worker 已退出（成功/失败/
            # 取消）后才安全地做物理清除。
            try:
                current = self.get_job(job_id) or {}
                self._purge_if_requested(
                    str(current.get("item_id") or "")
                )
            except Exception:  # noqa: BLE001 - 清理失败保留标记待重试
                pass
        metrics.record_job(
            str(job.get("action", "unknown")),
            str(job.get("status", "unknown")),
            (time.perf_counter() - started) * 1_000,
        )
        return job

    def _process_job_inner(self, job_id: str) -> dict[str, Any]:
        job = self.get_job(job_id)
        if job is None:
            raise LibraryNotFound("资料任务不存在")
        if job.get("status") in JOB_TERMINAL:
            return job
        if job.get("cancel_requested"):
            return self._cancel_job(job_id)

        action = str(job.get("action", ""))
        if action == "ingest":
            return self._run_ingest_job(job)
        if action == "reindex":
            return self._run_reindex_job(job)
        if action == "reindex_all":
            return self._run_reindex_all_job(job)
        return self._update_job(
            job_id,
            status=JOB_FAILED,
            stage="failed",
            progress=100,
            error=f"未知任务类型：{action}",
        )

    def _run_ingest_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["job_id"])
        item_id = str(job.get("item_id", ""))
        self._update_job(
            job_id, status=JOB_PROCESSING, stage="parsing", progress=10
        )
        self._set_item_status(item_id, STATUS_PROCESSING)
        status, error = self._process_item(item_id, job_id)
        if status == STATUS_CANCELLED:
            return self._cancel_job(job_id)
        return self._finish_job(job_id, status, error)

    def _run_reindex_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["job_id"])
        item_id = str(job.get("item_id", ""))
        self._update_job(
            job_id, status=JOB_PROCESSING, stage="parsing", progress=10
        )
        status, error = self._process_item(
            item_id, job_id, preserve_on_failure=True
        )
        if status == STATUS_CANCELLED:
            return self._cancel_job(job_id)
        return self._finish_job(job_id, status, error)

    def _run_reindex_all_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job["job_id"])
        item_ids = [str(value) for value in job.get("item_ids") or []]
        total = max(1, len(item_ids))
        errors: dict[str, str] = {}
        done = 0
        self._update_job(
            job_id, status=JOB_PROCESSING, stage="reindexing", progress=0
        )
        for item_id in item_ids:
            if self._cancel_requested(job_id):
                return self._cancel_job(job_id)
            status, error = self._process_item(
                item_id,
                job_id,
                update_job=False,
                preserve_on_failure=True,
            )
            if status == STATUS_CANCELLED:
                return self._cancel_job(job_id)
            if status not in {STATUS_READY, STATUS_NEEDS_OCR}:
                errors[item_id] = error or status
            done += 1
            self._update_job(
                job_id,
                stage="reindexing",
                progress=min(99, int(done * 100 / total)),
                done=done,
                total=total,
                errors=errors,
            )
        if errors:
            return self._update_job(
                job_id,
                status=JOB_FAILED,
                stage="done",
                progress=100,
                done=done,
                total=total,
                errors=errors,
                error=f"{len(errors)} 个条目重建失败",
            )
        return self._update_job(
            job_id,
            status=JOB_READY,
            stage="done",
            progress=100,
            done=done,
            total=total,
            errors={},
            error="",
        )

    def _finish_job(
        self, job_id: str, status: str, error: str
    ) -> dict[str, Any]:
        # needs_ocr 是任务的正常终态；条目自身状态会告诉用户不可检索。
        job_ok = status in {STATUS_READY, STATUS_NEEDS_OCR}
        return self._update_job(
            job_id,
            status=JOB_READY if job_ok else JOB_FAILED,
            stage="done",
            progress=100,
            error="" if status == STATUS_READY else (error or status),
        )

    def _set_item_status(
        self, item_id: str, status: str, **fields: Any
    ) -> None:
        if not item_id:
            return
        with self._locked():
            catalog = self._load_catalog()
            item = (catalog.get("items") or {}).get(item_id)
            if not isinstance(item, dict):
                return
            if item.get("status") in {STATUS_DELETED, STATUS_CANCELLED}:
                return
            item["status"] = status
            item.update(fields)
            item["updated_at"] = _now_iso()
            self._save_catalog(catalog)

    def _process_item(
        self,
        item_id: str,
        job_id: str,
        *,
        update_job: bool = True,
        preserve_on_failure: bool = False,
    ) -> tuple[str, str]:
        """解析/切块/embedding 与最终落账；锁只覆盖短写。

        `preserve_on_failure=True` 用于 reindex：失败时保留旧产物与 ready
        状态，只让 job 失败；新产物先写 staging，chunks 最后提交。
        """
        if not item_id:
            return STATUS_FAILED, "条目不存在"
        try:
            data = self._fs.read_bytes(
                self._source_path(item_id), max_bytes=self.max_item_bytes
            )
            if update_job:
                self._update_job(job_id, stage="parsing", progress=20)
            if job_id and self._cancel_requested(job_id):
                return STATUS_CANCELLED, "已取消"

            # 延迟导入：tools 包顶层会导入 registry，registry 又导入本模块。
            from ubismart.tools.documents.extract import parse_document
            from ubismart.tools.documents.models import DocumentLimits

            model = parse_document(
                self._item_name(item_id),
                data,
                limits=DocumentLimits(max_bytes=self.max_item_bytes),
            )
            chunks, outline = chunk_document(
                model,
                item_id=item_id,
                max_chars=self.chunk_chars,
                overlap=self.chunk_overlap,
            )
            if update_job:
                self._update_job(job_id, stage="chunking", progress=45)
            if job_id and self._cancel_requested(job_id):
                return STATUS_CANCELLED, "已取消"

            body = "\n".join(
                json.dumps(chunk.to_dict(), ensure_ascii=False)
                for chunk in chunks
            )
            outline_text = json.dumps(outline, ensure_ascii=False)

            vectors: dict[str, list[float]] | None = None
            if chunks and self._uses_vectors():
                if update_job:
                    self._update_job(
                        job_id, stage="embedding", progress=65
                    )
                embedded = embed_texts(
                    self._embedder, [chunk.text for chunk in chunks]
                )
                if embedded is None and preserve_on_failure:
                    # 重建时 embedding 失败：不要用“无向量”覆盖旧索引。
                    raise LibraryError("embedding 失败，已保留旧索引")
                if embedded:
                    vectors = {
                        chunk.chunk_id: vector
                        for chunk, vector in zip(
                            chunks, embedded, strict=True
                        )
                    }
            if job_id and self._cancel_requested(job_id):
                return STATUS_CANCELLED, "已取消"

            if preserve_on_failure and not chunks:
                # 重建得到空内容（例如 PDF 丢了文本层）不能覆盖旧索引。
                raise LibraryError("没有可索引的文本内容，已保留旧索引")

            if update_job:
                self._update_job(job_id, stage="writing", progress=85)
            self._commit_artifacts(
                item_id,
                job_id or secrets.token_hex(6),
                chunks_text=body,
                outline_text=outline_text,
                vectors_text=(
                    self._vectors_text(vectors)
                    if vectors is not None
                    else None
                ),
                remove_vectors=self._uses_vectors(),
            )

            if job_id and self._cancel_requested(job_id):
                return STATUS_CANCELLED, "已取消"

            if chunks:
                status = STATUS_READY
                error = ""
            elif model.needs_ocr:
                status = STATUS_NEEDS_OCR
                error = "PDF 没有文本层，暂不支持 OCR"
            else:
                status = STATUS_FAILED
                error = "没有可索引的文本内容"

            final = self._finalize_item(
                item_id,
                status=status,
                chunk_count=len(chunks),
                title=str(model.title or "")[:200],
                warnings=[str(w)[:500] for w in model.warnings[:20]],
                error=error,
            )
            if final is None or final.get("status") in {
                STATUS_DELETED,
                STATUS_CANCELLED,
            }:
                return STATUS_CANCELLED, "已删除或取消"
            return status, error
        except Exception as exc:  # noqa: BLE001 - 失败要落成可重建状态
            error = f"{type(exc).__name__}: {exc}"[:1_000]
            if not preserve_on_failure:
                self._finalize_item(
                    item_id,
                    status=STATUS_FAILED,
                    chunk_count=0,
                    error=error,
                )
            return STATUS_FAILED, error

    def _item_name(self, item_id: str) -> str:
        item = (self._load_catalog().get("items") or {}).get(item_id)
        return str(item.get("name", "")) if isinstance(item, dict) else ""

    def _finalize_item(
        self,
        item_id: str,
        *,
        status: str,
        chunk_count: int,
        title: str = "",
        warnings: list[str] | None = None,
        error: str = "",
    ) -> dict[str, Any] | None:
        with self._locked():
            catalog = self._load_catalog()
            item = (catalog.get("items") or {}).get(item_id)
            if not isinstance(item, dict):
                return None
            if item.get("status") in {STATUS_DELETED, STATUS_CANCELLED}:
                return dict(item)
            item["status"] = status
            item["chunk_count"] = chunk_count
            if title:
                item["title"] = title[:200]
            if warnings is not None:
                item["warnings"] = warnings
            item["error"] = error
            item["updated_at"] = _now_iso()
            self._save_catalog(catalog)
            self._index_key = None
            return dict(item)

    # ------------------------------------------------------------------
    # 同步兼容入口（测试/脚本）
    # ------------------------------------------------------------------

    def ingest(
        self,
        name: str,
        data: bytes,
        *,
        content_type: str = "",
    ) -> dict[str, Any]:
        started = self.start_ingest(
            name, data, content_type=content_type
        )
        if started["duplicate"] or not started.get("job"):
            return {**started["item"], "duplicate": True}
        job = self.process_job(str(started["job"]["job_id"]))
        item = self.get(str(job.get("item_id") or started["item"]["item_id"]))
        return {**(item or started["item"]), "duplicate": False}

    def reindex(self, item_id: str) -> dict[str, Any]:
        started = self.start_reindex(item_id)
        job = self.process_job(str(started["job"]["job_id"]))
        item = self.get(item_id)
        return {**(item or started["item"]), "duplicate": False, "job": job}

    def reindex_all(self) -> dict[str, Any]:
        started = self.start_reindex_all()
        return self.process_job(str(started["job"]["job_id"]))

    # ------------------------------------------------------------------
    # 向量
    # ------------------------------------------------------------------

    def _uses_vectors(self) -> bool:
        return self._embedder is not None and self.search_mode in {
            "vector",
            "hybrid",
        }

    def _remove_vectors(self, item_id: str) -> None:
        try:
            self._fs.remove(self._vectors_path(item_id), known_file=True)
        except Exception:  # noqa: BLE001 - 不存在或不可删都不影响文本索引
            pass

    def _vectors_text(self, vectors: dict[str, list[float]]) -> str:
        header = provider_header(self._embedder)
        return json.dumps(
            {"version": VECTOR_VERSION, **header, "chunks": vectors},
            ensure_ascii=False,
        )

    def _cleanup_stage(self, stage_dir: str) -> None:
        """尽力清掉 staging 目录；失败不影响已提交产物。"""
        try:
            entries = self._fs.list_dir(
                stage_dir, max_depth=2, limit=100
            )
        except Exception:  # noqa: BLE001
            entries = []
        for kind, rel in entries:
            target = posixpath.join(stage_dir, rel)
            if kind == "f":
                try:
                    self._fs.remove(target, known_file=True)
                except Exception:  # noqa: BLE001
                    pass
        try:
            self._fs.rmdir(stage_dir)
        except Exception:  # noqa: BLE001
            pass

    def _commit_artifacts(
        self,
        item_id: str,
        token: str,
        *,
        chunks_text: str,
        outline_text: str,
        vectors_text: str | None,
        remove_vectors: bool,
    ) -> None:
        """把派生产物先写 staging，再逐个换上台。

        提交顺序固定为 vectors → outline → chunks：chunks.jsonl 最后换，
        它被视为提交点。任何 rename 失败都在 chunks 之前或等于 chunks，
        旧 chunks（以及旧索引）仍可用；staging 失败一个字节都不动旧产物。
        """
        item_dir = self._item_dir(item_id)
        stage_dir = posixpath.join(item_dir, f".stage-{token}")
        staged: dict[str, str] = {
            "outline.json": outline_text,
            "chunks.jsonl": chunks_text,
        }
        if vectors_text is not None:
            staged["vectors.json"] = vectors_text
        try:
            for name, content in staged.items():
                self._fs.write_text(
                    posixpath.join(stage_dir, name), content
                )
            for name in ("vectors.json", "outline.json", "chunks.jsonl"):
                if name not in staged:
                    continue
                self._fs.rename(
                    posixpath.join(stage_dir, name),
                    posixpath.join(item_dir, name),
                )
            if vectors_text is None and remove_vectors:
                # chunks 已提交，旧向量不再匹配新 chunk_id；清掉退化为词法。
                self._remove_vectors(item_id)
        finally:
            self._cleanup_stage(stage_dir)

    def _load_vectors(self, item_id: str) -> dict[str, list[float]]:
        if self._embedder is None:
            return {}
        try:
            raw = self._fs.read_text(
                self._vectors_path(item_id), max_bytes=VECTOR_MAX_BYTES
            )
        except Exception:  # noqa: BLE001
            return {}
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        header = provider_header(self._embedder)
        if (
            int(parsed.get("version", 0) or 0) != VECTOR_VERSION
            or str(parsed.get("provider", "")) != header["provider"]
            or str(parsed.get("model", "")) != header["model"]
            or int(parsed.get("dimension", 0) or 0) != header["dimension"]
        ):
            return {}
        chunks = parsed.get("chunks")
        if not isinstance(chunks, dict):
            return {}
        dim = int(header["dimension"] or 0)
        vectors: dict[str, list[float]] = {}
        for key, value in chunks.items():
            vector = value
            if (
                isinstance(vector, list)
                and len(vector) == dim
                and all(isinstance(item, (int, float)) for item in vector)
            ):
                vectors[str(key)] = [float(item) for item in vector]
        return vectors

    # ------------------------------------------------------------------
    # 删除
    # ------------------------------------------------------------------

    def delete(self, item_id: str, *, purge: bool = False) -> dict[str, Any]:
        safe = _safe_item_id(item_id)
        with self._locked():
            catalog = self._load_catalog()
            items = catalog.get("items") or {}
            item = items.get(safe)
            if not isinstance(item, dict):
                raise LibraryNotFound("资料条目不存在")
            already_deleted = item.get("status") == STATUS_DELETED
            if already_deleted and not purge:
                raise LibraryNotFound("资料条目不存在")
            jobs = self._load_jobs()
            job_id = str(item.get("job_id") or "")
            job = (jobs.get("jobs") or {}).get(job_id)
            active_job = (
                isinstance(job, dict)
                and job.get("status") not in JOB_TERMINAL
            )
            if active_job:
                job["cancel_requested"] = True
                job["updated_at"] = _now_iso()
                jobs["jobs"][job_id] = job
            if already_deleted:
                # 文档约定：先软删，带 purge=true 再删除时做物理清除。
                item["purge_requested"] = True
                item["updated_at"] = _now_iso()
                items[safe] = item
                catalog["items"] = items
                self._save_catalog(catalog)
                self._save_jobs(jobs)
                self._index_key = None
                if active_job:
                    return dict(item)
                if not self._purge_item(safe):
                    raise LibraryError(
                        "物理删除失败，删除标记已保留；请稍后重试"
                    )
                items.pop(safe, None)
                catalog["items"] = items
                self._save_catalog(catalog)
                self._index_key = None
                return dict(item)

            item["status"] = STATUS_DELETED
            item["updated_at"] = _now_iso()
            if purge:
                # 先持久化清除意图：有活跃任务时必须等 worker 收尾后再删，
                # 否则会和仍在写 chunks/vectors 的任务竞态。
                item["purge_requested"] = True
            items[safe] = item
            catalog["items"] = items
            self._save_catalog(catalog)
            self._save_jobs(jobs)
            self._index_key = None
            if not purge or active_job:
                return dict(item)
            if not self._purge_item(safe):
                raise LibraryError(
                    "物理删除失败，删除标记已保留；请稍后重试"
                )
            items.pop(safe, None)
            catalog["items"] = items
            self._save_catalog(catalog)
            self._index_key = None
            return dict(item)

    def _purge_if_requested(self, item_id: str) -> bool:
        """任务收尾后执行被推迟的物理删除。

        只在 worker 已退出（正常/失败/取消）或 stale 恢复时调用，避免与
        仍在写文件的线程竞态。
        """
        if not item_id:
            return False
        with self._locked():
            catalog = self._load_catalog()
            items = catalog.get("items") or {}
            item = items.get(item_id)
            if not isinstance(item, dict):
                return False
            if (
                item.get("status") != STATUS_DELETED
                or not item.get("purge_requested")
            ):
                return False
            if not self._purge_item(item_id):
                return False
            items.pop(item_id, None)
            catalog["items"] = items
            self._save_catalog(catalog)
            self._index_key = None
            return True

    def _purge_item(self, item_id: str) -> bool:
        item_dir = self._item_dir(item_id)
        try:
            entries = self._fs.list_dir(
                item_dir, max_depth=3, limit=2_000
            )
        except Exception as exc:  # noqa: BLE001
            if _is_missing_error(exc):
                return True
            try:
                info = self._fs.stat(item_dir)
            except Exception:  # noqa: BLE001
                return False
            return not bool(getattr(info, "exists", False))
        dirs: list[str] = []
        for kind, rel in entries:
            target = posixpath.join(item_dir, rel)
            if kind == "f":
                try:
                    self._fs.remove(target, known_file=True)
                except Exception:  # noqa: BLE001
                    pass
            elif kind == "d":
                dirs.append(target)
        for target in sorted(dirs, key=lambda value: value.count("/"), reverse=True):
            try:
                self._fs.rmdir(target)
            except Exception:  # noqa: BLE001
                pass
        try:
            self._fs.rmdir(item_dir)
        except Exception:  # noqa: BLE001
            pass
        try:
            return not bool(self._fs.stat(item_dir).exists)
        except Exception:  # noqa: BLE001
            return False

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def _load_chunks(self, with_vectors: bool = False) -> list[dict[str, Any]]:
        catalog = self._load_catalog()
        header = (
            provider_header(self._embedder)
            if with_vectors and self._embedder is not None
            else {}
        )
        key = (
            str(catalog.get("updated_at", "")),
            len(self._active_items(catalog)),
            bool(with_vectors),
            str(header),
        )
        if self._index_key == key:
            return self._index
        entries: list[dict[str, Any]] = []
        for item in self._active_items(catalog):
            if item.get("status") not in _SEARCHABLE_STATUSES:
                continue
            item_id = str(item.get("item_id", ""))
            try:
                raw = self._fs.read_text(
                    self._chunks_path(item_id), max_bytes=CHUNKS_MAX_BYTES
                )
            except Exception:  # noqa: BLE001
                continue
            vectors = self._load_vectors(item_id) if with_vectors else {}
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                chunk_id = str(chunk.get("chunk_id", ""))
                entries.append(
                    {
                        "item_id": item_id,
                        "name": str(item.get("name", "")),
                        "anchor": str(chunk.get("anchor", "")),
                        "heading": str(chunk.get("heading", "")),
                        "kind": str(chunk.get("kind", "text")),
                        "ordinal": int(chunk.get("ordinal", 0) or 0),
                        "text": str(chunk.get("text", "")),
                        "tags": list(item.get("tags") or []),
                        "vector": vectors.get(chunk_id, []),
                    }
                )
        self._index_key = key
        self._index = entries
        return entries

    def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        mode: str = "",
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        started = time.perf_counter()
        effective_mode = normalize_mode(mode or self.search_mode)
        try:
            hits = self._search_inner(
                query, top_k=top_k, mode=mode, tags=tags
            )
        except Exception:
            metrics.record_search_error(effective_mode)
            raise
        metrics.record_search(
            effective_mode,
            (time.perf_counter() - started) * 1_000,
            len(hits),
        )
        return hits

    def _search_inner(
        self,
        query: str,
        *,
        top_k: int = 8,
        mode: str = "",
        tags: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        mode = normalize_mode(mode or self.search_mode)
        query_tokens = tokens(query)
        if not query_tokens and mode == "lexical":
            return []
        with_vectors = bool(
            mode in {"vector", "hybrid"} and self._embedder is not None
        )
        entries = self._load_chunks(with_vectors=with_vectors)
        if tags:
            wanted = {str(tag).strip().lower() for tag in tags if tag}
            entries = [
                entry
                for entry in entries
                if wanted & {
                    str(tag).lower()
                    for tag in (entry.get("tags") or [])
                }
            ]
        if not entries:
            return []

        docs = [token_counts(str(entry.get("text", ""))) for entry in entries]
        lexical = bm25_scores(query_tokens, docs)
        vector_scores = [0.0 for _ in entries]
        if with_vectors:
            vectors = embed_texts(self._embedder, [query])
            if vectors:
                query_vector = vectors[0]
                header = provider_header(self._embedder)
                dimension = int(header.get("dimension", 0) or 0)
                if not dimension:
                    dimension = next(
                        (
                            len(entry.get("vector") or [])
                            for entry in entries
                            if entry.get("vector")
                        ),
                        0,
                    )
                ann_key = (
                    self._index_key,
                    self.vector_backend,
                    dimension,
                )
                if self._ann_key != ann_key or self._ann_index is None:
                    self._ann_index = build_vector_index(
                        entries,
                        dimension=dimension,
                        backend=self.vector_backend,
                    )
                    self._ann_key = ann_key
                    metrics.set_vector_index_size(
                        self._ann_index.backend, self._ann_index.size
                    )
                for index, value in self._ann_index.search(
                    query_vector,
                    min(self.vector_candidates, self._ann_index.size),
                ):
                    if 0 <= index < len(vector_scores):
                        vector_scores[index] = max(0.0, value)
        if mode in {"vector", "hybrid"} and (
            not with_vectors
            or not any(value > 0 for value in vector_scores)
        ):
            # provider/索引缺失时回退词法，检索是增强而不是前提。
            mode = "lexical"

        relevance = [0.0 for _ in entries]
        if mode == "vector":
            relevance = [max(0.0, value) for value in vector_scores]
        elif mode == "hybrid":
            lexical_ranking = [
                index
                for index in sorted(
                    range(len(entries)),
                    key=lambda item: (-lexical[item], item),
                )
                if lexical[index] > 0
            ]
            vector_ranking = [
                index
                for index in sorted(
                    range(len(entries)),
                    key=lambda item: (-vector_scores[item], item),
                )
                if vector_scores[index] > 0
            ]
            relevance = rrf_scores(
                [lexical_ranking, vector_ranking],
                len(entries),
                k=RRF_K,
            )
        else:
            relevance = lexical

        hits: list[dict[str, Any]] = []
        for index, entry in enumerate(entries):
            value = relevance[index]
            if value <= 0:
                continue
            hits.append(
                {
                    **entry,
                    "_relevance": value,
                    "lexical_score": round(lexical[index], 4),
                    "vector_score": round(vector_scores[index], 4),
                    "mode": mode,
                }
            )
        if self.rerank_enabled:
            hits = rerank_hits(
                hits, query, boost=self.rerank_boost
            )
        hits.sort(
            key=lambda hit: (
                -float(hit.get("_relevance", 0.0)),
                str(hit.get("item_id", "")),
                str(hit.get("anchor", "")),
                int(hit.get("ordinal", 0) or 0),
            )
        )
        limit = max(1, int(top_k))
        if hits and self.mmr_lambda < 1.0:
            candidates = hits[: min(len(hits), max(limit * 4, limit))]
            hits = mmr_select(
                candidates, top_k=limit, lambda_=self.mmr_lambda
            )
        else:
            hits = hits[:limit]
        return [
            {
                **{
                    key: value
                    for key, value in hit.items()
                    if key not in {"vector", "_relevance"}
                },
                "score": round(
                    float(hit.get("_relevance", 0.0)), 4
                ),
            }
            for hit in hits[:limit]
        ]
