"""桌面进程使用的 loopback HTTP 助手。

独立于旧 `bootstrap`（同进程宿主模式），supervisor 只依赖这里。
"""

from __future__ import annotations

import time
import urllib.request


def local_open(request, timeout: float):
    """本地 loopback 请求显式绕过环境 HTTP(S)_PROXY。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(request, timeout=timeout)


def wait_health(url: str, timeout_s: float = 30.0) -> None:
    """WSL→Windows 的 loopback 转发在冷启动时可能慢几秒，给足重试时间。"""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            with local_open(url, 1.0) as response:
                if response.status == 200:
                    return
        except Exception:  # noqa: BLE001 - 启动阶段重试
            time.sleep(0.2)
    raise RuntimeError("桌面本地服务健康检查失败")
