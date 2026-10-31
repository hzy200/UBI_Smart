"""`document_read`：把二进制/文本文档转成带锚点的分页文本。"""

from __future__ import annotations

import hashlib
import io
import time
from collections import OrderedDict
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
    removed = 0
    try:
        entries = fs.list_dir(fs.lexical(base), max_depth=1, limit=5_000)
    except Exception:  # noqa: BLE001 - 没有缓存目录是常态
        return 0
    now = time.time()
    ttl = max(0, int(ttl_days)) * 86_400
    items: list[tuple[float, int, list[str]]] = []
    total = 0
    for kind, rel in entries:
        if kind != "d":
            continue
        directory = rel if rel.startswith(base) else f"{base}/{rel}"
        try:
            info = fs.stat(fs.lexical(directory))
            mtime = float(getattr(info, "mtime", 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            continue
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
        items.append((mtime, size, files))
        total += size

    def remove_files(files: list[str], size: int) -> int:
        nonlocal removed, total
        for path in files:
            try:
                fs.remove(fs.lexical(path))
                removed += 1
            except Exception:  # noqa: BLE001
                continue
        total = max(0, total - size)
        return len(files)

    remaining: list[tuple[float, int, list[str]]] = []
    for item in items:
        mtime, size, files = item
        if ttl > 0 and now - mtime > ttl:
            remove_files(files, size)
        else:
            remaining.append(item)
    total = sum(item[1] for item in remaining)
    for _mtime, size, files in sorted(remaining, key=lambda item: item[0]):
        if max_bytes <= 0 or total <= max_bytes:
            break
        remove_files(files, size)
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


def _selected(
    blocks: list[DocumentBlock],
    args: DocumentReadInput,
    model: DocumentModel,
) -> list[DocumentBlock]:
    pages = _range(args.page_range, model.pages)
    slides = _range(args.slide_range, model.slides)
    selected: list[DocumentBlock] = []
    for block in blocks:
        if args.sheet and block.kind == "sheet":
            if str(block.meta.get("sheet") or "") != args.sheet:
                continue
        if pages is not None and block.anchor.startswith("page="):
            try:
                page = int(block.anchor.split("=", 1)[1])
            except ValueError:
                page = 0
            if page not in pages:
                continue
        if slides is not None and block.anchor.startswith("slide="):
            try:
                slide = int(block.anchor.split("=", 1)[1].split("/", 1)[0])
            except ValueError:
                slide = 0
            if slide not in slides:
                continue
        if args.mode == "outline":
            if block.kind not in {"heading", "sheet", "slide"}:
                continue
        elif args.mode == "text":
            if block.kind in {"table", "sheet"}:
                continue
        elif args.mode == "tables":
            if block.kind not in {"table", "sheet"}:
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
    if root is None:
        if sandbox is None:
            raise ValueError("build_document_tools 需要 sandbox 或 root")
        root = resolve_workspace(settings, sandbox)
    if fs is None and sandbox is not None:
        fs = SandboxFs(sandbox, root)
    if file_access is not None:
        access: FileAccess = file_access
    elif fs is not None:
        access = SandboxFileAccess(fs, root)
    else:
        access = LocalFileAccess(root)
    if snapshots is None and sandbox is not None and fs is not None:
        from ubismart.sandbox.snapshots import SnapshotStore

        snapshots = SnapshotStore(
            sandbox,
            fs,
            root,
            manifest_root=settings.resolved_snapshot_manifest_dir,
        )
    scope = [
        str(item).replace("\\", "/").strip("/")
        for item in (write_scope or [])
        if str(item).strip()
    ]

    def _display(target: str) -> str:
        try:
            return access.relative_to_root(target)
        except Exception:  # noqa: BLE001 - 展示失败不影响操作
            return str(target)

    if fs is not None and getattr(settings, "document_cache_enabled", True):
        cache_key = f"{id(fs)}:{root}"
        if cache_key not in _CLEANED:
            _CLEANED.add(cache_key)
            try:
                cleanup_document_cache(
                    fs,
                    max_bytes=int(
                        getattr(
                            settings,
                            "document_cache_max_bytes",
                            512 * 1024 * 1024,
                        )
                    ),
                    ttl_days=int(
                        getattr(settings, "document_cache_ttl_days", 30)
                    ),
                )
            except Exception:  # noqa: BLE001 - 清理是 best-effort
                pass

    def _write_cache_bytes(rel: str, payload: bytes) -> None:
        if fs is not None:
            fs.write_bytes(fs.lexical(rel), payload)
        else:
            target = Path(root, rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)

    def _reject(path: str, message: str) -> str:
        artifact = DocumentArtifact(path=path, ok=False, rejected=True)
        return pack(message, artifact)

    def _error(path: str, message: str, artifact: DocumentArtifact | None = None) -> str:
        payload = artifact or DocumentArtifact(path=path, ok=False)
        payload.ok = False
        return pack(message, payload)

    def _read(
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
        try:
            target = access.lexical(path)
        except Exception as exc:  # noqa: BLE001
            return _reject(path, f"路径被拒绝：{exc}")

        display = _display(target)
        protected = protected_read_reason(display, settings.read_deny_globs)
        if protected:
            return _reject(path, protected)

        try:
            info = access.stat(path)
        except Exception as exc:  # noqa: BLE001
            return _reject(path, f"无法解析文件：{exc}")
        real_path = (
            str(info.get("real_path") or "")
            if isinstance(info, dict)
            else str(getattr(info, "real_path", "") or "")
        )
        if real_path:
            real_protected = protected_read_reason(real_path, settings.read_deny_globs)
            if real_protected:
                return _reject(path, real_protected)
        is_file = (
            bool(info.get("is_file"))
            if isinstance(info, dict)
            else bool(getattr(info, "is_file", False))
        )
        if not is_file:
            return _reject(path, "不是普通文件")

        limits = _limits(settings)
        limits.pptx_layout_images = bool(args.include_images)
        try:
            data = access.read_bytes(path, max_bytes=limits.max_bytes)
            if len(data) > limits.max_bytes:
                raise DocumentError(
                    f"文件过大：{len(data)} 字节 > {limits.max_bytes} 字节"
                )
            model, cache_hit = _parse_cached(
                data,
                display,
                limits,
                fs=fs,
                root=root,
                cache_enabled=bool(
                    getattr(settings, "document_cache_enabled", True)
                ),
            )
        except DocumentError as exc:
            return _error(path, str(exc))
        except Exception as exc:  # noqa: BLE001 - 沙箱/宿主错误统一可读化
            return _error(path, f"读取失败：{type(exc).__name__}: {exc}")

        image_paths: list[str] = []
        if args.include_images:
            try:
                images = extract_embedded_images(display, data)
            except DocumentError as exc:
                model.warnings.append(str(exc))
                images = []
            for index, (name, blob) in enumerate(images, 1):
                safe_name = Path(name).name.replace("/", "_")[:80] or f"{index}.png"
                rel = (
                    f".agent/documents/{model.sha256}/images/"
                    f"{index}-{safe_name}"
                )
                try:
                    _write_cache_bytes(rel, blob)
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
        attachment_count = 0
        if args.include_attachments and model.format == "pdf":
            try:
                attachments = extract_pdf_attachments(data)
            except DocumentError as exc:
                model.warnings.append(str(exc))
                attachments = []
            text_suffixes = {
                ".txt", ".md", ".markdown", ".csv", ".json", ".xml",
                ".yaml", ".yml", ".log", ".ini",
            }
            for index, (name, blob) in enumerate(attachments, 1):
                safe_name = Path(name).name.replace("/", "_")[:120] or f"{index}.bin"
                rel = (
                    f".agent/documents/{model.sha256}/attachments/"
                    f"{index}-{safe_name}"
                )
                try:
                    _write_cache_bytes(rel, blob)
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
                suffix = Path(name).suffix.lower()
                if suffix in text_suffixes:
                    try:
                        content = blob.decode("utf-8", errors="replace")
                    except Exception:  # noqa: BLE001
                        content = ""
                    if content.strip():
                        model.blocks.append(
                            DocumentBlock(
                                kind="text",
                                anchor=f"attachment={index}/content",
                                text=content[:20_000],
                            )
                        )
                elif suffix in {".pdf", ".docx", ".xlsx", ".xls", ".pptx"}:
                    try:
                        nested = parse_document(name, blob, limits=limits)
                    except DocumentError as exc:
                        model.warnings.append(
                            f"附件 {name} 无法解析：{exc}"
                        )
                        continue
                    chars = 0
                    for block in nested.blocks[:50]:
                        chars += len(block.text)
                        if chars > 20_000:
                            break
                        model.blocks.append(
                            block.model_copy(
                                update={
                                    "anchor": (
                                        f"attachment={index}/{block.anchor}"
                                        if block.anchor
                                        else f"attachment={index}"
                                    )
                                }
                            )
                        )

        blocks = _selected(model.blocks, args, model)
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

    def _page_image(path: str, page: int, reason: str) -> str:
        if Path(str(path or "")).suffix.lower() != ".pdf":
            return _error(path, "pdf_page_image 只接受 PDF")
        try:
            target = access.lexical(path)
        except Exception as exc:  # noqa: BLE001
            return _reject(path, f"路径被拒绝：{exc}")
        display = _display(target)
        protected = protected_read_reason(display, settings.read_deny_globs)
        if protected:
            return _reject(path, protected)
        try:
            info = access.stat(path)
            if not (
                bool(info.get("is_file"))
                if isinstance(info, dict)
                else bool(getattr(info, "is_file", False))
            ):
                return _reject(path, "不是普通文件")
            limits = _limits(settings)
            data = access.read_bytes(path, max_bytes=limits.max_bytes)
        except Exception as exc:  # noqa: BLE001
            return _error(path, f"读取 PDF 失败：{type(exc).__name__}: {exc}")

        try:
            import pypdfium2 as pdfium
        except ImportError:
            return _error(
                path,
                "缺少 PDF 渲染依赖 pypdfium2。"
                "请安装：pip install 'ubismart[documents]'。",
            )
        try:
            document = pdfium.PdfDocument(data)
            try:
                total = len(document)
                if page < 1 or page > total:
                    return _error(path, f"页码越界：{page}，共 {total} 页")
                bitmap = document[page - 1].render(scale=2)
                image = bitmap.to_pil()
                buffer = io.BytesIO()
                image.save(buffer, format="PNG")
            finally:
                document.close()
        except Exception as exc:  # noqa: BLE001 - 渲染失败应可读
            return _error(path, f"PDF 页面渲染失败：{type(exc).__name__}: {exc}")

        sha = hashlib.sha256(data).hexdigest()
        rel = f".agent/documents/{sha}/pages/page-{page}.png"
        payload = buffer.getvalue()
        try:
            if fs is not None:
                fs.write_bytes(fs.lexical(rel), payload)
            else:
                target_path = Path(root, rel)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(payload)
        except Exception as exc:  # noqa: BLE001
            return _error(path, f"缓存页面图片失败：{type(exc).__name__}: {exc}")

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

    def _edit(
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
            return _reject(path, f"参数无效：{exc}")
        max_ops = int(getattr(settings, "document_edit_max_ops", 50))
        if len(args.operations) > max_ops:
            return _reject(
                path, f"操作过多：{len(args.operations)} 条 > 上限 {max_ops}"
            )
        try:
            rel_out, fmt, source = validate_paths(args.path, args.out_path)
            source_target = access.lexical(source)
            out_target = access.lexical(rel_out)
        except Exception as exc:  # noqa: BLE001
            return _reject(path, f"路径被拒绝：{exc}")

        display = _display(source_target)
        protected = protected_read_reason(display, settings.read_deny_globs)
        if protected:
            return _reject(path, protected)
        try:
            info = access.stat(source)
            is_file = (
                bool(info.get("is_file"))
                if isinstance(info, dict)
                else bool(getattr(info, "is_file", False))
            )
        except Exception as exc:  # noqa: BLE001
            return _reject(path, f"无法解析源文件：{exc}")
        if not is_file:
            return _reject(path, "源路径不是普通文件")

        limits = _limits(settings)
        try:
            data = access.read_bytes(
                source, max_bytes=limits.max_bytes
            )
            payload, replacements, warnings = apply_edits(
                display, data, args.operations
            )
        except DocumentError as exc:
            return _error(path, str(exc))
        except Exception as exc:  # noqa: BLE001 - 解析/序列化错误统一可读化
            return _error(path, f"编辑失败：{type(exc).__name__}: {exc}")
        if len(payload) > limits.max_bytes:
            return _error(
                path,
                f"输出过大：{len(payload)} 字节 > {limits.max_bytes} 字节",
            )

        if scope and not any(
            rel_out == item or rel_out.startswith(item + "/")
            for item in scope
        ):
            return _reject(
                path,
                f"输出不在 write_scope 内：{rel_out}。"
                f"允许写入：{', '.join(scope)}",
            )
        out_protected = protected_write_reason(
            rel_out, settings.write_deny_globs
        )
        if out_protected:
            return _reject(path, out_protected)
        try:
            out_info = access.stat(rel_out)
            out_exists = (
                bool(out_info.get("is_file"))
                if isinstance(out_info, dict)
                else bool(getattr(out_info, "is_file", False))
            )
        except Exception:  # noqa: BLE001 - 不存在是常态
            out_exists = False
        if out_exists:
            return _reject(
                path,
                f"输出文件已存在：{rel_out}；为避免覆盖请换一个新路径",
            )

        snapshot_id = ""
        try:
            if snapshots is not None:
                snapshot_id = snapshots.save(
                    out_target, "", existed=False
                )
            if fs is not None:
                fs.write_bytes(out_target, payload)
            else:
                target_path = Path(out_target)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(payload)
        except Exception as exc:  # noqa: BLE001
            return _error(path, f"写入失败：{type(exc).__name__}: {exc}")

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

    tools: list[BaseTool] = [
        StructuredTool.from_function(
            func=_read,
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
            func=_page_image,
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
                func=_edit,
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
