/* Alpine mixin: sessions。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
  cachedJson,
  invalidateGet,
  readJson,
} from '../api/http';
import { clearDraft, draftText, saveDraft } from '../state/drafts';
import {
  deleteCachedTranscript,
  readCachedSessions,
  readCachedTranscript,
  writeCachedSessions,
  writeCachedTranscript,
} from '../state/history-cache';
import {
  renderMarkdown,
} from '../ui/format';
import {
  clearDerivedCaches,
  markdownCaches,
  messageHeights,
  renderTimers,
  tokenBuffers,
  toolMap,
} from '../state/runtime';
import { prependOlder } from '../ui/history';
import { normalizeSessionImport } from '../ui/session-import';
import { sessionMetaText, sessionPreviewText } from '../ui/sessions';

export const sessionsMixin = {
  async saveTranscript() {
    const threadId = this.threadId;
    const unread = this._isBackgroundApply || this.sessionHidden();
    if (!threadId || this.messages.length === 0) return;
    const messages = this.messages.map((message) => ({
      role: message.role,
      kind: message.kind || '',
      phase: message.phase || '',
      text: message.text || '',
      summary: message.summary || '',
      preview: message.preview || '',
      name: message.name || '',
      level: message.level || '',
      state: message.state || '',
      call_id: message.call_id || '',
      duration_ms: message.duration_ms ?? null,
      exit_code: message.exit_code ?? null,
      ok: message.ok ?? null,
    }));
    // 先写本地缓存：即使服务端保存失败，下次打开仍能立即看到最近轨迹。
    void writeCachedTranscript(threadId, { transcript: messages });
    const view = this.sessionViews[threadId];
    if (view) {
      // 当前视图已经包含新消息；不要再用旧的 offset 去拼接更早的一页。
      view._historyItems = messages;
      view.historyHasMore = false;
    }
    try {
      const response = await authFetch(
        `/api/thread/${encodeURIComponent(threadId)}/transcript`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messages, unread }),
        },
      );
      await readJson(response, '保存对话轨迹失败');
      if (unread) this.applySessionUnread(threadId, true);
    } catch (err) {
      // 轨迹只是 UI 增强，保存失败不能打断运行。
    }
  },

  restoreTranscript(items) {
    this.messages = [];
    for (const item of items) {
      if (!item || typeof item !== 'object') continue;
      if (item.kind === 'tool') {
        const state = item.state || (item.ok ? '完成' : '失败');
        const cls = item.ok
          ? 'tool-ok'
          : state === '已拒绝'
            ? 'tool-rejected'
            : 'tool-error';
        const text = [item.summary, item.preview]
          .filter(Boolean)
          .join('\n') || item.text || '';
        const toolMessage = {
          id: ++this._msgSeq,
          role: '工具',
          phase: 'tool',
          kind: 'tool',
          call_id: item.call_id || '',
          name: item.name || 'tool',
          summary: item.summary || '',
          level: item.level || '',
          state,
          preview: item.preview || '',
          text,
          done: true,
          _expanded: false,
          _cls: `msg-tool ${cls}`,
          _longOutput: String(item.preview || '').length > 800,
        };
        this.decorateMessage(toolMessage);
        this.messages.push(toolMessage);
        continue;
      }
      const role = ['user', 'system', 'assistant'].includes(item.role)
        ? item.role
        : 'assistant';
      const phase = item.phase || (role === 'assistant' ? 'answer' : role);
      let cls = `msg-${role}`;
      if (role === 'assistant') {
        cls = phase === 'final'
          ? 'msg-assistant msg-final'
          : phase === 'process'
            ? 'msg-assistant msg-process'
            : 'msg-assistant msg-answer';
      }
      const message = {
        id: ++this._msgSeq,
        role,
        phase,
        text: item.text || '',
        html: renderMarkdown(item.text || ''),
        done: true,
        _expanded: false,
        _cls: cls,
      };
      this.decorateMessage(message, phase);
      this.messages.push(message);
    }
    // 兼容旧轨迹：即使保存时尾部有过程提示，也把最终回答放回最后。
    this.moveFinalAnswerLast();
    this.$nextTick(() => {
      this.scrollBottom(true);
      this.decorateTranscript();
    });
  },

  decorateSession(row) {
    row.tags = Array.isArray(row.tags) ? row.tags : [];
    row.unread = Boolean(row.unread);
    row._title = row.title || row.thread_id;
    row._preview = sessionPreviewText(row.last_message);
    row._meta = sessionMetaText(row);
    row._pinLabel = row.pinned ? '取消置顶' : '置顶';
    row._archiveLabel = row.archived ? '取消归档' : '归档';
    row._unreadLabel = row.unread ? '标记已读' : '标记未读';
    row._hasTags = row.tags.length > 0;
    row._selected = Boolean(this.sessionSelected[row.thread_id]);
    row._selectTitle = row._selected ? '取消选择' : '选择会话';
    const classes = [];
    if (row.thread_id === this.threadId) classes.push('active');
    if (row.unread) classes.push('unread');
    row._cls = classes.join(' ');
    return row;
  },

  applySessionPayload(data) {
    const rows = ((data && data.sessions) || []).map((item) => (
      this.decorateSession({
        ...item,
        tags: Array.isArray(item.tags) ? item.tags : [],
      })
    ));
    const known = new Set(rows.map((row) => row.thread_id));
    const selected = {};
    for (const [id, active] of Object.entries(this.sessionSelected)) {
      if (active && known.has(id)) selected[id] = true;
    }
    this.sessionSelected = selected;
    this.sessions = rows;
    this.filterSessions();
  },

  async loadSessions(force = false) {
    this.sessionsLoading = true;
    let usedCache = false;
    if (!force && !this.sessions.length) {
      const cached = await readCachedSessions();
      if (cached && !this.sessions.length) {
        this.applySessionPayload(cached);
        usedCache = true;
        this.sessionsLoading = false;
      }
    }
    try {
      const data = await cachedJson(
        '/api/sessions?limit=50&include_archived=true',
        '无法读取会话',
        { ttlMs: 1500, force },
      );
      this.applySessionPayload(data);
      void writeCachedSessions(data);
    } catch (e) {
      if (usedCache) this.toastMessage(`会话列表刷新失败：${e}`);
      else this.reportError(`无法读取会话：${e}`, 'sessions');
    } finally {
      this.sessionsLoading = false;
    }
  },

  async openSplitSession(id) {
    if (!id) return;
    if (id === this.threadId) {
      this.toastMessage('该会话已在主区');
      return;
    }
    const view = this.ensureSessionView(id);
    view.lastActiveAt = Date.now();
    this.splitView = view;
    this.splitOpen = true;
    this.activePane = 'split';
    this.splitSessionId = id;
    this.splitError = '';
    this.splitInput = '';
    const row = this.sessions.find((item) => item.thread_id === id);
    this.splitTitle = (row && (row._title || row.title)) || id;
    this.markSessionRead(id);
    this.splitLoading = !view.loaded;
    if (!view.loaded || view._cacheOnly) {
      const loaded = await this.loadSessionHistory(view);
      view.loaded = loaded;
      if (!loaded) this.splitError = '读取分屏会话失败';
    }
    this.splitLoading = false;
    this.syncUrlState();
  },

  refreshSplit() {
    const view = this.splitView;
    if (!view) return;
    if (view.busy || view.connectionState === 'reconnecting') {
      void this.reconnect(view);
      return;
    }
    if (!view.loaded || view._cacheOnly) {
      this.splitLoading = true;
      void this.loadSessionHistory(view).then((loaded) => {
        view.loaded = loaded;
        if (!loaded) this.splitError = '读取分屏会话失败';
        this.splitLoading = false;
      });
      return;
    }
    this.toastMessage('分屏实时更新，无需刷新');
  },

  async sendSplit() {
    const view = this.splitView;
    const text = String(this.splitInput || '').trim();
    if (!view || !text || view.busy || view.approvals.length) return;
    this.splitInput = '';
    await this.sendToView(view, text, true);
  },

  closeSplit() {
    this.splitOpen = false;
    this.activePane = 'main';
    this.splitSessionId = '';
    this.splitView = null;
    this.splitInput = '';
    this.splitError = '';
    this.splitLoading = false;
    this.syncUrlState();
  },

  openSplitInMain() {
    const id = this.splitSessionId;
    this.closeSplit();
    void this.openSession(id);
  },

  currentSession() {
    return this.sessions.find(
      (item) => item.thread_id === this.threadId,
    ) || null;
  },

  setSessionFilter(name) {
    this.sessionFilter = name;
    this.sessionFilterClasses = {
      all: name === 'all' ? 'active' : '',
      pinned: name === 'pinned' ? 'active' : '',
      archived: name === 'archived' ? 'active' : '',
    };
    this.filterSessions();
  },

  sessionFilterClass(name) {
    return this.sessionFilter === name ? 'active' : '';
  },

  async updateSessionMeta(id, payload) {
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(id)}/meta`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        },
      );
      const data = await readJson(response, '更新会话失败');
      const row = this.sessions.find((item) => item.thread_id === id);
      if (row) {
        row.pinned = Boolean(data.pinned);
        row.archived = Boolean(data.archived);
        row.tags = Array.isArray(data.tags) ? data.tags : [];
        if (typeof data.unread === 'boolean') row.unread = data.unread;
        if (typeof data.last_message === 'string') {
          row.last_message = data.last_message;
        }
        this.decorateSession(row);
      }
      invalidateGet('/api/sessions');
      this.filterSessions();
      this.broadcastSync('sessions');
      return data;
    } catch (err) {
      this.toastMessage(String(err));
      return null;
    }
  },

  async toggleSessionPin(row) {
    if (!row) return;
    await this.updateSessionMeta(row.thread_id, { pinned: !row.pinned });
  },

  async toggleSessionArchive(row) {
    if (!row) return;
    await this.updateSessionMeta(row.thread_id, { archived: !row.archived });
  },

  async editSessionTags(row) {
    if (!row) return;
    const current = (row.tags || []).join(', ');
    const value = window.prompt('标签（逗号分隔，最多 8 个）：', current);
    if (value === null) return;
    const tags = value
      .split(/[,，]/)
      .map((tag) => tag.trim())
      .filter(Boolean);
    await this.updateSessionMeta(row.thread_id, { tags });
  },

  toggleSessionSelected(id) {
    if (!id) return;
    this.sessionSelected = {
      ...this.sessionSelected,
      [id]: !this.sessionSelected[id],
    };
    this.filterSessions();
  },

  clearSessionSelection() {
    this.sessionSelected = {};
    this.filterSessions();
  },

  selectedSessionIds() {
    return Object.keys(this.sessionSelected).filter(
      (id) => this.sessionSelected[id],
    );
  },

  sessionHidden() {
    return typeof document !== 'undefined'
      && document.visibilityState !== 'visible';
  },

  applySessionUnread(id, unread) {
    const row = this.sessions.find((item) => item.thread_id === id);
    if (!row || row.unread === Boolean(unread)) return;
    row.unread = Boolean(unread);
    this.decorateSession(row);
    this.filterSessions();
  },

  async setSessionUnread(id, unread) {
    if (!id) return;
    this.applySessionUnread(id, unread);
    await this.updateSessionMeta(id, { unread: Boolean(unread) });
  },

  markSessionRead(id) {
    const row = this.sessions.find((item) => item.thread_id === id);
    if (!row || !row.unread) return;
    void this.setSessionUnread(id, false);
  },

  async toggleSessionUnread(row) {
    if (!row) return;
    await this.setSessionUnread(row.thread_id, !row.unread);
  },

  async batchSessionAction(op) {
    if (op === 'clear') {
      this.clearSessionSelection();
      return;
    }
    const ids = this.selectedSessionIds();
    if (!ids.length || this.sessionBatchBusy) return;
    if (
      op === 'delete'
      && !window.confirm(`删除选中的 ${ids.length} 个会话？审计与 checkpoint 会保留。`)
    ) {
      return;
    }
    this.sessionBatchBusy = true;
    let ok = 0;
    let failed = 0;
    for (const id of ids) {
      try {
        const url = op === 'delete'
          ? `/api/sessions/${encodeURIComponent(id)}`
          : `/api/sessions/${encodeURIComponent(id)}/meta`;
        const response = await authFetch(url, {
          method: op === 'delete' ? 'DELETE' : 'POST',
          headers: op === 'delete'
            ? {}
            : { 'Content-Type': 'application/json' },
          body: op === 'delete'
            ? undefined
            : JSON.stringify({ archived: op === 'archive' }),
        });
        await readJson(response, '批量操作失败');
        ok += 1;
      } catch (err) {
        failed += 1;
      }
    }
    this.sessionBatchBusy = false;
    this.sessionSelected = {};
    invalidateGet('/api/sessions');
    await this.loadSessions(true);
    this.broadcastSync('sessions');
    this.toastMessage(
      failed
        ? `已完成 ${ok} 个，失败 ${failed} 个`
        : `已完成 ${ok} 个会话`,
    );
  },

  sessionWorkspaceLabel(item) {
    const project = this.projects.find(
      (entry) => entry.workspace_id === item.workspace_id,
    );
    if (project) {
      const kind = project.kind === 'host' ? ' · 本机' : '';
      return `${project.display_name || project.workspace_id}${kind}`;
    }
    if (item.workspace) return `WSL · ${item.workspace}`;
    return '未绑定工作区';
  },

  filterSessions() {
    const q = this.sessionQuery.trim().toLowerCase();
    for (const row of this.sessions) this.decorateSession(row);
    let base = this.sessions;
    if (this.sessionFilter === 'pinned') {
      base = base.filter((row) => row.pinned && !row.archived);
    } else if (this.sessionFilter === 'archived') {
      base = base.filter((row) => row.archived);
    } else {
      base = base.filter((row) => !row.archived);
    }
    const filtered = q
      ? base.filter((row) =>
          row._title.toLowerCase().includes(q) ||
          row.thread_id.toLowerCase().includes(q) ||
          String(row.workspace || '').toLowerCase().includes(q) ||
          (row.tags || []).join(' ').toLowerCase().includes(q) ||
          this.sessionWorkspaceLabel(row).toLowerCase().includes(q))
      : base.slice();
    filtered.forEach((row) => {
      row._groupLabel = this.sessionWorkspaceLabel(row);
    });
    const recency = new Map<string, number>();
    for (const row of filtered) {
      const when = Date.parse(row.last_active || '') || 0;
      recency.set(
        row._groupLabel,
        Math.max(recency.get(row._groupLabel) || 0, when),
      );
    }
    const groups = [...recency.keys()].sort((a, b) => {
      const diff = (recency.get(b) || 0) - (recency.get(a) || 0);
      return diff !== 0 ? diff : a.localeCompare(b);
    });
    const rows = [];
    for (const group of groups) {
      const items = filtered
        .filter((row) => row._groupLabel === group)
        .sort((a, b) => {
          const pin = Number(Boolean(b.pinned)) - Number(Boolean(a.pinned));
          if (pin !== 0) return pin;
          return String(b.last_active || '').localeCompare(
            String(a.last_active || ''),
          );
        });
      items.forEach((row, index) => {
        row._showGroup = index === 0;
        rows.push(row);
      });
    }
    this.sessionRows = rows;
  },

  formatBytes(value) {
    const size = Number(value || 0);
    if (size < 1024) return `${size} B`;
    if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
    return `${(size / 1024 / 1024).toFixed(1)} MB`;
  },

  async createSession() {
    const registry = Boolean(this.capabilities.workspace_registry);
    if (registry && !this.projectId) {
      this.toastMessage('请先选择或创建一个项目');
      return undefined;
    }
    const body = {
      thread_id: '',
      workspace_id: registry ? this.projectId : '',
      workspace: registry ? '' : this.workspaceInput.trim(),
      access_mode: this.accessMode,
      model: this.model,
      provider: this.provider,
    };
    const response = await authFetch('/api/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await readJson(response, '创建会话失败');
    this.activateSessionView(data.thread_id, true);
    this._boundProjectId = data.workspace_id || '';
    if (data.workspace_id) this.projectId = data.workspace_id;
    const project = this.selectedProject();
    // host 工作区没有 A2 侧路径，用项目名做可读标签。
    const label = data.workspace ||
      (project ? (project.display_name || project.root_path) : '');
    this.workspace = label;
    this.workspaceInput = label;
    this.accessMode = data.access_mode;
    this.provider = data.provider || '';
    this.capabilities = data.capabilities || this.capabilities;
    this.resetConversation();
    // 新会话输入框里的内容在创建前挂在 __new__ 下，绑定 thread_id 后迁移。
    const pendingDraft = draftText('');
    if (pendingDraft) {
      saveDraft(data.thread_id, pendingDraft);
      clearDraft('');
    }
    this.restoreDraft(data.thread_id);
    invalidateGet('/api/sessions');
    await this.loadSessions(true);
    this.broadcastSync('sessions');
    this.syncUrlState();
    this.toastMessage(`会话已创建：${data.thread_id}`);
    return data.thread_id;
  },

  async switchModel() {
    if (!this.threadId || this.busy) return;
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(this.threadId)}/model`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ model: this.model, provider: this.provider }),
        },
      );
      const data = await readJson(response, '切换模型失败');
      this.model = data.model;
      this.provider = data.provider;
      this.toastMessage('模型已切换，下一次运行生效');
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async renameSession(id) {
    const row = this.sessions.find((item) => item.thread_id === id);
    const title = window.prompt('会话名称：', row ? row._title : id);
    if (title === null) return;
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(id)}/rename`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title }),
        },
      );
      await readJson(response, '重命名失败');
      invalidateGet('/api/sessions');
      await this.loadSessions(true);
      this.broadcastSync('sessions');
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async deleteSession(id) {
    if (!window.confirm(`删除会话 ${id}？审计和 checkpoint 会保留。`)) return;
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(id)}`,
        { method: 'DELETE' },
      );
      await readJson(response, '删除失败');
      const wasActive = this.threadId === id;
      const wasSplit = this.splitSessionId === id;
      this.clearDraftFor(id);
      void deleteCachedTranscript(id);
      this.disposeSessionView(id);
      if (wasSplit) this.closeSplit();
      if (wasActive) {
        this._boundProjectId = '';
        this.resetConversation();
        this.restoreDraft('');
      }
      invalidateGet('/api/sessions');
      await this.loadSessions(true);
      this.broadcastSync('sessions');
      this.syncUrlState();
      this.toastMessage('会话已删除');
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async exportSession(id, format = 'markdown') {
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(id)}/export?format=${format}`,
      );
      const data = await readJson(response, '导出失败');
      const json = format === 'json';
      const body = json ? JSON.stringify(data, null, 2) : String(data.content || '');
      const blob = new Blob(
        [body],
        { type: json ? 'application/json' : 'text/markdown' },
      );
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download =
        `${(data.title || id).replace(/[\\/:*?"<>|]/g, '_')}.` +
        (json ? 'json' : 'md');
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async importSessionFile(event) {
    const file = event.target.files && event.target.files[0];
    event.target.value = '';
    if (!file) return;
    try {
      const text = await file.text();
      const payload = normalizeSessionImport(JSON.parse(text));
      await this.importSession(payload);
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async importSession(payload) {
    const response = await authFetch('/api/sessions/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title: payload.title || '',
        messages: payload.messages || [],
        transcript: payload.transcript || [],
        workspace_id: this.projectId || '',
        workspace: this.workspaceInput || '',
        access_mode: this.accessMode,
        model: this.model,
        provider: this.provider,
      }),
    });
    const data = await readJson(response, '导入会话失败');
    invalidateGet('/api/sessions');
    await this.loadSessions(true);
    this.broadcastSync('sessions');
    await this.openSession(data.thread_id);
    this.toastMessage(
      this.t('sidebar.imported', { count: Number(data.imported || 0) }),
    );
  },

  async ensureSession() {
    if (this.threadId) return this.threadId;
    return this.createSession();
  },

  async newSession() {
    this.activateSessionView('', true);
    this._boundProjectId = '';
    this.resetConversation();
    this.restoreDraft('');
    await this.loadSessions(true);
    this.syncUrlState();
    this.toastMessage('已切换到新会话');
  },

  resetSessionUi() {
    this.treeQuery = '';
    this.treeChildren = {};
    this.treeExpanded = {};
    this.treeLoadingMap = {};
    this.treeGit = {};
    this.treeGitState = '';
    this.treeActivePath = '';
    this._treeGitLoaded = false;
    this.gitInfo = {
      is_repo: false, _state: '', _branch: '',
      _changes: [], _commits: [],
    };
    this.gitDiffText = '';
  },

  resetEditorUi() {
    this.diffText = '';
    this.diffDisplay = '';
    this.diffPath = '';
    this.diffTruncated = false;
    this.diffExpanded = false;
    this.diffRowsData = [];
    this.splitDiffRowsData = [];
    this.diffView = 'unified';
    this.diffViewFlags = { unified: true, split: false };
    this.diffViewClasses = { unified: 'active', split: '' };
    this.fileLineInput = '';
    this.mainTab = 'chat';
    this.mainTabFlags = { chat: true, file: false, diff: false };
    this.mainTabClasses = { chat: 'active', file: '', diff: '' };
    this.editorTabs = [];
    this.activeEditorId = '';
    this.fileView = {
      path: '', size: 0, _text: '', _html: '',
      _lang: '', _size: '', _lineNumbers: '', _crumb: '',
      _isImage: false, _isText: true, _imageNote: '',
      data_url: '', mime: '',
    };
  },

  resetConversation() {
    const staleIds = new Set<number>(
      (this.messages || []).map((message) => Number(message.id)),
    );
    const threadKey = this.threadId || '-';
    this.messages = [];
    this.reasoningBlocks = [];
    this.lastSeq = 0;
    this.canReconnect = false;
    this.connectionState = 'idle';
    this._terminal = false;
    this._suspended = false;
    this._retryCount = 0;
    this._retryTimer = null;
    this._lastPrompt = '';
    this._currentMessage = null;
    this._currentNode = '';
    this.prompt = '';
    this.plan = [];
    this.kinds = [];
    this.planRows = [];
    this.timeline = [];
    this.problems = [];
    this.problemExpanded = {};
    this.stepIdx = -1;
    this.planDegraded = false;
    this.tools = [];
    this.toolFilter = 'all';
    this.toolFilterClasses = {
      all: 'active', running: '', ok: '', error: '',
    };
    this.toolQuery = '';
    this.audit = [];
    this.files = [];
    this.filesDirty = false;
    this.checkpoints = [];
    this.tasks = [];
    this.tasksDirty = false;
    this.uploads = [];
    this.uploadQuota = '';
    this.approvals = [];
    this.approval = null;
    this.approvalPosition = '';
    this.decisions = {};
    this.costText = '';
    this.usage = { input: 0, output: 0, cost: 0, budget: 0, ratio: 0, cache: 0 };
    this._toolsVersion = 0;
    if (this._virtualRaf) cancelAnimationFrame(this._virtualRaf);
    this._virtualRaf = 0;
    this.virtualStart = 0;
    this.virtualEnd = 0;
    this.virtualTop = 0;
    this.virtualBottom = 0;
    this.messageSearchOpen = false;
    this.messageQuery = '';
    this.messageMatches = [];
    this.messageMatchIndex = 0;
    this.showJumpLatest = false;
    this.paused = false;
    this.stateText = this.t('status.ready');
    if (this.activeView) this.activeView.loaded = false;
    this.resetSessionUi();
    this.resetEditorUi();
    // 只清理当前视图的缓存，避免打断其他会话仍在进行的流。
    const prefix = `${threadKey}::`;
    for (const key of Array.from(toolMap.keys())) {
      if (key.startsWith(prefix)) toolMap.delete(key);
    }
    for (const id of staleIds) {
      const timer = renderTimers.get(id);
      if (timer !== undefined) clearTimeout(timer);
      renderTimers.delete(id);
      tokenBuffers.delete(id);
      markdownCaches.delete(id);
      messageHeights.delete(id);
    }
    clearDerivedCaches();
  },

  async openSession(id) {
    // 在“新会话”界面先输入、再点已有会话时，把 __new__ 草稿带过去，
    // 而不是让输入框突然清空。
    const pendingDraft = this.activeView && !this.activeView.threadId
      ? String(this.activeView.prompt || '')
      : '';
    const view = this.activateSessionView(id);
    if (pendingDraft && !view.prompt) {
      saveDraft(id, pendingDraft);
      this.clearDraftFor('');
      view.prompt = pendingDraft;
    }
    if (this.splitView && this.splitView.threadId === id) {
      this.closeSplit();
    }
    this.markSessionRead(id);
    const row = this.sessions.find((item) => item.thread_id === id);
    this._boundProjectId = row && row.workspace_id ? row.workspace_id : '';
    if (row) {
      // 会话绑定必须跟随会话本身；没有 workspace_id 的旧 sandbox
      // 会话要清空项目选择，不能沿用上一个会话的项目。
      this.projectId = row.workspace_id || '';
      const project = this.selectedProject();
      // host 会话的目录只由项目元数据决定；旧的 sandbox 路径不能被
      // 拿来覆盖显示，避免看起来像换了文件夹。
      const label = project && project.kind === 'host'
        ? (project.display_name || project.workspace_id)
        : (row.workspace ||
          (project ? (project.display_name || project.root_path) : ''));
      if (label) {
        this.workspace = label;
        this.workspaceInput = label;
      }
      if (row.access_mode) this.accessMode = row.access_mode;
      if (row.model) this.model = row.model;
      if (row.provider) this.provider = row.provider;
    }
    this.resetSessionUi();
    await this.loadCapabilities();
    if (!view.loaded || view._cacheOnly) {
      const loaded = await this.loadSessionHistory(view);
      if (loaded) view.loaded = true;
    }
    this.$nextTick(() => this.recomputeVirtualRange());
    await this.loadUploads();
    await this.loadApprovals();
    await this.loadSessions(true);
    this.syncUrlState();
  },

  applyHistoryPayload(data) {
    this.messages = [];
    this.reasoningBlocks = [];
    if (Array.isArray(data.transcript) && data.transcript.length) {
      // 桌面 UI 保存过完整轨迹：包含工具卡片、步骤和最终回答。
      this.restoreTranscript(data.transcript);
    } else {
      for (const item of data.messages || []) {
        this.pushMessage(item.role, item.text, true);
      }
    }
  },

  async loadSessionHistory(view = this.activeView) {
    const id = view.threadId;
    if (!id) return false;
    let cacheApplied = false;
    const cached = await readCachedTranscript(id);
    if (cached) {
      this.withView(view, () => this.applyHistoryPayload(cached));
      view._historyItems = Array.isArray(cached.transcript)
        ? cached.transcript
        : [];
      view._historyOffset = view._historyItems.length;
      view.historyHasMore = Boolean(cached.transcript_has_more);
      cacheApplied = true;
    }
    try {
      const response = await authFetch(
        `/api/thread/${encodeURIComponent(id)}?limit=200&offset=0`,
      );
      const data = await readJson(response, '读取历史失败');
      this.withView(view, () => this.applyHistoryPayload(data));
      view._cacheOnly = false;
      view._historyItems = Array.isArray(data.transcript)
        ? data.transcript
        : [];
      view._historyOffset = view._historyItems.length;
      view.historyHasMore = Boolean(data.transcript_has_more);
      void writeCachedTranscript(id, data);
      return true;
    } catch (e) {
      if (cacheApplied) {
        view._cacheOnly = true;
        this.toastMessage('网络不可用，正在显示本地缓存');
        return true;
      }
      this.toastMessage(String(e));
      return false;
    }
  },

  async loadOlderHistory() {
    const view = this.activeView;
    if (!view || !view.threadId || view.historyLoading || !view.historyHasMore) {
      return;
    }
    view.historyLoading = true;
    const scroller = document.querySelector('.transcript') as HTMLElement | null;
    const beforeHeight = scroller ? scroller.scrollHeight : 0;
    try {
      const offset = Number(view._historyOffset || 0);
      const response = await authFetch(
        `/api/thread/${encodeURIComponent(view.threadId)}` +
        `?limit=200&offset=${offset}`,
      );
      const data = await readJson(response, '读取更早历史失败');
      const older = Array.isArray(data.transcript) ? data.transcript : [];
      if (!older.length) {
        view.historyHasMore = false;
        return;
      }
      view._historyItems = prependOlder(older, view._historyItems || []);
      view._historyOffset = offset + older.length;
      view.historyHasMore = Boolean(data.transcript_has_more);
      this.withView(view, () => this.applyHistoryPayload({
        transcript: view._historyItems,
      }));
      this.$nextTick(() => {
        if (!scroller) return;
        scroller.scrollTop = scroller.scrollHeight - beforeHeight;
      });
    } catch (err) {
      this.toastMessage(String(err));
    } finally {
      view.historyLoading = false;
    }
  },

  async loadApprovals() {
    if (!this.threadId) return;
    try {
      const response = await authFetch(
        `/api/approvals/${encodeURIComponent(this.threadId)}`,
      );
      const data = await readJson(response, '读取审批失败');
      this.approvals = data.approvals || [];
      if (this.approvals.length) {
        this.showNextApproval();
        this.stateText = this.t('status.waitingApproval');
      }
    } catch (e) {
      this.approvals = [];
    }
  },

};
