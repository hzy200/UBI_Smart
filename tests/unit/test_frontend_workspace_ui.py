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
    assert 'data-op="copy"' in text
    assert "consent-card" in text
    assert 'class="session-group"' in text
    assert 'class="msg-badge"' in text
    assert 'id="boot"' in text
    assert 'id="config-error"' in text
    assert "configError" in text
    assert "bootVisible" in text
    assert 'id="version-error"' in text
    assert "versionError" in text
    assert "loginVisible" in text
    assert "authBffVisible" in text
    assert "startLogin" in text
    assert "logout" in text
    assert "authModeText" in text


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
        ':style="layoutStyle"',
        'data-side="sidebar"',
        'data-side="inspector"',
        "viewClasses.sessions",
        "currentSessionTitle",
        "toggleSidebar",
        "toggleInspector",
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


def test_m3_workspace_features_are_guarded() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        "mainTabFlags.file",
        "mainTabFlags.diff",
        "diffRowsData",
        "fileView._html",
        "notification-panel",
        "hasUnreadNotifications",
        "tabClasses.timeline",
        "timeline-item",
    ):
        assert marker in html, marker

    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "diffRows",
        "highlightCode",
        "onCommandQuery",
        "searchFiles",
        "fileSearchResults",
        "pushNotification",
        "pushTimeline",
        "notifications",
    ):
        assert marker in app, marker

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for marker in (".editor-code", ".diff-add", ".notification-panel", ".timeline-item"):
        assert marker in css, marker


def test_m4_conversation_and_tools_are_guarded() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        "messageSearchOpen",
        "data-message-id",
        'data-op="edit"',
        "regenerate",
        "openMessageSearch",
        "jump-latest",
        "filteredToolResults",
        "toolFilterClasses",
        "tool-detail",
        "filteredTreeResults",
        "openFileInVscode",
        "usageLabel",
    ):
        assert marker in html, marker

    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "searchMessages",
        "editMessage",
        "regenerate",
        "onTranscriptScroll",
        "filteredTools",
        "refreshToolDetail",
        "filteredTreeRows",
        "openFileInVscode",
        "usageText",
    ):
        assert marker in app, marker

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for marker in (".main-search", ".jump-latest", ".msg-hit", ".tool-detail", ".tree-copy"):
        assert marker in css, marker


def test_m5_windowing_and_a11y_are_guarded() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        "virtualMessageResults",
        "virtualTop",
        "virtualBottom",
        "virtual-spacer",
        "transcriptClassValue",
        'aria-selected',
        'aria-pressed',
        'aria-activedescendant',
        'role="status"',
        'aria-controls="sidebar-panel"',
        'aria-controls="inspector-panel"',
    ):
        assert marker in html, marker

    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "virtualMessages",
        "computeVirtualRange",
        "scheduleVirtualRange",
        "recomputeVirtualRange",
        "onMessageMeasured",
        "messageHeights",
        "diffRowsFromText",
        "parseCommandQuery",
        "includesQuery",
    ):
        assert marker in app, marker

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    assert ".virtual-spacer" in css
    assert ".transcript.is-virtual" in css

    frontend = ROOT / "src" / "coding_agent" / "web" / "frontend"
    assert (frontend / "test.mjs").is_file()
    assert (frontend / "tests" / "diff.test.ts").is_file()
    assert (frontend / "tests" / "search.test.ts").is_file()
    assert (frontend / "tests" / "virtual.test.ts").is_file()


def test_m6_editor_tabs_tree_and_problems_are_guarded() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        "editorTabs",
        'data-action="editor-tab"',
        'data-op="activate"',
        'data-op="close"',
        "refreshTree",
        "filteredTreeResults",
        "problem-card",
        "activity-badge",
        "i-alert",
    ):
        assert marker in html, marker

    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "editorTabs",
        "activateEditorTab",
        "closeEditorTab",
        "openDiffTab",
        "refreshTree",
        "treeVisibleRows",
        "toggleTreeDir",
        "addProblem",
        "clearProblemsByKind",
        "treeLoadingMap",
    ):
        assert marker in app, marker

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for marker in (".activity-badge", ".problem-card", ".problem-detail"):
        assert marker in css, marker


def test_m7_session_management_is_guarded() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        "sessionFilter",
        "session-filters",
        'data-op="pin"',
        'data-op="archive"',
        'data-op="tags"',
        "session-pin",
        "session-tag",
    ):
        assert marker in html, marker

    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "updateSessionMeta",
        "toggleSessionPin",
        "toggleSessionArchive",
        "editSessionTags",
        "sessionFilterClass",
        "include_archived=true",
    ):
        assert marker in app, marker

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for marker in (".session-filters", ".session-pin", ".session-tag"):
        assert marker in css, marker


def test_batch2_diff_quickopen_and_jump_are_guarded() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        "diffRowsData",
        "splitDiffRowsData",
        "diffViewFlags",
        "editor-gutter",
        "fileView._lineNumbers",
        "goToFileLine",
        "command-group",
        "command-shortcut",
        "skip-link",
        'id="main"',
        'aria-busy="busy"',
        "openNotification",
        "onProblemClick",
    ):
        assert marker in html, marker

    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "splitDiffRows",
        "wordDiffMarkers",
        "rebuildDiffRows",
        "goToFileLine",
        "openQuickOpen",
        "quick-open",
        "parseCommandQuery",
        "fuzzyScore",
        "recentCommands",
        "loadNotifications",
        "persistNotifications",
        "openNotification",
        "onProblemClick",
    ):
        assert marker in app, marker

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for marker in (
        ".editor-gutter",
        ".diff-split",
        ".diff-word",
        ".command-group",
        ".command-shortcut",
        ".skip-link",
        ".problem-clickable",
    ):
        assert marker in css, marker


def test_jsdom_covers_approval_and_layout_resize() -> None:
    """审批流程与侧栏/检查器拖拽必须留在 jsdom 交互测试里。"""
    frontend = ROOT / "src" / "coding_agent" / "web" / "frontend"
    text = (
        frontend / "tests" / "dom" / "interaction.test.ts"
    ).read_text(encoding="utf-8")
    for marker in (
        "approval_requested",
        'data-allow="true"',
        "全部拒绝",
        "pointerdown",
        "pointermove",
        "pointerup",
        ".splitter-left",
        ".splitter-right",
        "codingagent.layout",
    ):
        assert marker in text, marker


def test_m13_theme_contrast_accent_and_code_are_guarded() -> None:
    """高对比度、强调色与代码字号/行高。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        'x-model="contrast"',
        'x-model="accent"',
        'x-model="codeFontSize"',
        'x-model="codeLineHeight"',
        'value="high"',
        'value="violet"',
    ):
        assert marker in html, marker

    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "ACCENT_PRESETS",
        "accentColor",
        "themeBodyClasses",
        "normalizeChoice",
        "--accent",
    ):
        assert marker in app, marker

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for marker in (
        "--code-size",
        "--code-line-height",
        "body.contrast-high",
        "body.code-large",
        "body.code-lh-relaxed",
    ):
        assert marker in css, marker

    frontend = ROOT / "src" / "coding_agent" / "web" / "frontend"
    assert (frontend / "src" / "ui" / "theme.ts").is_file()
    assert (frontend / "tests" / "theme.test.ts").is_file()


def test_m12_settings_search_skeleton_and_error_banner_are_guarded() -> None:
    """设置搜索、加载骨架与全局错误横幅。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        'class="settings-search"',
        "settingsQuery",
        "settingsSearchLabel",
        "settingsNoResults",
        "settingsViewVisible",
        'class="skeleton skeleton-row"',
        'class="editor-skeleton"',
        'class="error-banner"',
        "retryLastError",
        "dismissError",
    ):
        assert marker in html, marker

    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "matchesSettingsQuery",
        "settingsVisibility",
        "reportError",
        "retryLastError",
        "sessionsSkeletonVisible",
        "treeSkeletonVisible",
        "settingsViewVisible",
    ):
        assert marker in app, marker

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for marker in (".skeleton", ".settings-search", ".error-banner"):
        assert marker in css, marker

    frontend = ROOT / "src" / "coding_agent" / "web" / "frontend"
    assert (frontend / "src" / "ui" / "settings.ts").is_file()
    assert (frontend / "src" / "features" / "errors.ts").is_file()
    assert (frontend / "tests" / "settings.test.ts").is_file()


def test_m11_tool_output_paging_and_memo_are_guarded() -> None:
    """大工具输出分页与派生列表缓存。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        "m._outputText",
        "m._outputMore",
        'data-op="more-output"',
        "more-tool-preview",
        "t._previewText",
        'class="output-actions"',
    ):
        assert marker in html, marker

    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "outputPage",
        "memoize",
        "filteredToolsSlot",
        "virtualMessagesSlot",
        "clearDerivedCaches",
        "moreToolOutput",
        "moreToolPreview",
        "_outputLimit",
        "_previewLimit",
    ):
        assert marker in app, marker

    frontend = ROOT / "src" / "coding_agent" / "web" / "frontend"
    assert (frontend / "src" / "ui" / "output.ts").is_file()
    assert (frontend / "src" / "ui" / "memo.ts").is_file()
    assert (frontend / "tests" / "output.test.ts").is_file()
    assert (frontend / "tests" / "memo.test.ts").is_file()


def test_m10_keyboard_nav_and_live_region_are_guarded() -> None:
    """文件树/主区标签键盘导航与独立阶段播报区。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        'role="tree"',
        'role="treeitem"',
        ":aria-level",
        ":aria-expanded",
        ":tabindex=\"entry._tabindex\"",
        'aria-label="文件树"',
        "onTreeKeydown",
        "onMainTabKeydown",
        ":tabindex=\"chatTabIndex\"",
        ":tabindex=\"tab._tabindex\"",
        'class="sr-only live-region"',
        'aria-live="off"',
        "liveAnnouncement",
    ):
        assert marker in html, marker

    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "onTreeKeydown",
        "focusTreeRow",
        "onTreeFocus",
        "onMainTabKeydown",
        "chatTabIndex",
        "announce",
        "liveAnnouncement",
    ):
        assert marker in app, marker


def test_m9_session_unread_preview_and_batch_are_guarded() -> None:
    """会话未读、最后消息摘要、批量操作与 0008 迁移。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        "session-check",
        "session-unread-dot",
        "session-preview",
        "session-batch",
        "sessionSelectionText",
        'data-op="unread"',
        'data-action="session-select"',
        'data-action="session-batch"',
    ):
        assert marker in html, marker

    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "sessionPreviewText",
        "sessionMetaText",
        "toggleSessionSelected",
        "batchSessionAction",
        "markSessionRead",
        "setSessionUnread",
        "_unreadLabel",
        "_selectTitle",
    ):
        assert marker in app, marker

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for marker in (
        ".session-unread-dot",
        ".session-preview",
        ".session-batch",
        ".session-check",
    ):
        assert marker in css, marker

    root = ROOT / "src" / "coding_agent"
    schema = (root / "control" / "schema.py").read_text(encoding="utf-8")
    assert "unread" in schema
    assert "last_message" in schema
    migration = (
        root
        / "control"
        / "migrations"
        / "versions"
        / "0008_session_unread_preview.py"
    )
    assert migration.is_file()
    store = (root / "control" / "store.py").read_text(encoding="utf-8")
    assert '"unread"' in store
    assert '"last_message"' in store

    frontend = root / "web" / "frontend"
    assert (frontend / "tests" / "sessions.test.ts").is_file()


def test_m8_git_problems_incremental_and_focus_are_guarded() -> None:
    """文件树 Git 标记、Problems 聚合、增量 Markdown、焦点陷阱与 jsdom 交互测试。"""
    import json

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for marker in (
        'class="tree-git"',
        'x-show="entry._git"',
        "treeGitState",
        "problemGroups",
        "problem-group-head",
        'data-action="problem-group"',
        "problem-group-count",
        "commandActiveDescendant",
        "hasApprovals",
        "inspectorVisible",
        "_toolCallsText",
    ):
        assert marker in html, marker

    app = (STATIC / "app.js").read_text(encoding="utf-8")
    for marker in (
        "loadGitStatus",
        "markTreeGitDirty",
        "buildGitStatusMap",
        "gitTreeFields",
        "groupProblems",
        "toggleProblemGroup",
        "problemExpanded",
        "renderMarkdownIncremental",
        "markdownCaches",
        "trapModalFocus",
        "focusModal",
        "restoreModalFocus",
        "computeTabTarget",
    ):
        assert marker in app, marker

    css = (STATIC / "style.css").read_text(encoding="utf-8")
    for marker in (".tree-git", ".git-m", ".problem-group", ".problem-group-head"):
        assert marker in css, marker

    frontend = ROOT / "src" / "coding_agent" / "web" / "frontend"
    for relative in (
        "tests/git.test.ts",
        "tests/problems.test.ts",
        "tests/markdown.test.ts",
        "tests/focus.test.ts",
        "tests/dom/harness.ts",
        "tests/dom/interaction.test.ts",
        "test-dom.mjs",
    ):
        assert (frontend / relative).is_file(), relative

    package = json.loads(
        (frontend / "package.json").read_text(encoding="utf-8")
    )
    assert "jsdom" in package["devDependencies"]
    assert "test-dom.mjs" in package["scripts"]["test"]


def test_csp_build_has_no_unsafe_eval_expressions() -> None:
    """TD-016：模板必须保持 Alpine CSP 构建可用的表达式子集。"""
    import re

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'x-data="agentApp"' in html
    assert "/static/vendor/alpine-csp.min.js" in html
    assert "/static/vendor/alpine.min.js" not in html
    attrs = re.findall(
        r'\s(@[a-z.:-]+|x-[a-z-]+|:[a-z-]+)="([^"]*)"', html
    )
    incompatible = []
    for name, expr in attrs:
        text = expr.strip()
        if not text:
            continue
        if re.search(
            r"(\s=\s|===|!==|==|!=|\|\||&&|[?:]|\.\.\.|\{|\}|;|=>"
            r"|!(?!=)|[<>]|\+)",
            text,
        ) or re.search(r"\b[A-Za-z_$][\w$]*\s*\([^)]*[^\s)]", text):
            incompatible.append((name, text))
    assert not incompatible, (
        "CSP 不兼容表达式：" + repr(incompatible[:5])
    )
