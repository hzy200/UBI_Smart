"""前端构建产物包含工作区注册表流程（M5）。

这是构建产物守卫：改了 `frontend/src` 却忘记重新生成 `static/app.js`
时，界面不会静默退回旧版本。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "src" / "coding_agent" / "web" / "static"


def test_app_js_contains_workspace_flow() -> None:
    text = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "loadProjects",
        "importLocalProject",
        "uploadProject",
        "pollProjectJob",
        "rollbackRevision",
        "pinRevision",
        "pruneRevisions",
        "cancelProjectJob",
        "onProjectDrop",
        "releaseLease",
        "purgeMaintenance",
        "restoreRevision",
    ):
        assert marker in text, marker


def test_index_contains_registry_section() -> None:
    text = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "项目（工作区注册表）" in text
    assert "capabilities.workspace_registry" in text
    assert "导入本地文件夹" in text
    assert "上传并创建项目" in text
    assert "取消任务" in text
    assert "onProjectDrop" in text
    assert "释放当前会话租约" in text
    assert "清理过期租约/幂等键" in text
    assert "恢复归档" in text
