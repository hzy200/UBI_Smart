"""文档压测脚本的阈值判定与最小可运行性。"""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "bench_documents", ROOT / "scripts" / "bench_documents.py"
)
assert _SPEC is not None and _SPEC.loader is not None
bench = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(bench)


def _report(**overrides):
    report = {
        "config": {"pages": 20, "documents": 2},
        "large_pdf": {"seconds": 1.0, "peak_mb": 20.0},
        "concurrent": {"seconds": 2.0},
        "second_pass": {"seconds": 1.5},
    }
    report.update(overrides)
    return report


def test_evaluate_passes_within_thresholds() -> None:
    assert bench.evaluate(_report()) == {}


def test_evaluate_detects_regressions() -> None:
    failures = bench.evaluate(
        _report(
            large_pdf={"seconds": 999.0, "peak_mb": 999.0},
            concurrent={"seconds": 999.0},
        )
    )
    assert "large_pdf_seconds" in failures
    assert "large_pdf_peak_mb" in failures
    assert "concurrent_seconds" in failures

    # 二次解析按相对首轮退化判定（首轮 1s，二轮上限 4s）。
    second = bench.evaluate(_report(second_pass={"seconds": 999.0}))
    assert "second_pass_seconds" in second


def test_evaluate_factor_relaxes_limits() -> None:
    slow = _report(large_pdf={"seconds": 12.0, "peak_mb": 300.0})
    assert bench.evaluate(slow) != {}
    assert bench.evaluate(slow, factor=2.0) == {}


def test_build_report_smoke() -> None:
    report = bench.build_report(pages=1, docs=1)
    assert report["large_pdf"]["pages"] >= 1
    assert report["concurrent"]["documents"] == 1
    assert report["second_pass"]["seconds"] >= 0
