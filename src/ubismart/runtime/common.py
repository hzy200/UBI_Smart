"""runtime 内部共享的低层出口。

只放 core 与 stream 都要用的东西，避免二者互相 import 形成环。
"""

from __future__ import annotations

import logging

from ubismart.platform.observability import note_silent_failure

_LOG = logging.getLogger("ubismart.runtime")

def _swallow_audit(site: str, exc: BaseException) -> None:
    """best-effort 审计/清理分支的统一出口：吞掉可以，不留痕不行。

    审计是承重不变量（哈希链 + "日志有 end 无 begin"就算验收缺陷）。写入失败
    时继续跑是对的（不该让主流程为审计陪葬），但必须计数并告警——否则
    "审计链断了"只在人工翻日志时才会被发现。
    """
    note_silent_failure(site)
    _LOG.warning("%s 的 best-effort 步骤失败，已吞掉：%s", site, exc)
