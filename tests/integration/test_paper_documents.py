"""多篇英文论文的联合阅读评测（真实 WSL + 真实模型）。

论文数量由 `pdf/` 目录自动发现，不写死三篇；新增/删除 PDF 后用例会
自动扩展/收缩。默认 `-m "not llm"` 会跳过；显式运行：

```bash
pytest -m llm tests/integration/test_paper_documents.py -q
pytest -m llm tests/integration/test_paper_documents.py -q -k cross_method
```

判定在 `tests/eval/papers.py`：覆盖全部文档、每篇都有证据、答案引用每篇、
已知论文的概念齐全、长度受限，并通过确定性 judge。单次运行有模型
非确定性，失败时应结合 `reasons` 判断，而不是直接归因于功能回归。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"
if str(EVAL_DIR) not in sys.path:
    sys.path.insert(0, str(EVAL_DIR))

from papers import (  # noqa: E402
    build_cases,
    cases_by_id,
    discover_papers,
    run_case,
)

PAPERS = discover_papers()
CASES = build_cases(PAPERS)

pytestmark = [pytest.mark.llm]


def _skip_unless_ready() -> None:
    if not PAPERS:
        pytest.skip("未提供 pdf/ 语料目录")
    for paper in PAPERS:
        if not Path(str(paper["source"])).is_file():
            pytest.skip(f"缺少评测语料：{paper['source']}")

    from ubismart.config import get_settings

    settings = get_settings()
    if not settings.deepseek_api_key and not settings.llm_providers:
        pytest.skip("未配置 DEEPSEEK_API_KEY 或 AGENT_LLM_PROVIDERS")

    from ubismart.sandbox.backend import resolve_execution_backend

    if resolve_execution_backend(settings) != "local":
        from ubismart.sandbox.wsl_exec import WslSandbox

        if not WslSandbox.available(settings.wsl_distro):
            pytest.skip(f"WSL 发行版 {settings.wsl_distro} 不可用")


@pytest.mark.parametrize("case_id", [case["id"] for case in CASES] or [None])
def test_cross_document_case(case_id: str | None) -> None:
    if case_id is None:
        pytest.skip("没有可用的 PDF 用例")
    _skip_unless_ready()
    case = cases_by_id(PAPERS)[case_id]
    report = run_case(case)

    assert report["coverage_complete"], report.get("coverage")
    assert report["passed"], {
        "reasons": report["reasons"],
        "concepts": report.get("concepts"),
        "sections": report.get("sections"),
        "per_doc_sources": report.get("per_doc_sources"),
        "judgement": report.get("judgement"),
    }
