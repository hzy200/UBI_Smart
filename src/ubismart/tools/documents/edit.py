"""Office 文档的受限编辑（纯 Python，无 LibreOffice）。

设计约束：
- 只接受 docx/xlsx/pptx，宏文档（docm/xlsm/pptm）与旧格式直接拒绝；
- 只在内存里改写，调用方负责把结果写到**新路径**，避免不可回滚的二进制覆盖；
- 不支持跨格式转换，输出后缀必须与源文件一致；
- 每次操作都有上限，失败时返回可操作错误，不静默丢内容。

编辑以对应库重新序列化为基础，因此可能丢失库不支持的少量对象
（例如 openpyxl 对部分图表/图片的保真度有限）；提示会明确说明。
"""

from __future__ import annotations

import io
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

from ubismart.tools.documents.models import DocumentError

DOCUMENT_EDIT = "document_edit"

EDITABLE_FORMATS = {".docx": "docx", ".xlsx": "xlsx", ".pptx": "pptx"}
MACRO_SUFFIXES = {
    ".docm", ".dotm", ".xlsm", ".xltm", ".pptm", ".potm", ".ppsm",
}
CELL_RE = re.compile(r"^[A-Za-z]{1,3}[1-9][0-9]*$")

DOCX_OPS = {"replace_text", "append_paragraph", "set_header", "set_footer"}
XLSX_OPS = {"set_cell"}
PPTX_OPS = {"replace_text", "add_slide_text"}
ALLOWED_OPS = {"docx": DOCX_OPS, "xlsx": XLSX_OPS, "pptx": PPTX_OPS}


class DocumentEditOperation(BaseModel):
    """一条编辑操作；不同 `op` 使用的字段不同。"""

    op: Literal[
        "replace_text",
        "append_paragraph",
        "set_header",
        "set_footer",
        "set_cell",
        "add_slide_text",
    ] = Field(description="操作类型")
    find: str = Field(default="", description="replace_text 要查找的文本")
    replace: str = Field(default="", description="replace_text 的替换文本")
    text: str = Field(
        default="",
        description="追加段落/页眉页脚/幻灯片文本框的文本",
    )
    limit: int = Field(
        default=0, ge=0, description="replace_text 最多替换几处；0 表示全部"
    )
    sheet: str = Field(default="", description="set_cell 的工作表名；留空=活动表")
    cell: str = Field(default="", description='set_cell 的单元格，例如 "B2"')
    value: str = Field(default="", description="set_cell 的值")
    formula: bool = Field(
        default=False, description="set_cell 是否把 value 当作公式"
    )
    slide: int = Field(
        default=0, ge=0, description="add_slide_text 的页码（从 1 开始）"
    )


class DocumentEditInput(BaseModel):
    path: str = Field(description="工作区内源文档的相对路径")
    out_path: str = Field(
        description="工作区内输出路径，必须与源格式相同且指向一个新文件"
    )
    reason: str = Field(description="编辑文档的意图，一句话说明")
    operations: list[DocumentEditOperation] = Field(
        min_length=1, description="按顺序执行的操作列表"
    )


def normalize_out_path(path: str) -> str:
    rel = str(path or "").replace("\\", "/").strip()
    while rel.startswith("./"):
        rel = rel[2:]
    rel = rel.lstrip("/")
    if not rel:
        raise DocumentError("输出路径不能为空")
    if "\x00" in rel or ".." in rel.split("/"):
        raise DocumentError("输出路径必须是工作区内的相对路径")
    return rel


def validate_paths(path: str, out_path: str) -> tuple[str, str, str]:
    """校验源/输出路径，返回 (rel_out, format, display)。"""
    source = str(path or "").replace("\\", "/").strip()
    rel_out = normalize_out_path(out_path)
    source_suffix = _suffix(source)
    out_suffix = _suffix(rel_out)
    if source_suffix in MACRO_SUFFIXES or out_suffix in MACRO_SUFFIXES:
        raise DocumentError("不支持宏文档（docm/xlsm/pptm 等），请另存为普通格式")
    if source_suffix not in EDITABLE_FORMATS:
        raise DocumentError(
            "document_edit 只支持 .docx/.xlsx/.pptx；"
            f"当前是 {source_suffix or '（无后缀）'}"
        )
    if out_suffix != source_suffix:
        raise DocumentError(
            f"不支持跨格式转换：源 {source_suffix}，输出 {out_suffix}"
        )
    if _norm(source) == _norm(rel_out):
        raise DocumentError("输出路径不能与源文件相同；请指定一个新文件")
    return rel_out, EDITABLE_FORMATS[source_suffix], source


def _suffix(path: str) -> str:
    text = str(path or "").replace("\\", "/").strip().lower()
    index = text.rfind(".")
    return text[index:] if index >= 0 else ""


def _norm(path: str) -> str:
    return str(path or "").replace("\\", "/").strip().lstrip("./").casefold()


def apply_edits(
    path: str, data: bytes, operations: list[DocumentEditOperation]
) -> tuple[bytes, int, list[str]]:
    """按后缀分派编辑；返回 (新字节, 替换次数, 警告)。"""
    fmt = EDITABLE_FORMATS.get(_suffix(path))
    if fmt is None:
        raise DocumentError(f"不支持编辑：{path}")
    allowed = ALLOWED_OPS[fmt]
    for index, op in enumerate(operations, 1):
        if op.op not in allowed:
            names = "、".join(sorted(allowed))
            raise DocumentError(
                f"{fmt} 不支持操作 {op.op}（第 {index} 条）；可用：{names}"
            )
    if fmt == "docx":
        return _docx_edit(data, operations)
    if fmt == "xlsx":
        return _xlsx_edit(data, operations)
    return _pptx_edit(data, operations)


# ---------------------------------------------------------------------------
# 通用文本替换
# ---------------------------------------------------------------------------


def _replace_in_paragraphs(
    paragraphs: list[Any], find: str, replace: str, limit: int
) -> int:
    budget = 0 if limit <= 0 else limit
    total = 0
    for paragraph in paragraphs:
        if budget and total >= budget:
            break
        remaining = 0 if not budget else budget - total
        total += _replace_in_paragraph(paragraph, find, replace, remaining)
    return total


def _replace_in_runs(
    runs: list[Any], find: str, replace: str, budget: int
) -> int:
    """在单个 run 内替换，返回已替换次数。"""
    count = 0
    for run in runs:
        if budget and count >= budget:
            break
        if not run.text or find not in run.text:
            continue
        occurrences = run.text.count(find)
        allowed = occurrences if not budget else min(occurrences, budget - count)
        if allowed <= 0:
            continue
        run.text = run.text.replace(find, replace, allowed)
        count += allowed
    return count


def _replace_across_runs(
    runs: list[Any], find: str, replace: str, budget: int, count: int
) -> int:
    """run 级替换后处理跨 run 命中，整段重建并保留首个 run 的样式。"""
    full = "".join(run.text for run in runs)
    if find not in full:
        return count
    if budget and count >= budget:
        return count
    occurrences = full.count(find)
    allowed = occurrences if not budget else min(occurrences, budget - count)
    if allowed <= 0:
        return count
    runs[0].text = full.replace(find, replace, allowed)
    for run in runs[1:]:
        run.text = ""
    return count + allowed


def _replace_in_paragraph(
    paragraph: Any, find: str, replace: str, limit: int
) -> int:
    """先按 run 替换以保留格式；跨 run 命中时退回整段重写。"""
    if not find:
        return 0
    runs = list(getattr(paragraph, "runs", []))
    if not runs:
        return 0
    budget = 0 if limit <= 0 else limit
    count = _replace_in_runs(runs, find, replace, budget)
    return _replace_across_runs(runs, find, replace, budget, count)


# ---------------------------------------------------------------------------
# DOCX
# ---------------------------------------------------------------------------


def _docx_edit(
    data: bytes, operations: list[DocumentEditOperation]
) -> tuple[bytes, int, list[str]]:
    try:
        import docx
    except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可操作提示
        raise DocumentError(
            "缺少 python-docx。请安装：pip install 'ubismart[documents]'"
        ) from exc

    document = docx.Document(io.BytesIO(data))
    replacements = 0
    warnings: list[str] = []
    for op in operations:
        if op.op == "replace_text":
            if not op.find:
                raise DocumentError("replace_text 需要 find")
            replacements += _replace_in_paragraphs(
                list(_docx_paragraphs(document)),
                op.find,
                op.replace,
                op.limit,
            )
        elif op.op == "append_paragraph":
            document.add_paragraph(op.text)
        elif op.op in {"set_header", "set_footer"}:
            _set_docx_section_text(document, op.op, op.text)
        else:  # validate_paths 已拦截
            raise DocumentError(f"docx 不支持操作：{op.op}")
    buffer = io.BytesIO()
    document.save(buffer)
    warnings.append("DOCX 编辑会重新序列化文档，复杂版式/嵌入对象可能被库简化")
    return buffer.getvalue(), replacements, warnings


def _docx_paragraphs(document: Any) -> Any:
    yield from document.paragraphs
    for table in document.tables:
        yield from _docx_table_paragraphs(table)
    for section in document.sections:
        for container in (section.header, section.footer):
            yield from container.paragraphs


def _docx_table_paragraphs(table: Any) -> Any:
    for row in table.rows:
        for cell in row.cells:
            yield from cell.paragraphs
            for nested in cell.tables:
                yield from _docx_table_paragraphs(nested)


def _set_docx_section_text(document: Any, kind: str, text: str) -> None:
    if not document.sections:
        raise DocumentError("文档没有分节，无法设置页眉/页脚")
    section = document.sections[0]
    container = section.header if kind == "set_header" else section.footer
    paragraphs = container.paragraphs
    if paragraphs:
        paragraphs[0].text = text
        for extra in paragraphs[1:]:
            extra.text = ""
    else:
        container.add_paragraph(text)


# ---------------------------------------------------------------------------
# XLSX
# ---------------------------------------------------------------------------


def _xlsx_edit(
    data: bytes, operations: list[DocumentEditOperation]
) -> tuple[bytes, int, list[str]]:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover
        raise DocumentError(
            "缺少 openpyxl。请安装：pip install 'ubismart[documents]'"
        ) from exc

    workbook = load_workbook(io.BytesIO(data))
    for op in operations:
        if op.op != "set_cell":
            raise DocumentError(f"xlsx 不支持操作：{op.op}")
        if not CELL_RE.match(str(op.cell or "").strip()):
            raise DocumentError(f"无效单元格：{op.cell!r}")
        if op.sheet:
            if op.sheet not in workbook.sheetnames:
                raise DocumentError(f"工作表不存在：{op.sheet}")
            worksheet = workbook[op.sheet]
        else:
            worksheet = workbook.active
        ref = str(op.cell).strip().replace("$", "").upper()
        try:
            cell = worksheet[ref]
        except Exception as exc:  # noqa: BLE001
            raise DocumentError(f"无法定位单元格 {ref}：{exc}") from exc
        if op.formula:
            cell.value = (
                op.value if str(op.value).startswith("=") else f"={op.value}"
            )
        else:
            cell.value = _coerce_value(op.value)
    buffer = io.BytesIO()
    workbook.save(buffer)
    warnings = [
        "XLSX 编辑会重新序列化工作簿，图表/条件格式等对象可能被简化"
    ]
    return buffer.getvalue(), 0, warnings


def _coerce_value(value: str) -> Any:
    text = str(value)
    lowered = text.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    for cast in (int, float):
        try:
            return cast(text)
        except (TypeError, ValueError):
            continue
    return text


# ---------------------------------------------------------------------------
# PPTX
# ---------------------------------------------------------------------------


def _pptx_edit(
    data: bytes, operations: list[DocumentEditOperation]
) -> tuple[bytes, int, list[str]]:
    try:
        from pptx import Presentation
        from pptx.util import Inches
    except ImportError as exc:  # pragma: no cover
        raise DocumentError(
            "缺少 python-pptx。请安装：pip install 'ubismart[documents]'"
        ) from exc

    presentation = Presentation(io.BytesIO(data))
    replacements = 0
    for op in operations:
        if op.op == "replace_text":
            if not op.find:
                raise DocumentError("replace_text 需要 find")
            replacements += _replace_in_paragraphs(
                list(_pptx_paragraphs(presentation)),
                op.find,
                op.replace,
                op.limit,
            )
        elif op.op == "add_slide_text":
            total = len(presentation.slides)
            if op.slide < 1 or op.slide > total:
                raise DocumentError(f"幻灯片越界：{op.slide}，共 {total} 页")
            slide = presentation.slides[op.slide - 1]
            box = slide.shapes.add_textbox(
                Inches(1), Inches(1), Inches(6), Inches(1.2)
            )
            box.text_frame.word_wrap = True
            box.text_frame.text = op.text
        else:  # pragma: no cover - 已在校验层拦截
            raise DocumentError(f"pptx 不支持操作：{op.op}")
    buffer = io.BytesIO()
    presentation.save(buffer)
    warnings = [
        "PPTX 编辑会重新序列化演示文稿，动画/切换效果等对象可能被简化"
    ]
    return buffer.getvalue(), replacements, warnings


def _pptx_paragraphs(presentation: Any) -> Any:
    for slide in presentation.slides:
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False):
                yield from shape.text_frame.paragraphs
            if getattr(shape, "has_table", False):
                for row in shape.table.rows:
                    for cell in row.cells:
                        yield from cell.text_frame.paragraphs
