"""桌面 GUI：pywebview 窗口 + 本地 FastAPI 服务。

与 Web 共用同一套后端与前端；桌面模式只监听 loopback，并使用每次启动
生成的随机 token，避免本机其他进程直接调用 API。
"""

from ubismart.desktop.bootstrap import DesktopServer, run_desktop

__all__ = ["DesktopServer", "run_desktop"]
