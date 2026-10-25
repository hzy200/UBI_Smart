"""失败重试上下文：只给有用的信息，且必须有上限。"""

from __future__ import annotations

from coding_agent.orchestrator.retry import format_retry_context


def test_empty_context_returns_empty_string() -> None:
    assert format_retry_context({}) == ""
    assert format_retry_context({"retry_context": None}) == ""
    assert format_retry_context({"retry_context": {}}) == ""
    assert format_retry_context({"retry_context": "not-a-dict"}) == ""


def test_full_context_is_rendered() -> None:
    text = format_retry_context(
        {
            "retry_context": {
                "attempt": 2,
                "status": "failed",
                "summary": "测试失败",
                "error": "AssertionError",
                "findings": ["缺少导入", "路径写错"],
                "verification": {
                    "issues": [
                        {"location": "src/a.py:3", "message": "未定义 x"},
                        {"location": "", "message": "空位置也要保留"},
                    ]
                },
            }
        }
    )

    assert "第 3 次尝试" in text
    assert "上次状态：failed" in text
    assert "上次摘要：测试失败" in text
    assert "上次错误：AssertionError" in text
    assert "缺少导入" in text
    assert "src/a.py:3 未定义 x" in text
    assert "空位置也要保留" in text


def test_attempt_defaults_to_first_retry() -> None:
    text = format_retry_context({"retry_context": {"status": "failed"}})
    assert "第 2 次尝试" in text


def test_empty_fields_are_skipped() -> None:
    text = format_retry_context(
        {
            "retry_context": {
                "status": "  ",
                "summary": "",
                "error": "",
                "findings": [],
                "verification": {"issues": []},
            }
        }
    )
    assert "上次状态" not in text
    assert "上次摘要" not in text
    assert "上次发现" not in text
    assert "上次验证问题" not in text


def test_long_fields_are_truncated_and_capped() -> None:
    text = format_retry_context(
        {
            "retry_context": {
                "summary": "s" * 700,
                "error": "e" * 700,
                "findings": [f"f{index}-" + "x" * 400 for index in range(10)],
                "verification": {
                    "issues": [
                        {"location": f"p{index}", "message": "m"}
                        for index in range(12)
                    ]
                },
            }
        }
    )

    assert "s" * 600 in text
    assert "s" * 601 not in text
    assert "e" * 600 in text
    assert "e" * 601 not in text
    assert text.count("上次发现") == 1
    assert "f5-" in text
    assert "f6-" not in text
    assert "p7" in text
    assert "p8" not in text
