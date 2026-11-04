"""`document_read`：把二进制/文本文档转成带锚点的分页文本。"""

from __future__ import annotations

import hashlib
import io
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, Field

from ubismart.config import Settings
from ubismart.execution.file_access import (
    FileAccess,
    LocalFileAccess,
    SandboxFileAccess,
)
from ubismart.platform.process_runner import (
    ProcessError,
    configure_process_runner,
    run_in_process,
)
from ubismart.sandbox.fs import SandboxFs
from ubismart.sandbox.wsl_exec import resolve_workspace
from ubismart.security.read_policy import protected_read_reason
from ubismart.security.secrets import redact_secrets
from ubismart.security.write_policy import protected_write_reason
from ubismart.tools.artifacts import (
    DocumentArtifact,
    DocumentEditArtifact,
    pack,
)
from ubismart.tools.documents.edit import (
    DOCUMENT_EDIT,
    DocumentEditInput,
    apply_edits,
    validate_paths,
)
from ubismart.tools.documents.extract import (
    extract_embedded_images,
    extract_pdf_attachments,
    parse_document,
)
from ubismart.tools.documents.models import (
    DocumentBlock,
    DocumentError,
    DocumentLimits,
    DocumentModel,
)

DOCUMENT_READ = "document_read"
PDF_PAGE_IMAGE = "pdf_page_image"
# CPU 密集、且底层库通常不可取消的格式；即使文件不大也走进程隔离。
_CPU_SUFFIXES = frozenset(
    {".pdf", ".docx", ".xlsx", ".xls", ".pptx", ".epub"}
)
_CACHE_SIZE = 16
_CACHE: OrderedDict[str, DocumentModel] = OrderedDict()
_CLEANED: set[str] = set()


class PdfPageImageInput(BaseModel):
    path: str = Field(description="工作区内 PDF 的相对路径")
    page: int = Field(default=1, ge=1, description="从 1 开始的页码")
    reason: str = Field(description="渲染页面的意图，一句话说明")


class DocumentReadInput(BaseModel):
    path: str = Field(description="工作区内文档的相对路径")
    reason: str = Field(description="读取文档的意图，一句话说明")
    mode: Literal["auto", "outline", "text", "tables"] = Field(
        default="auto",
        description="auto=全文分页；outline=只返回大纲/工作表/幻灯片标题；"
        "text=只要文本；tables=只要表格",
    )
    page_range: str = Field(
        default="", description='PDF 页码，例如 "1-5,8"；留空表示从第一页开始'
    )
    sheet: str = Field(default="", description="只读取指定工作表名称")
    slide_range: str = Field(
        default="", description='PPTX 幻灯片范围，例如 "1-3"；留空表示全部'
    )
    include_tables: bool = Field(default=True, description="是否包含表格内容")
    include_images: bool = Field(
        default=False,
        description="是否提取 DOCX/PPTX 内嵌图片到缓存并返回路径",
    )
    include_attachments: bool = Field(
        default=False,
        description="是否提取 PDF 附件到缓存并解析文本类附件",
    )
    offset: int = Field(default=0, ge=0, description="从第几个内容块开始返回")
    max_chars: int = Field(
        default=20_000,
        ge=1_000,
        le=200_000,
        description="本次返回的最大字符数（deep map-reduce 可到 200k）",
    )


def _limits(settings: Settings) -> DocumentLimits:
    return DocumentLimits(
        max_bytes=int(getattr(settings, "document_max_bytes", 20 * 1024 * 1024)),
        max_pages=int(getattr(settings, "document_max_pages", 200)),
        max_cells=int(getattr(settings, "document_max_cells", 20_000)),
        max_chars=int(
            getattr(settings, "document_max_chars", 2_000_000)
        ),
        pdf_text_only=bool(getattr(settings, "pdf_text_only", True)),
        pdf_metadata=bool(getattr(settings, "pdf_extract_metadata", True)),
        pdf_outline=bool(getattr(settings, "pdf_extract_outline", True)),
        pdf_annotations=bool(
            getattr(settings, "pdf_extract_annotations", True)
        ),
        pdf_forms=bool(getattr(settings, "pdf_extract_forms", True)),
        pdf_attachments=bool(
            getattr(settings, "pdf_extract_attachments", True)
        ),
        pdf_tables=bool(getattr(settings, "pdf_extract_tables", True)),
    )


def _cache_rel(sha: str) -> str:
    return f".agent/documents/{sha}/extract.json"


def _cache_dir_files(fs: Any, directory: str) -> tuple[list[str], int]:
    files: list[str] = []
    size = 0
    try:
        children = fs.list_dir(
            fs.lexical(directory), max_depth=3, limit=5_000
        )
    except Exception:  # noqa: BLE001
        children = []
    for child_kind, child_rel in children:
        if child_kind != "f":
            continue
        path = (
            child_rel
            if child_rel.startswith(directory + "/")
            else f"{directory}/{child_rel}"
        )
        try:
            child_info = fs.stat(fs.lexical(path))
            size += int(getattr(child_info, "size", 0) or 0)
            files.append(path)
        except Exception:  # noqa: BLE001
            continue
    return files, size


def _cache_items(fs: Any, base: str) -> list[tuple[float, int, list[str]]]:
    try:
        entries = fs.list_dir(fs.lexical(base), max_depth=1, limit=5_000)
    except Exception:  # noqa: BLE001 - 没有缓存目录是常态
        return []
    items: list[tuple[float, int, list[str]]] = []
    for kind, rel in entries:
        if kind != "d":
            continue
        directory = rel if rel.startswith(base) else f"{base}/{rel}"
        try:
            info = fs.stat(fs.lexical(directory))
            mtime = float(getattr(info, "mtime", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            continue
        files, size = _cache_dir_files(fs, directory)
        items.append((mtime, size, files))
    return items


def _remove_cache_files(fs: Any, files: list[str]) -> int:
    removed = 0
    for path in files:
        try:
            fs.remove(fs.lexical(path))
            removed += 1
        except Exception:  # noqa: BLE001
            continue
    return removed


def cleanup_document_cache(
    fs: Any | None,
    *,
    max_bytes: int = 512 * 1024 * 1024,
    ttl_days: int = 30,
) -> int:
    """按 TTL 与容量清理 `.agent/documents`；返回删除的文件数。"""
    if fs is None:
        return 0
    base = ".agent/documents"
    items = _cache_items(fs, base)
    now = time.time()
    ttl = max(0, int(ttl_days)) * 86_400
    removed = 0
    remaining: list[tuple[float, int, list[str]]] = []
    for item in items:
        mtime, size, files = item
        if ttl > 0 and now - mtime > ttl:
            removed += _remove_cache_files(fs, files)
        else:
            remaining.append(item)
    total = sum(item[1] for item in remaining)
    for _mtime, size, files in sorted(remaining, key=lambda item: item[0]):
        if max_bytes <= 0 or total <= max_bytes:
            break
        removed += _remove_cache_files(fs, files)
        total = max(0, total - size)
    return removed


def _disk_get(sha: str, fs: Any | None, root: str | None) -> DocumentModel | None:
    rel = _cache_rel(sha)
    try:
        if fs is not None:
            text = fs.read_text(rel, max_bytes=8 * 1024 * 1024)
        elif root is not None:
            text = Path(root, rel).read_text(encoding="utf-8")
        else:
            return None
        return DocumentModel.model_validate_json(text)
    except Exception:  # noqa: BLE001 - 缓存损坏/不可读都按 miss 处理
        return None


def _disk_put(
    model: DocumentModel, fs: Any | None, root: str | None
) -> None:
    rel = _cache_rel(model.sha256)
    try:
        payload = model.model_dump_json()
        if fs is not None:
            fs.write_text(rel, payload)
        elif root is not None:
            target = Path(root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
    except Exception:  # noqa: BLE001 - 缓存写入失败不影响读取
        return


def _remember(model: DocumentModel) -> None:
    _CACHE[model.sha256] = model.model_copy(deep=True)
    while len(_CACHE) > _CACHE_SIZE:
        _CACHE.popitem(last=False)


def _parse_cached(
    data: bytes,
    path: str,
    limits: DocumentLimits,
    *,
    fs: Any | None = None,
    root: str | None = None,
    cache_enabled: bool = True,
    settings: Settings | None = None,
) -> tuple[DocumentModel, bool]:
    sha = hashlib.sha256(data).hexdigest()
    cached = _CACHE.get(sha)
    if cached is not None:
        _CACHE.move_to_end(sha)
        result = cached.model_copy(deep=True)
        result.path = path
        return result, True
    if cache_enabled:
        disk = _disk_get(sha, fs, root)
        if disk is not None:
            disk.path = path
            _remember(disk)
            return disk, True
    suffix = Path(path).suffix.lower()
    isolate = bool(
        settings is not None
        and getattr(settings, "document_process_isolation", False)
        and (
            suffix in _CPU_SUFFIXES
            or (
                int(
                    getattr(
                        settings,
                        "document_process_threshold_bytes",
                        0,
                    )
                    or 0
                )
                > 0
                and len(data)
                >= int(
                    getattr(
                        settings,
                        "document_process_threshold_bytes",
                        0,
                    )
                )
            )
        )
    )
    if isolate:
        model = run_in_process(
            parse_document,
            (path, data),
            {"limits": limits},
            timeout_s=float(
                getattr(settings, "document_process_timeout_s", 60) or 60
            ),
            name=f"document:{suffix or 'unknown'}",
        )
    else:
        model = parse_document(path, data, limits=limits)
    _remember(model)
    if cache_enabled:
        _disk_put(model, fs, root)
    return model, False


def _range(text: str, total: int) -> set[int] | None:
    """解析 "1-3,8"；空串返回 None，非法值返回空集合（调用方给提示）。"""
    text = str(text or "").strip()
    if not text:
        return None
    values: set[int] = set()
    try:
        for part in text.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                left, right = part.split("-", 1)
                start, end = int(left), int(right)
                if start < 1 or end < start:
                    return set()
                values.update(range(start, end + 1))
            else:
                number = int(part)
                if number < 1:
                    return set()
                values.add(number)
    except ValueError:
        return set()
    return {value for value in values if value <= total}


def _block_page_number(block: DocumentBlock) -> int:
    try:
        return int(block.anchor.split("=", 1)[1])
    except ValueError:
        return 0


def _block_slide_number(block: DocumentBlock) -> int:
    try:
        return int(block.anchor.split("=", 1)[1].split("/", 1)[0])
    except ValueError:
        return 0


def _block_in_range(
    block: DocumentBlock,
    prefix: str,
    values: set[int] | None,
    parser: Any,
) -> bool:
    if values is None or not block.anchor.startswith(prefix):
        return True
    return parser(block) in values


def _sheet_selected(block: DocumentBlock, sheet: str) -> bool:
    if not sheet or block.kind != "sheet":
        return True
    return str(block.meta.get("sheet") or "") == sheet


def _mode_selected(block: DocumentBlock, mode: str) -> bool:
    if mode == "outline":
        return block.kind in {"heading", "sheet", "slide"}
    if mode == "text":
        return block.kind not in {"table", "sheet"}
    if mode == "tables":
        return block.kind in {"table", "sheet"}
    return True


def _selected(
    blocks: list[DocumentBlock],
    args: DocumentReadInput,
    model: DocumentModel,
) -> list[DocumentBlock]:
    pages = _range(args.page_range, model.pages)
    slides = _range(args.slide_range, model.slides)
    selected: list[DocumentBlock] = []
    for block in blocks:
        if not _sheet_selected(block, args.sheet):
            continue
        if not _block_in_range(block, "page=", pages, _block_page_number):
            continue
        if not _block_in_range(block, "slide=", slides, _block_slide_number):
            continue
        if not _mode_selected(block, args.mode):
            continue
        if not args.include_tables and block.kind in {"table", "sheet"}:
            continue
        selected.append(block)
    return selected


def _render(block: DocumentBlock) -> str:
    label = f"[{block.anchor}]" if block.anchor else ""
    if block.kind == "image":
        image_path = str(block.meta.get("image_path") or "")
        return f"{label} {redact_secrets(block.text)}（image_inspect: {image_path}）"
    if block.kind == "attachment":
        path = str(block.meta.get("attachment_path") or "")
        return f"{label} {redact_secrets(block.text)}（file_read: {path}）"
    if block.kind in {"table", "sheet"}:
        rows = [
            " | ".join(redact_secrets(cell) for cell in row)
            for row in block.rows
        ]
        body = "\n".join(rows) if rows else "（空表）"
        return f"{label} {redact_secrets(block.text)}\n{body}"
    return f"{label} {redact_secrets(block.text)}".strip()


_ATTACHMENT_TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".xml",
    ".yaml", ".yml", ".log", ".ini",
}
_NESTED_DOCUMENT_SUFFIXES = {".pdf", ".docx", ".xlsx", ".xls", ".pptx"}


def _is_file_info(info: Any) -> bool:
    if isinstance(info, dict):
        return bool(info.get("is_file"))
    return bool(getattr(info, "is_file", False))


@dataclass
class _ToolContext:
    settings: Settings
    access: FileAccess
    fs: Any | None
    root: str | None
    snapshots: Any | None
    scope: list[str]

    def display(self, target: str) -> str:
        try:
            return self.access.relative_to_root(target)
        except Exception:  # noqa: BLE001 - 展示失败不影响操作
            return str(target)

    def write_cache_bytes(self, rel: str, payload: bytes) -> None:
        if self.fs is not None:
            self.fs.write_bytes(self.fs.lexical(rel), payload)
        else:
            target = Path(self.root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

    def reject(self, path: str, message: str) -> str:
        artifact = DocumentArtifact(path=path, ok=False, rejected=True)
        return pack(message, artifact)

    def error(
        self, path: str, message: str, artifact: DocumentArtifact | None = None
    ) -> str:
        payload = artifact or DocumentArtifact(path=path, ok=False)
        payload.ok = False
        return pack(message, payload)


def _resolve_access(
    file_access: FileAccess | None,
    fs: Any | None,
    root: str | None,
) -> FileAccess:
    if file_access is not None:
        return file_access
    if fs is not None:
        return SandboxFileAccess(fs, root)
    return LocalFileAccess(root)


def _resolve_snapshots(
    settings: Settings,
    sandbox: Any | None,
    fs: Any | None,
    root: str | None,
    snapshots: Any | None,
) -> Any | None:
    if snapshots is not None or sandbox is None or fs is None:
        return snapshots
    from ubismart.sandbox.snapshots import SnapshotStore

    return SnapshotStore(
        sandbox,
        fs,
        root,
        manifest_root=settings.resolved_snapshot_manifest_dir,
    )


def _normalize_scope(write_scope: list[str] | None) -> list[str]:
    return [
        str(item).replace("\\", "/").strip("/")
        for item in (write_scope or [])
        if str(item).strip()
    ]


def _build_context(
    settings: Settings,
    sandbox: Any | None,
    *,
    file_access: FileAccess | None,
    fs: Any | None,
    root: str | None,
    snapshots: Any | None,
    write_scope: list[str] | None,
) -> _ToolContext:
    if root is None:
        if sandbox is None:
            raise ValueError("build_document_tools 需要 sandbox 或 root")
        root = resolve_workspace(settings, sandbox)
    if fs is None and sandbox is not None:
        fs = SandboxFs(sandbox, root)
    access = _resolve_access(file_access, fs, root)
    snapshots = _resolve_snapshots(settings, sandbox, fs, root, snapshots)
    return _ToolContext(
        settings=settings,
        access=access,
        fs=fs,
        root=root,
        snapshots=snapshots,
        scope=_normalize_scope(write_scope),
    )


def _bootstrap_cache(ctx: _ToolContext) -> None:
    settings = ctx.settings
    if ctx.fs is None or not getattr(settings, "document_cache_enabled", True):
        return
    cache_key = f"{id(ctx.fs)}:{ctx.root}"
    if cache_key in _CLEANED:
        return
    _CLEANED.add(cache_key)
    try:
        cleanup_document_cache(
            ctx.fs,
            max_bytes=int(
                getattr(
                    settings,
                    "document_cache_max_bytes",
                    512 * 1024 * 1024,
                )
            ),
            ttl_days=int(getattr(settings, "document_cache_ttl_days", 30)),
        )
    except Exception:  # noqa: BLE001 - 清理是 best-effort
        pass


def _read_target(ctx: _ToolContext, path: str) -> tuple[str | None, str | None]:
    try:
        target = ctx.access.lexical(path)
    except Exception as exc:  # noqa: BLE001
        return None, ctx.reject(path, f"路径被拒绝：{exc}")
    display = ctx.display(target)
    protected = protected_read_reason(display, ctx.settings.read_deny_globs)
    if protected:
        return None, ctx.reject(path, protected)
    return display, None


def _read_stat(ctx: _ToolContext, path: str) -> str | None:
    try:
        info = ctx.access.stat(path)
    except Exception as exc:  # noqa: BLE001
        return ctx.reject(path, f"无法解析文件：{exc}")
    real_path = (
        str(info.get("real_path") or "")
        if isinstance(info, dict)
        else str(getattr(info, "real_path", "") or "")
    )
    if real_path:
        real_protected = protected_read_reason(
            real_path, ctx.settings.read_deny_globs
        )
        if real_protected:
            return ctx.reject(path, real_protected)
    if not _is_file_info(info):
        return ctx.reject(path, "不是普通文件")
    return None


def _read_document(
    ctx: _ToolContext,
    path: str,
    display: str,
    limits: DocumentLimits,
    cache_enabled: bool,
) -> tuple[bytes | None, DocumentModel | None, bool, str | None]:
    try:
        data = ctx.access.read_bytes(path, max_bytes=limits.max_bytes)
        if len(data) > limits.max_bytes:
            raise DocumentError(
                f"文件过大：{len(data)} 字节 > {limits.max_bytes} 字节"
            )
        model, cache_hit = _parse_cached(
            data,
            display,
            limits,
            fs=ctx.fs,
            root=ctx.root,
            cache_enabled=cache_enabled,
            settings=ctx.settings,
        )
    except DocumentError as exc:
        return None, None, False, ctx.error(path, str(exc))
    except ProcessError as exc:
        return None, None, False, ctx.error(
            path, f"文档解析进程失败：{exc}"
        )
    except Exception as exc:  # noqa: BLE001 - 沙箱/宿主错误统一可读化
        return None, None, False, ctx.error(
            path, f"读取失败：{type(exc).__name__}: {exc}"
        )
    return data, model, cache_hit, None


def _read_images(
    ctx: _ToolContext,
    args: DocumentReadInput,
    model: DocumentModel,
    display: str,
    data: bytes,
) -> list[str]:
    image_paths: list[str] = []
    if not args.include_images:
        return image_paths
    try:
        images = extract_embedded_images(display, data)
    except DocumentError as exc:
        model.warnings.append(str(exc))
        images = []
    for index, (name, blob) in enumerate(images, 1):
        safe_name = Path(name).name.replace("/", "_")[:80] or f"{index}.png"
        rel = f".agent/documents/{model.sha256}/images/{index}-{safe_name}"
        try:
            ctx.write_cache_bytes(rel, blob)
        except Exception as exc:  # noqa: BLE001
            model.warnings.append(f"图片 {index} 缓存失败：{exc}")
            continue
        image_paths.append(rel)
        model.blocks.append(
            DocumentBlock(
                kind="image",
                anchor=f"image={index}",
                text=f"图片 {index}",
                meta={"image_path": rel},
            )
        )
    return image_paths


def _attachment_text_blocks(
    index: int, name: str, blob: bytes
) -> list[DocumentBlock]:
    suffix = Path(name).suffix.lower()
    if suffix not in _ATTACHMENT_TEXT_SUFFIXES:
        return []
    try:
        content = blob.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        content = ""
    if not content.strip():
        return []
    return [
        DocumentBlock(
            kind="text",
            anchor=f"attachment={index}/content",
            text=content[:20_000],
        )
    ]


def _attachment_nested_blocks(
    model: DocumentModel,
    index: int,
    name: str,
    blob: bytes,
    limits: DocumentLimits,
) -> list[DocumentBlock]:
    if Path(name).suffix.lower() not in _NESTED_DOCUMENT_SUFFIXES:
        return []
    try:
        nested = parse_document(name, blob, limits=limits)
    except DocumentError as exc:
        model.warnings.append(f"附件 {name} 无法解析：{exc}")
        return []
    blocks: list[DocumentBlock] = []
    chars = 0
    for block in nested.blocks[:50]:
        chars += len(block.text)
        if chars > 20_000:
            break
        anchor = (
            f"attachment={index}/{block.anchor}"
            if block.anchor
            else f"attachment={index}"
        )
        blocks.append(block.model_copy(update={"anchor": anchor}))
    return blocks


def _read_attachments(
    ctx: _ToolContext,
    args: DocumentReadInput,
    model: DocumentModel,
    data: bytes,
    limits: DocumentLimits,
) -> int:
    if not args.include_attachments or model.format != "pdf":
        return 0
    try:
        attachments = extract_pdf_attachments(data)
    except DocumentError as exc:
        model.warnings.append(str(exc))
        attachments = []
    attachment_count = 0
    for index, (name, blob) in enumerate(attachments, 1):
        safe_name = Path(name).name.replace("/", "_")[:120] or f"{index}.bin"
        rel = (
            f".agent/documents/{model.sha256}/attachments/"
            f"{index}-{safe_name}"
        )
        try:
            ctx.write_cache_bytes(rel, blob)
        except Exception as exc:  # noqa: BLE001
            model.warnings.append(f"附件 {name} 缓存失败：{exc}")
            continue
        attachment_count += 1
        model.blocks.append(
            DocumentBlock(
                kind="attachment",
                anchor=f"attachment={index}",
                text=f"{name}（{len(blob)} 字节，cache: {rel}）",
                meta={"attachment_path": rel, "name": name},
            )
        )
        model.blocks.extend(
            _attachment_text_blocks(index, name, blob)
        )
        model.blocks.extend(
            _attachment_nested_blocks(model, index, name, blob, limits)
        )
    return attachment_count


def _render_body(
    blocks: list[DocumentBlock],
    args: DocumentReadInput,
    model: DocumentModel,
) -> tuple[list[str], int, bool]:
    body: list[str] = []
    used = 0
    truncated = bool(model.truncated)
    for block in blocks[args.offset :]:
        text = _render(block)
        if used + len(text) + 1 > args.max_chars:
            truncated = True
            break
        body.append(text)
        used += len(text) + 1
    return body, used, truncated


def _read_header(
    display: str,
    model: DocumentModel,
    reason: str,
    args: DocumentReadInput,
    body: list[str],
    truncated: bool,
) -> str:
    header = (
        f"文档 {display}（{model.format or '未知'}，{model.size} 字节"
        f"，页 {model.pages} / 表 {model.sheets} / 幻灯片 {model.slides}"
        f"，共 {len(model.blocks)} 个内容块）\n{reason}"
    )
    if model.title:
        header += f"\n标题：{model.title}"
    if model.warnings:
        header += "\n提示：" + "；".join(model.warnings)
    if args.offset:
        header += f"\n从第 {args.offset + 1} 个内容块开始"
    if not body:
        header += "\n（没有匹配的内容；可调整 page_range/sheet/slide_range 或 mode）"
    if truncated:
        header += "\n（内容已截断；请用 offset/max_chars 继续读取）"
    return header


def _read(
    ctx: _ToolContext,
    path: str,
    reason: str,
    *,
    mode: str = "auto",
    page_range: str = "",
    sheet: str = "",
    slide_range: str = "",
    include_tables: bool = True,
    include_images: bool = False,
    include_attachments: bool = False,
    offset: int = 0,
    max_chars: int = 20_000,
) -> str:
    args = DocumentReadInput(
        path=path,
        reason=reason,
        mode=mode,  # type: ignore[arg-type]
        page_range=page_range,
        sheet=sheet,
        slide_range=slide_range,
        include_tables=include_tables,
        include_images=include_images,
        include_attachments=include_attachments,
        offset=offset,
        max_chars=max_chars,
    )
    display, error = _read_target(ctx, path)
    if error is not None:
        return error
    error = _read_stat(ctx, path)
    if error is not None:
        return error

    limits = _limits(ctx.settings)
    limits.pptx_layout_images = bool(args.include_images)
    cache_enabled = bool(
        getattr(ctx.settings, "document_cache_enabled", True)
    )
    data, model, cache_hit, error = _read_document(
        ctx, path, display, limits, cache_enabled
    )
    if error is not None:
        return error

    image_paths = _read_images(ctx, args, model, display, data)
    attachment_count = _read_attachments(ctx, args, model, data, limits)

    blocks = _selected(model.blocks, args, model)
    body, used, truncated = _render_body(blocks, args, model)
    header = _read_header(display, model, reason, args, body, truncated)

    artifact = DocumentArtifact(
        path=display,
        format=model.format,
        ok=True,
        size=model.size,
        sha256=model.sha256,
        title=model.title,
        pages=model.pages,
        sheets=model.sheets,
        slides=model.slides,
        blocks=len(model.blocks),
        chars=used,
        truncated=truncated,
        needs_ocr=model.needs_ocr,
        scanned_pages=len(model.scanned_pages),
        cached=cache_hit,
        image_paths=image_paths,
        attachments=attachment_count,
        warnings=model.warnings,
    )
    return pack(header + "\n\n" + "\n\n".join(body), artifact)


def _page_image_target(
    ctx: _ToolContext, path: str
) -> tuple[str | None, str | None]:
    if Path(str(path or "")).suffix.lower() != ".pdf":
        return None, ctx.error(path, "pdf_page_image 只接受 PDF")
    try:
        target = ctx.access.lexical(path)
    except Exception as exc:  # noqa: BLE001
        return None, ctx.reject(path, f"路径被拒绝：{exc}")
    display = ctx.display(target)
    protected = protected_read_reason(display, ctx.settings.read_deny_globs)
    if protected:
        return None, ctx.reject(path, protected)
    return display, None


def _page_image_data(
    ctx: _ToolContext, path: str
) -> tuple[bytes | None, str | None]:
    try:
        info = ctx.access.stat(path)
        if not _is_file_info(info):
            return None, ctx.reject(path, "不是普通文件")
        limits = _limits(ctx.settings)
        data = ctx.access.read_bytes(path, max_bytes=limits.max_bytes)
    except Exception as exc:  # noqa: BLE001
        return None, ctx.error(
            path, f"读取 PDF 失败：{type(exc).__name__}: {exc}"
        )
    return data, None


def _render_pdf_page(
    data: bytes, page: int
) -> tuple[tuple[int, bytes] | None, str | None]:
    try:
        import pypdfium2 as pdfium
    except ImportError:
        return None, (
            "缺少 PDF 渲染依赖 pypdfium2。"
            "请安装：pip install 'ubismart[documents]'。"
        )
    try:
        document = pdfium.PdfDocument(data)
        try:
            total = len(document)
            if page < 1 or page > total:
                return None, f"页码越界：{page}，共 {total} 页"
            bitmap = document[page - 1].render(scale=2)
            image = bitmap.to_pil()
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
        finally:
            document.close()
    except Exception as exc:  # noqa: BLE001 - 渲染失败应可读
        return None, f"PDF 页面渲染失败：{type(exc).__name__}: {exc}"
    return (total, buffer.getvalue()), None


def _page_image(ctx: _ToolContext, path: str, page: int, reason: str) -> str:
    display, error = _page_image_target(ctx, path)
    if error is not None:
        return error
    data, error = _page_image_data(ctx, path)
    if error is not None:
        return error
    rendered, message = _render_pdf_page(data, page)
    if message is not None:
        return ctx.error(path, message)
    total, payload = rendered

    sha = hashlib.sha256(data).hexdigest()
    rel = f".agent/documents/{sha}/pages/page-{page}.png"
    try:
        ctx.write_cache_bytes(rel, payload)
    except Exception as exc:  # noqa: BLE001
        return ctx.error(
            path, f"缓存页面图片失败：{type(exc).__name__}: {exc}"
        )

    artifact = DocumentArtifact(
        path=display,
        format="pdf",
        ok=True,
        size=len(data),
        sha256=sha,
        pages=total,
        image_path=rel,
    )
    return pack(
        f"已渲染 {display} 第 {page}/{total} 页为图片：{rel}\n{reason}",
        artifact,
    )


def _edit_paths(
    ctx: _ToolContext, path: str, args: DocumentEditInput
) -> tuple[tuple[str, str, str, str, str] | None, str | None]:
    try:
        rel_out, fmt, source = validate_paths(args.path, args.out_path)
        source_target = ctx.access.lexical(source)
        out_target = ctx.access.lexical(rel_out)
    except Exception as exc:  # noqa: BLE001
        return None, ctx.reject(path, f"路径被拒绝：{exc}")
    return (rel_out, fmt, source, source_target, out_target), None


def _edit_source_check(
    ctx: _ToolContext, path: str, source: str, source_target: str
) -> tuple[str | None, str | None]:
    display = ctx.display(source_target)
    protected = protected_read_reason(display, ctx.settings.read_deny_globs)
    if protected:
        return None, ctx.reject(path, protected)
    try:
        info = ctx.access.stat(source)
    except Exception as exc:  # noqa: BLE001
        return None, ctx.reject(path, f"无法解析源文件：{exc}")
    if not _is_file_info(info):
        return None, ctx.reject(path, "源路径不是普通文件")
    return display, None


def _apply_edit_payload(
    ctx: _ToolContext,
    path: str,
    display: str,
    source: str,
    operations: list[Any],
) -> tuple[tuple[bytes, int, list[str], DocumentLimits] | None, str | None]:
    limits = _limits(ctx.settings)
    try:
        data = ctx.access.read_bytes(source, max_bytes=limits.max_bytes)
        payload, replacements, warnings = apply_edits(
            display, data, operations
        )
    except DocumentError as exc:
        return None, ctx.error(path, str(exc))
    except Exception as exc:  # noqa: BLE001 - 解析/序列化错误统一可读化
        return None, ctx.error(
            path, f"编辑失败：{type(exc).__name__}: {exc}"
        )
    if len(payload) > limits.max_bytes:
        return None, ctx.error(
            path,
            f"输出过大：{len(payload)} 字节 > {limits.max_bytes} 字节",
        )
    return (payload, replacements, warnings, limits), None


def _edit_write_check(
    ctx: _ToolContext, path: str, rel_out: str
) -> str | None:
    scope = ctx.scope
    if scope and not any(
        rel_out == item or rel_out.startswith(item + "/")
        for item in scope
    ):
        return ctx.reject(
            path,
            f"输出不在 write_scope 内：{rel_out}。"
            f"允许写入：{', '.join(scope)}",
        )
    out_protected = protected_write_reason(
        rel_out, ctx.settings.write_deny_globs
    )
    if out_protected:
        return ctx.reject(path, out_protected)
    try:
        out_exists = _is_file_info(ctx.access.stat(rel_out))
    except Exception:  # noqa: BLE001 - 不存在是常态
        out_exists = False
    if out_exists:
        return ctx.reject(
            path,
            f"输出文件已存在：{rel_out}；为避免覆盖请换一个新路径",
        )
    return None


def _write_edit_output(
    ctx: _ToolContext, path: str, out_target: str, payload: bytes
) -> tuple[str | None, str | None]:
    snapshot_id = ""
    try:
        if ctx.snapshots is not None:
            snapshot_id = ctx.snapshots.save(out_target, "", existed=False)
        if ctx.fs is not None:
            ctx.fs.write_bytes(out_target, payload)
        else:
            target_path = Path(out_target)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(payload)
    except Exception as exc:  # noqa: BLE001
        return None, ctx.error(
            path, f"写入失败：{type(exc).__name__}: {exc}"
        )
    return snapshot_id, None


def _edit_result(
    ctx: _ToolContext,
    path: str,
    args: DocumentEditInput,
    rel_out: str,
    fmt: str,
    display: str,
    reason: str,
    payload: bytes,
    replacements: int,
    warnings: list[str],
    snapshot_id: str,
) -> str:
    artifact = DocumentEditArtifact(
        path=rel_out,
        action="create",
        ok=True,
        bytes_written=len(payload),
        snapshot_id=snapshot_id or None,
        format=fmt,
        operations=len(args.operations),
        replacements=replacements,
        warnings=warnings,
    )
    header = (
        f"已编辑 {display} → {rel_out}（{fmt}，"
        f"{len(args.operations)} 个操作"
        + (f"，替换 {replacements} 处" if replacements else "")
        + f"，{len(payload)} 字节）\n{reason}"
    )
    if warnings:
        header += "\n提示：" + "；".join(warnings)
    return pack(header, artifact)


def _edit(
    ctx: _ToolContext,
    path: str,
    out_path: str,
    reason: str,
    operations: Any,
) -> str:
    try:
        args = DocumentEditInput(
            path=path,
            out_path=out_path,
            reason=reason,
            operations=operations,
        )
    except Exception as exc:  # noqa: BLE001 - pydantic 校验错误要可读
        return ctx.reject(path, f"参数无效：{exc}")
    max_ops = int(getattr(ctx.settings, "document_edit_max_ops", 50))
    if len(args.operations) > max_ops:
        return ctx.reject(
            path, f"操作过多：{len(args.operations)} 条 > 上限 {max_ops}"
        )
    paths, error = _edit_paths(ctx, path, args)
    if error is not None:
        return error
    rel_out, fmt, source, source_target, out_target = paths

    display, error = _edit_source_check(
        ctx, path, source, source_target
    )
    if error is not None:
        return error

    applied, error = _apply_edit_payload(
        ctx, path, display, source, args.operations
    )
    if error is not None:
        return error
    payload, replacements, warnings, _limits_used = applied

    error = _edit_write_check(ctx, path, rel_out)
    if error is not None:
        return error

    snapshot_id, error = _write_edit_output(ctx, path, out_target, payload)
    if error is not None:
        return error

    return _edit_result(
        ctx,
        path,
        args,
        rel_out,
        fmt,
        display,
        reason,
        payload,
        replacements,
        warnings,
        snapshot_id,
    )


def _make_read_tool(ctx: _ToolContext) -> Any:
    def _bound_read(
        path: str,
        reason: str,
        mode: str = "auto",
        page_range: str = "",
        sheet: str = "",
        slide_range: str = "",
        include_tables: bool = True,
        include_images: bool = False,
        include_attachments: bool = False,
        offset: int = 0,
        max_chars: int = 20_000,
    ) -> str:
        return _read(
            ctx,
            path,
            reason,
            mode=mode,
            page_range=page_range,
            sheet=sheet,
            slide_range=slide_range,
            include_tables=include_tables,
            include_images=include_images,
            include_attachments=include_attachments,
            offset=offset,
            max_chars=max_chars,
        )

    return _bound_read


def _make_page_image_tool(ctx: _ToolContext) -> Any:
    def _bound_page_image(path: str, page: int, reason: str) -> str:
        return _page_image(ctx, path, page, reason)

    return _bound_page_image


def _make_edit_tool(ctx: _ToolContext) -> Any:
    def _bound_edit(
        path: str, out_path: str, reason: str, operations: Any
    ) -> str:
        return _edit(ctx, path, out_path, reason, operations)

    return _bound_edit


def _make_tools(ctx: _ToolContext, allow_write: bool) -> list[BaseTool]:
    tools: list[BaseTool] = [
        StructuredTool.from_function(
            func=_make_read_tool(ctx),
            name=DOCUMENT_READ,
            description=(
                "读取并解析工作区内的 PDF/DOCX/XLSX/XLS/PPTX/Markdown/CSV/文本文件，"
                "返回带页码/工作表/幻灯片锚点的分页文本与表格。"
                "旧版 .doc/.ppt 不支持，会提示先转为 docx/pptx。"
                "长文档先用 mode=outline 看结构，再按 page_range/sheet/slide_range 分段读取。"
                "只读，不修改文件。"
            ),
            args_schema=DocumentReadInput,
        ),
        StructuredTool.from_function(
            func=_make_page_image_tool(ctx),
            name=PDF_PAGE_IMAGE,
            description=(
                "把 PDF 的指定页渲染成 PNG（写入 .agent/documents 缓存），"
                "返回图片路径，供 image_inspect 理解扫描页/图表。只读原 PDF。"
            ),
            args_schema=PdfPageImageInput,
        ),
    ]
    if allow_write:
        tools.append(
            StructuredTool.from_function(
                func=_make_edit_tool(ctx),
                name=DOCUMENT_EDIT,
                description=(
                    "编辑 DOCX/XLSX/PPTX 并写入一个**新文件**（纯 Python，不调用"
                    " LibreOffice）。支持：docx 文本替换/追加段落/设置页眉页脚；"
                    "xlsx 设置单元格值或公式；pptx 文本替换/新增幻灯片文本。"
                    "源与输出格式必须一致，输出路径不能已存在；不支持宏文档与"
                    "旧格式，也不做跨格式转换。"
                ),
                args_schema=DocumentEditInput,
            )
        )
    return tools


def build_document_tools(
    settings: Settings,
    sandbox: Any | None = None,
    *,
    allow_write: bool = False,
    write_scope: list[str] | None = None,
    file_access: FileAccess | None = None,
    fs: Any | None = None,
    root: str | None = None,
    snapshots: Any | None = None,
) -> list[BaseTool]:
    configure_process_runner(
        max_concurrent=int(
            getattr(settings, "process_runner_max_concurrent", 2) or 2
        )
    )
    ctx = _build_context(
        settings,
        sandbox,
        file_access=file_access,
        fs=fs,
        root=root,
        snapshots=snapshots,
        write_scope=write_scope,
    )
    _bootstrap_cache(ctx)
    return _make_tools(ctx, allow_write)
