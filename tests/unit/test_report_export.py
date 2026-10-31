"""report_export / report_check / 格式解析。"""

from __future__ import annotations

from pathlib import Path

from ubismart.config import Settings
from ubismart.tools.artifacts import unpack
from ubismart.tools.report import (
    REPORT_CHECK,
    REPORT_EXPORT,
    build_report_tools,
    resolve_output_format,
    resolve_output_formats,
    resolve_required_sections,
)


def _settings() -> Settings:
    return Settings(_env_file=None, report_output_dir="reports")


def _tools(root: Path) -> dict:
    return {
        tool.name: tool
        for tool in build_report_tools(_settings(), root=str(root), allow_write=True)
    }


def test_resolve_output_format_rules() -> None:
    assert resolve_output_format("写一份报告") == "md"
    assert resolve_output_format("写一份报告", "正式交付给客户") == "docx"
    assert resolve_output_format("输出 PDF") == "pdf"
    assert resolve_output_formats("输出 PDF 和 Word") == ["pdf", "docx"]


def test_export_md_docx_pdf(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    content = "# 标题\n\n正文\n\n- 一\n- 二\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    for fmt in ("md", "docx", "pdf"):
        raw = tools[REPORT_EXPORT].invoke(
            {
                "content": content,
                "out_path": f"reports/report.{fmt}",
                "reason": "test",
                "format": fmt,
                "title": "测试报告",
            }
        )
        text, artifact = unpack(raw)
        assert artifact and artifact["ok"] is True, text
        assert (tmp_path / "reports" / f"report.{fmt}").is_file()
        check_text, check = unpack(
            tools["report_check"].invoke(
                {"path": f"reports/report.{fmt}", "reason": "test"}
            )
        )
        assert check and check["ok"] is True, check_text

    pdf_text, pdf_artifact = unpack(
        tools[REPORT_EXPORT].invoke(
            {
                "content": "# 标题\n\n正文",
                "out_path": "reports/check-pages.pdf",
                "reason": "test",
                "format": "pdf",
            }
        )
    )
    assert pdf_artifact and pdf_artifact["ok"] is True, pdf_text
    assert pdf_artifact["pages"] > 0

    from docx import Document

    docx = Document(tmp_path / "reports" / "report.docx")
    texts = [paragraph.text for paragraph in docx.paragraphs]
    assert "标题" in texts
    assert "一" in texts
    assert docx.tables and docx.tables[0].cell(0, 0).text == "A"


def test_report_read_returns_report_text(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools[REPORT_EXPORT].invoke(
        {
            "content": "# 报告\n\n正文",
            "out_path": "reports/read.md",
            "reason": "test",
            "format": "md",
        }
    )
    text, artifact = unpack(
        tools["report_read"].invoke(
            {"path": "reports/read.md", "reason": "test"}
        )
    )
    assert artifact and artifact["ok"] is True
    assert "正文" in text


def test_export_uses_template_file(tmp_path: Path) -> None:
    template = tmp_path / ".agent" / "templates" / "report.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# {{title}}\n\n{{content}}\n\n---\n模板尾注\n", encoding="utf-8"
    )
    tools = _tools(tmp_path)
    text, artifact = unpack(
        tools[REPORT_EXPORT].invoke(
            {
                "content": "正文内容",
                "out_path": "reports/templated.md",
                "reason": "test",
                "format": "md",
                "title": "模板报告",
                "template": ".agent/templates/report.md",
            }
        )
    )
    assert artifact and artifact["ok"] is True, text
    rendered = (tmp_path / "reports" / "templated.md").read_text(
        encoding="utf-8"
    )
    assert rendered.startswith("# 模板报告")
    assert "正文内容" in rendered
    assert "模板尾注" in rendered


def test_report_check_validates_citation_anchors(tmp_path: Path) -> None:
    (tmp_path / "a.md").write_text("# 标题\n\n正文", encoding="utf-8")
    tools = _tools(tmp_path)
    tools[REPORT_EXPORT].invoke(
        {
            "content": "# 报告\n\n见 [来源](a.md#heading=1)。",
            "out_path": "reports/good.md",
            "reason": "test",
            "format": "md",
        }
    )
    _, good = unpack(
        tools["report_check"].invoke(
            {"path": "reports/good.md", "reason": "test"}
        )
    )
    assert good and good["ok"] is True

    tools[REPORT_EXPORT].invoke(
        {
            "content": "# 报告\n\n见 [来源](a.md#heading=99)。",
            "out_path": "reports/bad-cite.md",
            "reason": "test",
            "format": "md",
        }
    )
    text, bad = unpack(
        tools["report_check"].invoke(
            {"path": "reports/bad-cite.md", "reason": "test"}
        )
    )
    assert bad and bad["ok"] is False
    assert "引用锚点不存在" in text


def test_template_without_content_placeholder_is_rejected(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    text, artifact = unpack(
        tools[REPORT_EXPORT].invoke(
            {
                "content": "正文",
                "out_path": "reports/bad.md",
                "reason": "test",
                "format": "md",
                "template": "只有 {{title}}",
            }
        )
    )
    assert artifact and artifact["ok"] is False
    assert "{{content}}" in text


def test_export_outside_report_dir_is_rejected(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    text, artifact = unpack(
        tools[REPORT_EXPORT].invoke(
            {
                "content": "x",
                "out_path": "src/report.md",
                "reason": "test",
                "format": "md",
            }
        )
    )
    assert artifact and artifact["ok"] is False
    assert "reports/" in text


class _FakeSnapshots:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def write_with_snapshot(self, path, content, original, *, existed):
        self.calls.append(("write", path))
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return "snap-1"

    def save(self, path, original, *, existed=True):
        self.calls.append(("save", path))
        return "snap-2"


def test_export_uses_snapshot_and_records_it(tmp_path: Path) -> None:
    snapshots = _FakeSnapshots()
    tool = {
        item.name: item
        for item in build_report_tools(
            _settings(),
            root=str(tmp_path),
            allow_write=True,
            snapshots=snapshots,
        )
    }[REPORT_EXPORT]
    text, artifact = unpack(
        tool.invoke(
            {
                "content": "# x",
                "out_path": "reports/r.md",
                "reason": "test",
                "format": "md",
            }
        )
    )
    assert artifact and artifact["ok"] is True, text
    assert artifact["snapshot_id"] == "snap-1"
    assert snapshots.calls


def test_existing_binary_report_is_not_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "reports" / "r.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old")
    tool = {
        item.name: item
        for item in build_report_tools(
            _settings(),
            root=str(tmp_path),
            allow_write=True,
            snapshots=_FakeSnapshots(),
        )
    }[REPORT_EXPORT]
    text, artifact = unpack(
        tool.invoke(
            {
                "content": "# x",
                "out_path": "reports/r.docx",
                "reason": "test",
                "format": "docx",
            }
        )
    )
    assert artifact and artifact["ok"] is False
    assert "已存在" in text
    assert target.read_bytes() == b"old"


def test_pdf_without_font_reports_actionable_error(
    tmp_path: Path, monkeypatch
) -> None:
    import ubismart.tools.report as report_module

    monkeypatch.setattr(report_module, "_font_candidates", lambda settings: [])
    tools = _tools(tmp_path)
    text, artifact = unpack(
        tools[REPORT_EXPORT].invoke(
            {
                "content": "# x",
                "out_path": "reports/report.pdf",
                "reason": "test",
                "format": "pdf",
            }
        )
    )
    assert artifact and artifact["ok"] is False
    assert "AGENT_REPORT_FONT_PATH" in text


def test_resolve_required_sections_from_request() -> None:
    assert resolve_required_sections("报告需要结论和风险分析") == ["结论", "风险"]
    assert resolve_required_sections("写一份总结") == []


def test_report_check_required_sections_md(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    (reports / "r.md").write_text(
        "# 综合报告\n\n## 结论\n\n- 要点\n", encoding="utf-8"
    )
    text, artifact = unpack(
        tools[REPORT_CHECK].invoke(
            {
                "path": "reports/r.md",
                "reason": "检查章节",
                "required_sections": ["结论"],
            }
        )
    )
    assert artifact["ok"] is True, text

    text, artifact = unpack(
        tools[REPORT_CHECK].invoke(
            {
                "path": "reports/r.md",
                "reason": "检查章节",
                "required_sections": ["结论", "风险"],
            }
        )
    )
    assert artifact["ok"] is False
    assert any("缺少必需章节" in item for item in artifact["warnings"])


def test_report_check_required_sections_docx(tmp_path: Path) -> None:
    from docx import Document

    tools = _tools(tmp_path)
    reports = tmp_path / "reports"
    reports.mkdir()
    document = Document()
    document.add_heading("综合报告", level=1)
    document.add_heading("结论", level=2)
    document.add_paragraph("要点")
    document.save(reports / "r.docx")

    _text, artifact = unpack(
        tools[REPORT_CHECK].invoke(
            {
                "path": "reports/r.docx",
                "reason": "检查章节",
                "required_sections": ["结论"],
            }
        )
    )
    assert artifact["ok"] is True

    _text, artifact = unpack(
        tools[REPORT_CHECK].invoke(
            {
                "path": "reports/r.docx",
                "reason": "检查章节",
                "required_sections": ["风险"],
            }
        )
    )
    assert artifact["ok"] is False
