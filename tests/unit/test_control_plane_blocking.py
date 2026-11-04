"""控制面禁止清单的 CI 静态检查。

文档 P5：控制面事件循环不得做同步 HTTP、subprocess、sleep、join 或跨步锁。
这里只扫承重的控制面模块；工具/沙箱本身是执行面，允许同步调用（由
`asyncio.to_thread` 下放）。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / "src" / "ubismart"

CONTROL_PLANE_FILES = (
    "runtime/stream.py",
    "runtime/audit_bridge.py",
    "runtime/event_map.py",
    "graph/nodes/steering.py",
    "orchestrator/scheduler_run.py",
    "orchestrator/plan.py",
    "orchestrator/plan_review.py",
    "orchestrator/budget.py",
    "orchestrator/workflow.py",
)

FORBIDDEN_CALLS = {
    "subprocess.run",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "subprocess.Popen",
    "os.system",
    "os.popen",
    "requests.get",
    "requests.post",
    "requests.put",
    "requests.delete",
    "requests.patch",
    "requests.request",
    "urllib.request.urlopen",
    "time.sleep",
    # 跨步锁：控制面只允许在检查点提交的毫秒级临界区内持锁，
    # 这些承重模块不应直接构造文件锁。
    "filelock.FileLock",
    "ubismart.platform.filelock.FileLock",
}


def _qualified_name(node: ast.AST) -> str:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _qualified_name(node.func)
        if name in FORBIDDEN_CALLS:
            found.append(f"{path.name}:{node.lineno} 调用了 {name}")
            continue
        # 线程 join：只拦截名字里带 thread/worker 的接收者，避免误伤 str.join。
        if isinstance(node.func, ast.Attribute) and node.func.attr == "join":
            receiver = _qualified_name(node.func.value).lower()
            if "thread" in receiver or "worker" in receiver:
                found.append(
                    f"{path.name}:{node.lineno} 调用了阻塞 join：{name}"
                )
    return found


def test_control_plane_has_no_known_blocking_calls() -> None:
    violations: list[str] = []
    for relative in CONTROL_PLANE_FILES:
        path = ROOT / relative
        assert path.exists(), f"控制面文件不存在：{path}"
        violations.extend(_violations(path))
    assert not violations, "控制面出现阻塞调用：\n" + "\n".join(violations)
