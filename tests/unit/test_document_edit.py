"""document_edit：Office 文档的受限编辑。"""

from __future__ import annotations

from pathlib import Path

from ubismart.config import Settings
from ubismart.graph.nodes.approve import tool_level
from ubismart.sandbox.policy import CommandLevel
from ubismart.tools.artifacts import (
    DocumentEditArtifact,
    parse_artifact,
    unpack,
)
from ubismart.tools.documents import build_document_tools


def _settings() -> Settings:
    return Settings(_env_file=None)


def _tools(root: Path, **kwargs):
    return build_document_tools(_settings(), root=str(root), allow_write=True, **kwargs)


def _edit_tool(root: Path, **kwargs):
    return [t for t in _tools(root, **kwargs) if t.name == "document_edit"][0]


def _invoke(tool, **kwargs) -> tuple[str, dict]:
    raw = tool.invoke(kwargs)
    text, artifact = unpack(raw)
    assert artifact is not None
    return text, artifact


def _docx(path: Path, *paragraphs: str) -> None:
    from docx import Document

    document = Document()
    for text in paragraphs:
        document.add_paragraph(text)
    document.sections[0].header.paragraphs[0].text = "原页眉"
    document.save(path)


def test_docx_replace_append_and_header(tmp_path: Path) -> None:
    from docx import Document

    source = tmp_path / "a.docx"
    _docx(source, "旧内容", "第二段")
    tool = _edit_tool(tmp_path)
    text, artifact = _invoke(
        tool,
        path="a.docx",
        out_path="edited.docx",
        reason="测试编辑",
        operations=[
            {"op": "replace_text", "find": "旧内容", "replace": "新内容"},
            {"op": "append_paragraph", "text": "追加段落"},
            {"op": "set_header", "text": "新页眉"},
        ],
    )
    assert artifact["ok"] is True
    assert artifact["kind"] == "document_edit"
    assert artifact["action"] == "create"
    assert artifact["replacements"] == 1
    assert artifact["bytes_written"] > 0
    assert "edited.docx" in text
    parsed = parse_artifact(artifact)
    assert isinstance(parsed, DocumentEditArtifact)
    assert parsed.action == "create"

    edited = Document(tmp_path / "edited.docx")
    body = "\n".join(p.text for p in edited.paragraphs)
    assert "新内容" in body
    assert "旧内容" not in body
    assert "追加段落" in body
    assert edited.sections[0].header.paragraphs[0].text == "新页眉"


def test_docx_replace_across_runs(tmp_path: Path) -> None:
    from docx import Document

    document = Document()
    paragraph = document.add_paragraph()
    paragraph.add_run("旧")
    paragraph.add_run("内容")
    source = tmp_path / "runs.docx"
    document.save(source)

    tool = _edit_tool(tmp_path)
    _text, artifact = _invoke(
        tool,
        path="runs.docx",
        out_path="runs-out.docx",
        reason="跨 run 替换",
        operations=[{"op": "replace_text", "find": "旧内容", "replace": "新内容"}],
    )
    assert artifact["ok"] is True
    assert artifact["replacements"] == 1
    edited = Document(tmp_path / "runs-out.docx")
    assert edited.paragraphs[0].text == "新内容"


def test_xlsx_set_value_and_formula(tmp_path: Path) -> None:
    from openpyxl import Workbook, load_workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "数据"
    sheet["A1"] = "名称"
    sheet["B2"] = 1
    source = tmp_path / "book.xlsx"
    workbook.save(source)

    tool = _edit_tool(tmp_path)
    _text, artifact = _invoke(
        tool,
        path="book.xlsx",
        out_path="book-out.xlsx",
        reason="写公式",
        operations=[
            {
                "op": "set_cell",
                "sheet": "数据",
                "cell": "B2",
                "value": "3",
            },
            {
                "op": "set_cell",
                "sheet": "数据",
                "cell": "C2",
                "value": "B2*2",
                "formula": True,
            },
        ],
    )
    assert artifact["ok"] is True
    edited = load_workbook(tmp_path / "book-out.xlsx")
    assert edited["数据"]["B2"].value == 3
    assert edited["数据"]["C2"].value == "=B2*2"


def test_pptx_replace_and_add_text(tmp_path: Path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1))
    box.text_frame.text = "旧标题"
    source = tmp_path / "deck.pptx"
    presentation.save(source)

    tool = _edit_tool(tmp_path)
    _text, artifact = _invoke(
        tool,
        path="deck.pptx",
        out_path="deck-out.pptx",
        reason="替换标题",
        operations=[
            {"op": "replace_text", "find": "旧标题", "replace": "新标题"},
            {"op": "add_slide_text", "slide": 1, "text": "补充说明"},
        ],
    )
    assert artifact["ok"] is True
    assert artifact["replacements"] == 1

    edited = Presentation(tmp_path / "deck-out.pptx")
    texts = []
    for shape in edited.slides[0].shapes:
        if getattr(shape, "has_text_frame", False):
            texts.append(shape.text_frame.text)
    joined = "\n".join(texts)
    assert "新标题" in joined
    assert "补充说明" in joined


def test_rejects_same_or_existing_output(tmp_path: Path) -> None:
    source = tmp_path / "a.docx"
    _docx(source, "内容")
    tool = _edit_tool(tmp_path)
    operations = [{"op": "append_paragraph", "text": "x"}]

    text, artifact = _invoke(
        tool,
        path="a.docx",
        out_path="a.docx",
        reason="同路径",
        operations=operations,
    )
    assert artifact["ok"] is False
    assert artifact["rejected"] is True
    assert "不能与源文件相同" in text

    (tmp_path / "exists.docx").write_bytes(b"x")
    text, artifact = _invoke(
        tool,
        path="a.docx",
        out_path="exists.docx",
        reason="已存在",
        operations=operations,
    )
    assert artifact["ok"] is False
    assert artifact["rejected"] is True
    assert "已存在" in text


def test_rejects_macro_cross_format_and_bad_op(tmp_path: Path) -> None:
    source = tmp_path / "a.docx"
    _docx(source, "内容")
    (tmp_path / "macro.docm").write_bytes(b"x")
    tool = _edit_tool(tmp_path)

    text, artifact = _invoke(
        tool,
        path="macro.docm",
        out_path="macro-out.docm",
        reason="宏文档",
        operations=[{"op": "append_paragraph", "text": "x"}],
    )
    assert artifact["rejected"] is True
    assert "宏文档" in text

    text, artifact = _invoke(
        tool,
        path="a.docx",
        out_path="a.xlsx",
        reason="跨格式",
        operations=[{"op": "append_paragraph", "text": "x"}],
    )
    assert artifact["rejected"] is True
    assert "跨格式" in text

    text, artifact = _invoke(
        tool,
        path="a.docx",
        out_path="bad.docx",
        reason="错操作",
        operations=[{"op": "set_cell", "cell": "A1", "value": "1"}],
    )
    assert artifact["ok"] is False
    assert "不支持操作" in text


def test_rejects_traversal_write_scope_and_protected(tmp_path: Path) -> None:
    source = tmp_path / "a.docx"
    _docx(source, "内容")
    operations = [{"op": "append_paragraph", "text": "x"}]

    tool = _edit_tool(tmp_path)
    text, artifact = _invoke(
        tool,
        path="a.docx",
        out_path="../escape.docx",
        reason="越界",
        operations=operations,
    )
    assert artifact["rejected"] is True

    scoped = _edit_tool(tmp_path, write_scope=["allowed"])
    text, artifact = _invoke(
        scoped,
        path="a.docx",
        out_path="other.docx",
        reason="scope",
        operations=operations,
    )
    assert artifact["rejected"] is True
    assert "write_scope" in text

    _text, artifact = _invoke(
        scoped,
        path="a.docx",
        out_path="allowed/ok.docx",
        reason="scope ok",
        operations=operations,
    )
    assert artifact["ok"] is True

    text, artifact = _invoke(
        _edit_tool(tmp_path),
        path="a.docx",
        out_path=".git/evil.docx",
        reason="受保护",
        operations=operations,
    )
    assert artifact["rejected"] is True


def test_tool_is_l1_and_only_exposed_with_write(tmp_path: Path) -> None:
    assert tool_level("document_edit", {}, "") == CommandLevel.LOW_WRITE
    read_only = build_document_tools(_settings(), root=str(tmp_path))
    assert "document_edit" not in [tool.name for tool in read_only]
