"""runtime 拆包后的公开面。

`coding_agent.runtime` 是前端与测试共同依赖的导入路径。把它从单文件拆成
包时，路径与符号必须保持不变；这里锁住那些**外部确实在用**的名字，
防止拆分过程中出现"文件搬走了、导入静默失效"。
"""

from __future__ import annotations


def test_runtime_public_symbols_are_importable() -> None:
    from coding_agent.runtime import (
        AgentRuntime,
        HistoryMessage,
        _verification_verdict,
        submit_steering_command,
    )

    assert AgentRuntime.__name__ == "AgentRuntime"
    assert HistoryMessage(role="user", text="hi").text == "hi"
    assert callable(submit_steering_command)
    assert _verification_verdict({"status": "ok"}) is True
    assert _verification_verdict({"status": "failed"}) is False
    assert _verification_verdict({"status": "skipped"}) is None
