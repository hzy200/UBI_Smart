"""文档管线性能/容量基准与 CI 门槛。

手工基准：
    python scripts/bench_documents.py --pages 200 --docs 8

CI 门槛（小规模、数量级回归才红）：
    python scripts/bench_documents.py --check --pages 20 --docs 2 --factor 2

覆盖：
- 单份大 PDF 逐页文本提取；
- 多份 PDF 并发解析；
- 峰值内存（tracemalloc）；
- 缓存命中后的二次解析耗时；
- `--check` 时按页数放宽的耗时/内存阈值。

依赖可选 extra：pip install 'ubismart[documents]'。
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
import time
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ubismart.tools.documents import DocumentLimits, parse_document

# 数量级回归阈值；CI 用 --pages 20 --docs 2，慢 runner 用 --factor 放宽。
MAX_SECONDS_PER_PAGE = 0.5
MIN_LARGE_SECONDS = 5.0
MAX_LARGE_PEAK_MB = 256.0
MAX_CONCURRENT_SECONDS = 20.0
MAX_SECOND_PASS_FACTOR = 2.0


def _make_pdf(pages: int, label: str) -> bytes:
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer)
    for page in range(1, pages + 1):
        pdf.drawString(72, 720, f"{label} page {page}")
        pdf.drawString(72, 700, "lorem ipsum " * 20)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _timed(label: str, fn) -> dict:
    started = time.perf_counter()
    value = fn()
    return {
        "label": label,
        "seconds": round(time.perf_counter() - started, 3),
        "value": value,
    }


def build_report(pages: int, docs: int) -> dict[str, Any]:
    limits = DocumentLimits(pdf_tables=True, pdf_text_only=True)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        payload = _make_pdf(pages, "large")
        large = root / "large.pdf"
        large.write_bytes(payload)

        tracemalloc.start()
        result = _timed(
            "large_pdf",
            lambda: parse_document("large.pdf", payload, limits=limits),
        )
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        model = result.pop("value")
        result.update(
            {
                "pages": model.pages,
                "blocks": len(model.blocks),
                "chars": sum(len(block.text) for block in model.blocks),
                "peak_mb": round(peak / 1024 / 1024, 2),
            }
        )

        documents = []
        for index in range(docs):
            data = _make_pdf(30, f"doc-{index}")
            documents.append((f"doc-{index}.pdf", data))
        tracemalloc.start()
        started = time.perf_counter()
        workers = max(1, min(4, docs))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            parsed = list(
                pool.map(
                    lambda item: parse_document(item[0], item[1], limits=limits),
                    documents,
                )
            )
        concurrent_seconds = round(time.perf_counter() - started, 3)
        _current, concurrent_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()

        second = _timed(
            "large_pdf_second_pass",
            lambda: parse_document("large.pdf", payload, limits=limits),
        )
        second.pop("value", None)

        return {
            "config": {"pages": pages, "documents": docs},
            "large_pdf": result,
            "concurrent": {
                "documents": len(parsed),
                "seconds": concurrent_seconds,
                "peak_mb": round(concurrent_peak / 1024 / 1024, 2),
            },
            "second_pass": second,
            "note": "第二次解析仍会重新解析；document_read 工具层才会命中磁盘缓存。",
        }


def evaluate(report: dict[str, Any], factor: float = 1.0) -> dict[str, str]:
    """返回超过阈值的项；空字典表示通过。"""
    factor = max(1.0, float(factor))
    pages = max(1, int(report.get("config", {}).get("pages", 1)))
    large = report.get("large_pdf", {})
    concurrent = report.get("concurrent", {})
    second = report.get("second_pass", {})
    failures: dict[str, str] = {}

    large_seconds = float(large.get("seconds", 0.0))
    large_limit = max(MIN_LARGE_SECONDS, pages * MAX_SECONDS_PER_PAGE) * factor
    if large_seconds > large_limit:
        failures["large_pdf_seconds"] = (
            f"{large_seconds:.3f}s > {large_limit:.3f}s"
        )

    peak = float(large.get("peak_mb", 0.0))
    if peak > MAX_LARGE_PEAK_MB * factor:
        failures["large_pdf_peak_mb"] = (
            f"{peak:.2f}MB > {MAX_LARGE_PEAK_MB * factor:.2f}MB"
        )

    concurrent_seconds = float(concurrent.get("seconds", 0.0))
    if concurrent_seconds > MAX_CONCURRENT_SECONDS * factor:
        failures["concurrent_seconds"] = (
            f"{concurrent_seconds:.3f}s > "
            f"{MAX_CONCURRENT_SECONDS * factor:.3f}s"
        )

    second_seconds = float(second.get("seconds", 0.0))
    second_limit = large_seconds * MAX_SECOND_PASS_FACTOR + 2.0
    if second_seconds > second_limit:
        failures["second_pass_seconds"] = (
            f"{second_seconds:.3f}s > {second_limit:.3f}s"
        )
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pages", type=int, default=200)
    parser.add_argument("--docs", type=int, default=8)
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--check", action="store_true", help="超过阈值时返回非零退出码"
    )
    parser.add_argument(
        "--factor", type=float, default=1.0, help="放宽阈值倍数（慢 runner）"
    )
    args = parser.parse_args()

    report = build_report(max(1, args.pages), max(1, args.docs))
    failures = evaluate(report, args.factor)
    payload: dict[str, Any] = dict(report)
    payload["check"] = {
        "ok": not failures,
        "factor": max(1.0, float(args.factor)),
        "failures": failures,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if args.check and failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
