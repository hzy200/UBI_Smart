"""reasoning 流是 best-effort：有就展示，没有不伪造。"""

from __future__ import annotations

from langchain_core.messages import AIMessage

from coding_agent.messages import reasoning_of


def test_reasoning_from_additional_kwargs() -> None:
    message = AIMessage(
        content="最终答复",
        additional_kwargs={"reasoning_content": "先分析再回答"},
    )
    assert reasoning_of(message) == "先分析再回答"


def test_reasoning_from_content_blocks() -> None:
    message = AIMessage(
        content=[
            {"type": "thinking", "text": "想一想"},
            {"type": "text", "text": "答案"},
        ]
    )
    assert reasoning_of(message) == "想一想"


def test_no_reasoning_returns_empty() -> None:
    assert reasoning_of(AIMessage(content="只有答案")) == ""
