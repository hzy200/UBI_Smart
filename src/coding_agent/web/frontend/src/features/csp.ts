/* CSP 构建兼容层：delegated actions + 无参 getter。
 *
 * Alpine CSP 构建不支持带参数的方法调用，也不支持运算符/三元表达式。
 * 模板统一通过 data-action 触发的 onAction 和 getter 访问状态。
 */

import { groupProblems } from '../ui/problems';
import { settingsVisibility } from '../ui/settings';

export const cspMixin = {
  onAction(event) {
    const element = event && (event.currentTarget || event.target);
    const data = (element && element.dataset) || {};
    const action = data.action || '';
    switch (action) {
      case 'view': this.setView(data.view); break;
      case 'session-filter': this.setSessionFilter(data.filter); break;
      case 'settings-tab': this.setSettingsTab(data.tab); break;
      case 'inspector-tab': this.setTab(data.tab); break;
      case 'tool-filter': this.setToolFilter(data.filter); break;
      case 'load-git': this.loadGit(data.diff === 'true'); break;
      case 'diff-view': this.setDiffView(data.view); break;
      case 'steer': this.steer(data.steer); break;
      case 'approval': this.decide(data.allow === 'true'); break;
      case 'split-approval':
        this.decideSplit(data.allow === 'true');
        break;
      case 'split-reject-all': this.decideAllSplit(false); break;
      case 'consent': this.decideConsent(data.id, data.allow === 'true'); break;
      case 'session': this.onSessionAction(data.op, data.id); break;
      case 'session-select': this.toggleSessionSelected(data.id); break;
      case 'session-batch': this.batchSessionAction(data.op); break;
      case 'editor-tab': this.onEditorTabAction(data.op, data.id); break;
      case 'file': this.onFileAction(data.op, data.key, data.snapshot, data.path); break;
      case 'checkpoint': this.onCheckpointAction(data.op, data.id); break;
      case 'revision': this.onRevisionAction(data.op, data.id); break;
      case 'profile': this.onProfileAction(data.op, data.name); break;
      case 'tree': this.onTreeEntryAction(data.path, data.dir === 'true'); break;
      case 'message': this.onMessageAction(data.op, data.id); break;
      case 'memory-forget': this.forget(Number(data.index)); break;
      case 'command': this.onCommandAction(Number(data.index)); break;
      case 'command-hover': this.hoverCommand(Number(data.index)); break;
      case 'copy-tool': this.copyToolDetail(data.key); break;
      case 'copy-tool-text': this.copyToolText(data.key); break;
      case 'more-tool-preview': this.moreToolPreview(data.key); break;
      case 'copy-path': this.copyText(data.path); break;
      case 'delete-upload': this.deleteUpload(data.path); break;
      case 'select-project': this.selectProject(data.id); break;
      case 'recovery': this.onRecoveryAction(data.op, data.id); break;
      case 'match': this.nextMessageMatch(Number(data.delta)); break;
      case 'problem-group': this.toggleProblemGroup(data.group); break;
      default: break;
    }
  },

  onSessionAction(op, id) {
    const row = this.sessions.find((item) => item.thread_id === id);
    if (op === 'open') { this.openSession(id); return; }
    if (op === 'delete') { this.deleteSession(id); return; }
    if (op === 'rename') { this.renameSession(id); return; }
    if (op === 'export') { this.exportSession(id); return; }
    if (op === 'export-json') { this.exportSession(id, 'json'); return; }
    if (!row) return;
    if (op === 'pin') this.toggleSessionPin(row);
    if (op === 'archive') this.toggleSessionArchive(row);
    if (op === 'tags') this.editSessionTags(row);
    if (op === 'unread') this.toggleSessionUnread(row);
    if (op === 'split') this.openSplitSession(row.thread_id);
  },

  onEditorTabAction(op, id) {
    if (op === 'activate') this.activateEditorTab(id);
    if (op === 'close') this.closeEditorTab(id);
  },

  onFileAction(op, key, snapshot, path) {
    if (op === 'diff') { this.loadDiff(snapshot, path); return; }
    if (op === 'reject') { this.restoreSnapshot(snapshot, path); return; }
    const file = this.files.find((item) => item._key === key);
    if (op === 'accept' && file) this.acceptFile(file);
  },

  onCheckpointAction(op, id) {
    if (op === 'fork') this.forkCheckpoint(id);
    if (op === 'rewind') this.rewindTo(id);
  },

  onRevisionAction(op, id) {
    const revision = this.revisions.find((item) => item.revision_id === id);
    if (!revision) return;
    if (op === 'rollback') this.rollbackRevision(revision);
    if (op === 'restore') this.restoreRevision(revision);
    if (op === 'pin') this.pinRevision(revision);
  },

  onProfileAction(op, name) {
    const profile = this.profiles.find((item) => item.name === name);
    if (!profile) return;
    if (op === 'edit') this.editProfile(profile);
    if (op === 'delete') this.deleteProfile(name);
  },

  onTreeEntryAction(path, isDir) {
    this.treeActivePath = path;
    this.openTreeEntry({ path, is_dir: isDir });
  },

  onMessageAction(op, id) {
    const message = this.messages.find(
      (item) => String(item.id) === String(id),
    );
    if (!message) return;
    if (op === 'edit') this.editMessage(message);
    if (op === 'expand') {
      message._expanded = !message._expanded;
      this.decorateMessage(message);
    }
    if (op === 'copy') this.copyTranscriptMessage(message);
    if (op === 'more-output') this.moreToolOutput(message);
  },

  onCommandAction(index) {
    const command = this.filteredCommands()[index];
    if (command) this.runCommand(command);
  },

  onCommandHover(event) {
    this.commandIndex = Number(event.currentTarget.dataset.index);
  },

  onCommandClick(event) {
    this.onCommandAction(Number(event.currentTarget.dataset.index));
  },

  onProblemClick(event) {
    const path = event.currentTarget.dataset.path;
    if (path) void this.openFile(path);
  },

  onRecoveryAction(op, id) {
    if (op === 'delete') { this.deleteSession(id); return; }
    const item = this.recoveryItems.find((row) => row.thread_id === id);
    if (item) this.recoveryResume(item);
  },

  copyToolDetail(key) {
    const tool = this.tools.find((item) => item.key === key);
    if (tool) this.copyText(tool._detail);
  },

  copyToolText(key) {
    const tool = this.tools.find((item) => item.key === key);
    if (tool) this.copyText(tool.preview || tool.summary);
  },

  onStartResize(event) {
    const side = event.currentTarget.dataset.side;
    this.startResize(side, event);
  },

  onMessageSearchInput(event) {
    this.searchMessages(event.target.value);
  },

  onSessionSearchInput(event) {
    this.sessionQuery = event.target.value;
    this.filterSessions();
  },

  onCommandInput(event) {
    this.commandQuery = event.target.value;
    this.onCommandQuery();
  },

  selectCurrentProject(event) {
    this.selectProject(event.target.value);
  },

  loadRecentCommands() {
    try {
      const saved = JSON.parse(
        localStorage.getItem('codingagent.recentCommands') || '[]',
      );
      this.recentCommands = Array.isArray(saved)
        ? saved.map((item) => String(item)).filter(Boolean).slice(0, 8)
        : [];
    } catch (err) {
      this.recentCommands = [];
    }
  },

  recordCommand(id) {
    const clean = String(id || '').replace(/^recent:/, '');
    if (!clean) return;
    this.recentCommands = [
      clean,
      ...this.recentCommands.filter((item) => item !== clean),
    ].slice(0, 8);
    try {
      localStorage.setItem(
        'codingagent.recentCommands',
        JSON.stringify(this.recentCommands),
      );
    } catch (err) { /* ignore */ }
  },

  openQuickOpen() {
    this.commandOpen = true;
    this.rememberModalFocus();
    this.commandQuery = '#';
    this.commandIndex = 0;
    this.fileSearchResults = [];
    this.fileSearchLoading = false;
    this._searchSeq += 1;
    if (this._searchTimer) clearTimeout(this._searchTimer);
    this.$nextTick(() => {
      const input = this.$refs.commandInput;
      if (input) input.focus();
    });
  },

  commandGroupOrder() {
    return ['最近使用', '命令', '会话', '文件'];
  },

  openShortcuts() {
    this.shortcutsOpen = true;
    this.focusModal('.shortcuts-modal');
  },
  closeShortcuts() {
    this.shortcutsOpen = false;
    this.$nextTick(() => this.restoreModalFocus());
  },
  openNotifyUnread() {},
  onProjectDragOver() { this.projectDropActive = true; },
  onProjectDragLeave() { this.projectDropActive = false; },
  rejectAll() { this.decideAll(false); },
  scrollLatest() { this.scrollBottom(true); },
  nextMatchNext() { this.nextMessageMatch(1); },
  nextMatchPrev() { this.nextMessageMatch(-1); },
  copyActiveFilePath() { this.copyText(this.fileView.path); },
  copyActiveFileContent() { this.copyText(this.fileView._text); },
  copyActiveDiff() { this.copyText(this.diffDisplay); },

  get bootVisible() {
    return (!this.configLoaded || !this.versionChecked)
      && !this.configError && !this.versionError;
  },
  get bootText() {
    return this.configLoaded
      ? '正在校验前后端版本…'
      : '正在加载运行时配置…';
  },
  get configErrorCodeText() { return `错误码：${this.configErrorCode}`; },
  get versionErrorCodeText() { return `错误码：${this.versionErrorCode}`; },
  get loginVisible() {
    return this.ready === false && this.configLoaded && this.versionChecked
      && !this.configError && !this.versionError;
  },
  get authTokenVisible() {
    return this.authMode !== 'bff-cookie' && this.authMode !== 'local-session';
  },
  get authBffVisible() { return this.authMode === 'bff-cookie'; },
  get authLocalVisible() {
    return this.authMode === 'local-session' && !this.mfaRequired;
  },
  get mfaVisible() {
    return this.authMode === 'local-session' && this.mfaRequired;
  },
  get authOidcVisible() {
    return this.authMode === 'local-session'
      && this.oidcAvailable && !this.mfaRequired;
  },
  get authModeText() {
    return this.t('settings.authMode', { mode: this.authModeLabel });
  },
  get chatTabIndex() { return this.mainTab === 'chat' ? 0 : -1; },
  get mainPaneClass() {
    return this.splitOpen && this.activePane === 'main' ? 'pane-active' : '';
  },
  get splitPaneClass() {
    return this.splitOpen && this.activePane === 'split'
      ? 'pane-active split-pane-active'
      : '';
  },
  get settingsViewVisible() { return settingsVisibility(this.settingsQuery); },
  get settingsMatchCount() {
    return Object.values(this.settingsViewVisible).filter(Boolean).length;
  },
  get settingsSearchLabel() {
    if (!String(this.settingsQuery || '').trim()) return '';
    const count = this.settingsMatchCount;
    return count
      ? this.t('settings.matches', { count })
      : this.t('settings.noResults');
  },
  get settingsNoResults() {
    return Boolean(String(this.settingsQuery || '').trim())
      && this.settingsMatchCount === 0;
  },
  get treeSkeletonVisible() {
    return this.treeRootLoading() && this.filteredTreeRows().length === 0;
  },
  get sessionsSkeletonVisible() {
    return Boolean(this.sessionsLoading) && this.sessions.length === 0;
  },
  get splitPaneMessages() {
    return this.splitView ? this.splitView.messages.slice(-200) : [];
  },
  get splitEmpty() {
    return Boolean(this.splitView)
      && !this.splitLoading
      && !this.splitError
      && this.splitPaneMessages.length === 0;
  },
  get splitHasApprovals() {
    return Boolean(this.splitView && this.splitView.approvals.length);
  },
  get splitApproval() {
    return this.splitView ? this.splitView.approval : null;
  },
  get splitApprovalPosition() {
    return this.splitView ? this.splitView.approvalPosition : '';
  },
  get splitApprovalLevelText() {
    return this.splitApproval ? String(this.splitApproval.level || '') : '';
  },
  get splitApprovalCommandText() {
    return this.splitApproval ? String(this.splitApproval.command || '') : '';
  },
  get splitApprovalReasonText() {
    return this.splitApproval ? String(this.splitApproval.reason || '') : '';
  },
  get splitApprovalToolText() {
    return this.splitApproval
      ? `工具：${String(this.splitApproval.tool || '')}`
      : '';
  },
  get splitStatusText() {
    return this.splitView ? this.splitView.stateText : '';
  },
  get splitSendDisabled() {
    return !this.splitView
      || Boolean(this.splitView.busy)
      || Boolean(this.splitView.approvals.length);
  },
  get sidebarExpanded() { return this.sidebarCollapsed ? 'false' : 'true'; },
  get inspectorExpanded() { return this.inspectorCollapsed ? 'false' : 'true'; },
  get hasUnreadNotifications() { return this.unreadNotifications > 0; },
  get hasSessionSelection() { return this.selectedSessionIds().length > 0; },
  get sessionSelectionText() {
    return this.t('sidebar.selected', {
      count: this.selectedSessionIds().length,
    });
  },
  get hasApprovals() { return this.approvals.length > 0; },
  get commandActiveDescendant() { return `command-item-${this.commandIndex}`; },
  get unreadNotificationsText() {
    return this.t('notifications.unread', {
      count: this.unreadNotifications,
    });
  },
  get workspaceDisplay() {
    return this.workspace || this.t('common.unknown');
  },
  get workspaceInputPlaceholder() {
    return this.capabilities.host_mode
      ? this.t('sidebar.hostWorkspace')
      : this.t('sidebar.projectWorkspace');
  },
  get noProjects() { return this.projects.length === 0; },
  get noWorkspaceRegistry() { return !this.capabilities.workspace_registry; },
  get applyModelDisabled() { return !this.threadId || this.busy; },
  get createSessionDisabled() {
    return this.busy || this.approvals.length > 0 || this.paused;
  },
  get sidebarVisible() { return !this.sidebarCollapsed; },
  get inspectorVisible() { return !this.inspectorCollapsed; },
  get messageSearchClosed() { return !this.messageSearchOpen; },
  get virtualTopStyle() { return `height:${this.virtualTop}px`; },
  get virtualBottomStyle() { return `height:${this.virtualBottom}px`; },
  get pauseDisabled() { return !this.busy; },
  get cancelDisabled() { return !this.busy && !this.paused; },
  get resumeDisabled() { return !this.paused; },
  get appendDisabled() { return !this.busy; },
  get replanDisabled() { return !this.busy; },
  get historyMoreLabel() {
    return this.historyLoading
      ? this.t('main.loadingOlder')
      : this.t('main.loadOlder');
  },
  get uploadRetryVisible() {
    return Boolean(this.uploadFailed) && !this.uploadBusy;
  },
  get hasUploads() { return this.uploads.length > 0; },
  get problemGroups() { return groupProblems(this.problems, this.problemExpanded); },
  get problemsCountText() {
    const groups = this.problemGroups.length;
    return groups ? `${this.problems.length} 个问题 · ${groups} 组` : '0 个问题';
  },
  get noSessions() { return this.sessionRows.length === 0; },
  get filesEmptyState() { return this.filesEmpty(); },
  get treeLoadingText() { return this.treeRootLoading() ? '加载中…' : ''; },
  get treeEmpty() {
    return !this.treeRootLoading() && this.filteredTreeRows().length === 0;
  },
  get gitDiffTabOpen() {
    return this.mainTab === 'diff' && this.diffPath === 'Git';
  },
  get noTasks() { return this.tasks.length === 0; },
  get noMemory() { return this.memory.length === 0; },
  get noAudit() { return this.audit.length === 0; },
  get noProblems() { return this.problems.length === 0; },
  get hasProblems() { return this.problems.length > 0; },
  get noPlan() { return this.planRows.length === 0; },
  get compactDisabled() { return !this.threadId || this.busy; },
  get noTools() { return this.filteredTools().length === 0; },
  get noTimeline() { return this.timeline.length === 0; },
  get noCommands() {
    return this.filteredCommands().length === 0 && !this.fileSearchLoading;
  },
  get noRecovery() { return this.recoveryItems.length === 0; },
  get noNotifications() { return this.notifications.length === 0; },
  get hasReasoning() {
    return this.showReasoning && this.reasoningBlocks.length > 0;
  },
  get canRetry() { return Boolean(this._lastPrompt) && !this.busy; },
  get updateAvailable() {
    return Boolean(this.updateInfo && this.updateInfo.available);
  },
  get canPickWorkspace() {
    return this.desktopAvailable && !this.capabilities.host_mode;
  },
  get showCancelProjectJob() {
    return this.projectBusy && Boolean(this.projectJobId);
  },
  get showProjectProgress() {
    return this.projectBusy || this.projectPercent > 0;
  },
  get activeEditorStale() {
    const tab = this.activeEditorTab();
    return Boolean(tab && tab.stale);
  },
  get diffPathLabel() { return this.diffPath || '本轮改动'; },
  get canExpandDiff() { return this.diffTruncated && !this.diffExpanded; },
  get approvalLevelText() {
    return this.approval ? String(this.approval.level || '') : '';
  },
  get fourEyesApproval() {
    return Boolean(
      this.fourEyes
      && this.approval
      && Number(this.approval.level_int || 0) >= this.fourEyesMinLevel,
    );
  },
  get approvalActionsVisible() {
    return !this.fourEyesApproval;
  },
  get approvalCommandText() {
    return this.approval ? String(this.approval.command || '') : '';
  },
  get approvalReasonText() {
    return this.approval ? String(this.approval.reason || '') : '';
  },
  get approvalToolText() {
    return this.approval
      ? `工具：${String(this.approval.tool || '')}`
      : '';
  },
  get usageLabel() {
    if (!this.usage.input && !this.usage.output) return '';
    return `${this.usage.input} in / ${this.usage.output} out`;
  },
  get fileInVscodeAvailable() { return this.canOpenFileInVscode(); },
  get fileInExplorerAvailable() { return this.canRevealInExplorer(); },
  get connectionLabel() { return this.connectionText(); },
  get transcriptEmpty() {
    return this.messages.length === 0 && this.consents.length === 0;
  },
  get filteredTreeResults() { return this.filteredTreeRows(); },
  get filteredToolResults() { return this.filteredTools(); },
  get filteredCommandResults() {
    let lastGroup = '';
    return this.filteredCommands().map((item, index) => {
      const groupStart = item.group !== lastGroup;
      lastGroup = item.group;
      return {
        detail: '',
        ...item,
        _index: index,
        _selected: index === this.commandIndex,
        _cls: index === this.commandIndex ? 'selected' : '',
        _domId: `command-item-${index}`,
        _groupStart: groupStart,
        _groupLabel: item.group || '',
        _shortcut: item.shortcut || '',
      };
    });
  },
  get shortcutRows() { return this.shortcuts(); },
  get profilesDisabled() { return !this.profilesEnabled; },
  get noRevisions() { return this.revisions.length === 0; },
  get projectUploadDisabled() { return this.projectBusy || !this.projectFile; },
  get noDiagnosticsPath() { return !this.diagnosticsPath; },
  get noThread() { return !this.threadId; },
  get virtualMessageResults() { return this.virtualMessages(); },
  get diffResultRows() { return this.diffRows(); },
  get transcriptClassValue() { return this.transcriptClass(); },
  get currentEditorPath() {
    return this.mainTab === 'file' ? this.fileView.path : '';
  },
};
