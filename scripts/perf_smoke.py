"""轻量性能冒烟：热路径出现数量级退化时让 CI 变红。

不是基准测试，也不承诺 p95；只给策略分类、终端清洗、内存限流设非常宽松
的上限，挡住“本来 0.2s 变成 20s”这类回归。阈值可在慢 runner 上用
`--factor` 放大。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from coding_agent.sandbox.policy import classify  # noqa: E402
from coding_agent.terminal_text import sanitize_terminal_text  # noqa: E402
from coding_agent.web.middleware import FixedWindowRateLimiter  # noqa: E402

BASE_LIMITS = {
    "classify_20k": 10.0,
    "sanitize_20k": 5.0,
    "rate_limit_100k": 5.0,
}


def _timed(func) -> float:
    started = time.perf_counter()
    func()
    return time.perf_counter() - started


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--factor", type=float, default=1.0, help="放宽阈值倍数（慢 runner）"
    )
    args = parser.parse_args()
    factor = max(1.0, float(args.factor))
    samples = [
        "ls -la",
        "git status --short",
        "rm -rf build",
        "command python3 -c 'print(1)'",
        "find . -name '*.py' -exec sh -c id \\;",
    ]

    def classify_loop() -> None:
        for _ in range(4000):
            for text in samples:
                classify(text)

    def sanitize_loop() -> None:
        payload = "a\x1b[31mred\x1b[0m\x1b]52;c;x\x07" * 4
        for _ in range(5000):
            sanitize_terminal_text(payload)

    def rate_loop() -> None:
        limiter = FixedWindowRateLimiter(per_minute=1_000_000)
        for index in range(100_000):
            limiter.allow(f"k{index % 1000}", now=float(index))

    measured = {
        "classify_20k": _timed(classify_loop),
        "sanitize_20k": _timed(sanitize_loop),
        "rate_limit_100k": _timed(rate_loop),
    }
    failures = {
        name: round(value, 3)
        for name, value in measured.items()
        if value > BASE_LIMITS[name] * factor
    }
    print(
        json.dumps(
            {
                "ok": not failures,
                "limits": {
                    name: round(limit * factor, 1)
                    for name, limit in BASE_LIMITS.items()
                },
                "measured": {
                    name: round(value, 3)
                    for name, value in measured.items()
                },
                "failures": failures,
            },
            ensure_ascii=False,
        )
    )
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
