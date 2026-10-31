"""前端错误摘要的落盘。

只追加 JSONL，超过上限时滚动截断，避免被异常页面刷爆磁盘。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
MAX_BYTES = 1_000_000


def append_client_error(path: Path, record: dict[str, Any]) -> None:
    line = json.dumps(record, ensure_ascii=False) + "\n"
    encoded = line.encode("utf-8")
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        if size + len(encoded) > MAX_BYTES:
            path.write_text("", encoding="utf-8")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
