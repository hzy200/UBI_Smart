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
        "registerHostWorkspace",
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
        "projectProgressText",
        "projectBarStyle",
        "XMLHttpRequest",
        "_boundProjectId",
        "copyTranscriptMessage",
        "upsertToolMessage",
        "dedupeAssistantMessages",
        "tokenBuffers",
        "sessionWorkspaceLabel",
        "messageLabel",
        "markFinalAnswer",
        "saveTranscript",
        "restoreTranscript",
        "moveFinalAnswerLast",
        "loadRuntimeConfig",
        "validateRuntimeConfig",
        "config.invalid",
        "apiUrl",
        "checkVersion",
        "compareSemver",
        "min_frontend_version",
        "X-Frontend-Version",
        "version.frontend_too_old",
        "refreshPromise",
        "codingagent:auth-required",
        "bff-cookie",
        "X-CSRF-Token",
        "startLogin",
        "logout",
        "withCredentials",
        "ProblemError",
        "request_id=",
        "idleTimeoutMs",
        "SseIdleError",
        "Idempotency-Key",
        "installDesktopBridgeGuard",
        "window.top !== window",
    ):
        assert marker in text, marker


def test_index_contains_registry_section() -> None:
    text = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "项目（工作区注册表）" in text
    assert "capabilities.workspace_registry" in text
    assert "导入到 WSL 工作区" in text
    assert "添加本机工作区" in text
    assert "上传并创建项目" in text
    assert "取消任务" in text
    assert "onProjectDrop" in text
    assert "释放当前会话租约" in text
    assert "清理过期租约/幂等键" in text
    assert "恢复归档" in text
    assert 'aria-label="选择项目"' in text
    assert 'aria-label="项目归档文件"' in text
    assert 'class="progress-bar"' in text
    assert 'x-text="projectProgressText"' in text
    assert 'class="msg-copy"' in text
    assert 'class="inline-tool"' in text
    assert "copyTranscriptMessage(m)" in text
    assert "consent-card" in text
    assert 'class="session-group"' in text
    assert 'class="msg-badge"' in text
    assert 'id="boot"' in text
    assert 'id="config-error"' in text
    assert "configError" in text
    assert "configLoaded" in text
    assert 'id="version-error"' in text
    assert "versionChecked" in text
    assert "versionError" in text
    assert "authMode === 'bff-cookie'" in text
    assert "startLogin()" in text
    assert "logout()" in text
    assert "authModeLabel()" in text


def test_style_has_narrow_layout_and_focus() -> None:
    text = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "@media (max-width: 760px)" in text
    assert "focus-visible" in text
    assert "prefers-reduced-motion" in text


def test_layout_can_shrink_so_composer_stays_visible() -> None:
    """长对话时 transcript 自己滚动，不能把输入区挤出视口。"""
    text = (STATIC / "style.css").read_text(encoding="utf-8")
    assert "#app-root" in text
    assert "min-height: 0" in text
    assert "overflow-y: auto" in text
    assert "pointer-events: none" in text


def test_ide_shell_and_command_palette_are_guarded() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        'class="activitybar"',
        'class="statusbar"',
        'command-modal',
        'x-ref="commandInput"',
        ':style="layoutStyle()"',
        "startResize('sidebar'",
        "startResize('inspector'",
        "viewClasses.sessions",
        "currentSessionTitle()",
        "toggleSidebar()",
        "toggleInspector()",
        "activity-dot",
        "filesDirty",
        "tasksDirty",
    ):
        assert marker in html, marker

    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "initLayout",
        "setView",
        "toggleSidebar",
        "toggleInspector",
        "filteredCommands",
        "runCommand",
        "layoutStyle",
        "filesDirty",
        "tasksDirty",
    ):
        assert marker in app, marker

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for marker in (".activitybar", ".statusbar", ".command-box", ".splitter"):
        assert marker in css, marker
