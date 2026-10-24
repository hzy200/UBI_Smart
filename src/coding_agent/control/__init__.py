"""A2 控制面（Phase B）。

边界：只管理业务数据与路由，不执行工具、不直接读写工作区。
Standalone 不导入本包；只有配置了 A2 数据源或显式使用控制面时才加载。
"""

from coding_agent.control.store import ControlStore

__all__ = ["ControlStore"]
