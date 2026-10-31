"""跨文档归属校验。"""

from __future__ import annotations

from ubismart.knowledge.attribution import (
    document_aliases,
    find_attribution_issues,
)

_PAPERS = [
    {"path": "pdf/paper-1.pdf", "title": "DiffFNO"},
    {"path": "pdf/paper-6.pdf", "title": "Score-Based SDE"},
    {"path": "pdf/paper-10.pdf", "title": "WaveFace"},
    {"path": "pdf/UNet.pdf", "title": "U-Net"},
    {"path": "pdf/VMamba.pdf", "title": "VMamba"},
]


def test_document_aliases_include_title_and_stem() -> None:
    aliases = document_aliases(_PAPERS)

    assert aliases["difffno"] == "pdf/paper-1.pdf"
    assert aliases["paper1"] == "pdf/paper-1.pdf"
    assert aliases["unet"] == "pdf/UNet.pdf"
    assert "paper" not in aliases


def test_attribution_correct_path_has_no_issue() -> None:
    text = "DiffFNO 用傅里叶算子做超分 [来源: pdf/paper-1.pdf#page=3]。"
    assert find_attribution_issues(text, _PAPERS) == []


def test_attribution_wrong_path_is_flagged() -> None:
    text = "DiffFNO 用傅里叶算子做超分 [来源: pdf/paper-6.pdf#page=3]。"
    issues = find_attribution_issues(text, _PAPERS)

    assert len(issues) == 1
    assert issues[0]["mentioned"] == ["pdf/paper-1.pdf"]
    assert issues[0]["cited"] == ["pdf/paper-6.pdf"]


def test_attribution_multi_document_sentence_is_allowed() -> None:
    text = (
        "DiffFNO 与 paper-6 都使用扩散框架 "
        "[来源: pdf/paper-6.pdf#page=3]。"
    )
    assert find_attribution_issues(text, _PAPERS) == []


def test_attribution_paper1_does_not_match_paper10() -> None:
    text = "paper-10 做盲人脸修复 [来源: pdf/paper-10.pdf#page=1]。"
    assert find_attribution_issues(text, _PAPERS) == []


def test_attribution_uncited_mention_is_not_an_issue() -> None:
    assert find_attribution_issues("DiffFNO 没有引用。", _PAPERS) == []
