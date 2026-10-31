/* Alpine mixin: layout。
 *
 * 负责 IDE 外壳：活动栏视图、可拖拽侧栏/检查器、状态栏与命令面板。
 * 只改界面状态，不触碰运行/审批/工作区事实。
 */

import { authFetch, readJson } from '../api/http';
import { fuzzyScore, parseCommandQuery } from '../ui/search';

const LAYOUT_KEY = 'codingagent.layout';
const ACTIVITY_WIDTH = 52;
const MIN_SIDEBAR = 220;
const MAX_SIDEBAR = 460;
const MIN_INSPECTOR = 260;
const MAX_INSPECTOR = 560;
const MIN_SPLIT = 320;
const MAX_SPLIT = 760;

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

const VIEW_TITLES: Record<string, string> = {
  sessions: 'nav.sessions',
  files: 'nav.files',
  tree: 'nav.tree',
  git: 'nav.git',
  tasks: 'nav.tasks',
  memory: 'nav.memory',
  audit: 'nav.audit',
  problems: 'nav.problems',
};

export const layoutMixin = {
  initLayout() {
    try {
      const raw = localStorage.getItem(LAYOUT_KEY);
      if (raw) {
        const saved = JSON.parse(raw);
        if (Number.isFinite(saved.sidebarWidth)) {
          this.sidebarWidth = clamp(saved.sidebarWidth, MIN_SIDEBAR, MAX_SIDEBAR);
        }
        if (Number.isFinite(saved.inspectorWidth)) {
          this.inspectorWidth = clamp(saved.inspectorWidth, MIN_INSPECTOR, MAX_INSPECTOR);
        }
        if (Number.isFinite(saved.splitWidth)) {
          this.splitWidth = clamp(saved.splitWidth, MIN_SPLIT, MAX_SPLIT);
        }
        this.sidebarCollapsed = Boolean(saved.sidebarCollapsed);
        this.inspectorCollapsed = Boolean(saved.inspectorCollapsed);
      } else if (window.innerWidth < 1100) {
        // 窄屏首次打开时检查器以浮层形式存在，默认收起。
        this.inspectorCollapsed = true;
      }
    } catch (err) {
      // 布局偏好损坏时回到默认值，不影响启动。
    }
    this.applyViewClasses();
  },

  saveLayout() {
    try {
      localStorage.setItem(LAYOUT_KEY, JSON.stringify({
        sidebarWidth: this.sidebarWidth,
        inspectorWidth: this.inspectorWidth,
        splitWidth: this.splitWidth,
        sidebarCollapsed: this.sidebarCollapsed,
        inspectorCollapsed: this.inspectorCollapsed,
      }));
    } catch (err) {
      // 隐私模式或配额不足时忽略。
    }
  },

  resetLayout() {
    this.sidebarWidth = 280;
    this.inspectorWidth = 340;
    this.splitWidth = 480;
    this.sidebarCollapsed = false;
    this.inspectorCollapsed = false;
    this.saveLayout();
  },

  get layoutStyle() {
    const sidebar = this.sidebarCollapsed ? 0 : this.sidebarWidth;
    const inspector = this.inspectorCollapsed ? 0 : this.inspectorWidth;
    return `--sidebar-width: ${sidebar}px; --inspector-width: ${inspector}px; --split-width: ${this.splitWidth}px;`;
  },

  focusPaneMain() {
    this.activePane = 'main';
  },

  focusPaneSplit() {
    this.activePane = 'split';
  },

  toggleSidebar() {
    this.sidebarCollapsed = !this.sidebarCollapsed;
    this.saveLayout();
  },

  toggleInspector() {
    this.inspectorCollapsed = !this.inspectorCollapsed;
    this.saveLayout();
  },

  startResize(side: string, event: Event) {
    const pointer = event as PointerEvent;
    pointer.preventDefault();
    this._resizing = side;
    document.body.classList.add('is-resizing');
    const move = (moveEvent: PointerEvent) => this.onResize(moveEvent);
    const stop = () => {
      this._resizing = '';
      document.body.classList.remove('is-resizing');
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', stop);
      this.saveLayout();
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', stop, { once: true });
  },

  onResize(event: PointerEvent) {
    if (this._resizing === 'sidebar') {
      this.sidebarWidth = clamp(
        event.clientX - ACTIVITY_WIDTH,
        MIN_SIDEBAR,
        MAX_SIDEBAR,
      );
    } else if (this._resizing === 'inspector') {
      this.inspectorWidth = clamp(
        window.innerWidth - event.clientX,
        MIN_INSPECTOR,
        MAX_INSPECTOR,
      );
    } else if (this._resizing === 'split') {
      const main = document.querySelector('.main') as HTMLElement | null;
      const right = main ? main.getBoundingClientRect().right : window.innerWidth;
      const max = Math.min(MAX_SPLIT, Math.max(MIN_SPLIT, right * 0.7));
      this.splitWidth = clamp(right - event.clientX, MIN_SPLIT, max);
    }
  },

  applyViewClasses() {
    const next: Record<string, string> = {};
    const flags: Record<string, boolean> = {};
    for (const name of Object.keys(VIEW_TITLES)) {
      const active = this.view === name;
      next[name] = active ? 'active' : '';
      flags[name] = active;
    }
    this.viewClasses = next;
    this.viewFlags = flags;
  },

  setView(name: string, load = true, reveal = true) {
    this.view = name;
    this.applyViewClasses();
    if (reveal && this.sidebarCollapsed) this.sidebarCollapsed = false;
    if (name === 'files') this.filesDirty = false;
    if (name === 'tasks') this.tasksDirty = false;
    if (load) {
      if (name === 'files') this.loadSnapshots();
      if (name === 'tree') this.loadTree('');
      if (name === 'git') this.loadGit(false);
      if (name === 'memory') this.loadMemory();
      if (name === 'audit') this.loadAudit();
    }
    if (reveal) this.saveLayout();
  },

  get viewTitle() {
    return this.t(VIEW_TITLES[this.view] || 'nav.sessions');
  },

  get currentSessionTitle() {
    const current = (this.sessions || []).find(
      (item) => item.thread_id === this.threadId,
    );
    if (current) return current.title || current.thread_id;
    return this.threadId || this.t('common.newSession');
  },

  get modelLabel() {
    if (this.model) return this.model;
    if (this.provider) return this.provider;
    return this.t('sidebar.defaultModel');
  },

  get connectionClass() {
    if (this.connectionState === 'streaming' || this.connectionState === 'reconnecting') {
      return 'status-dot-warn';
    }
    if (this.connectionState === 'closed') return 'status-dot-bad';
    return 'status-dot-ok';
  },

  applyTheme(name: string) {
    this.theme = name;
    this.applyBodyClasses();
    this.saveSettings();
  },

  openCommandPalette() {
    this.commandOpen = true;
    this.rememberModalFocus();
    this.commandQuery = '';
    this.commandIndex = 0;
    this.fileSearchResults = [];
    this.fileSearchLoading = false;
    this.remoteSessionResults = [];
    this.sessionSearchLoading = false;
    this._searchSeq += 1;
    this._sessionSearchSeq += 1;
    if (this._searchTimer) clearTimeout(this._searchTimer);
    this.$nextTick(() => {
      const input = this.$refs.commandInput;
      if (input) input.focus();
    });
  },

  closeCommandPalette() {
    this.commandOpen = false;
    this.commandQuery = '';
    this.commandIndex = 0;
    this.fileSearchResults = [];
    this.fileSearchLoading = false;
    this.remoteSessionResults = [];
    this.sessionSearchLoading = false;
    this._searchSeq += 1;
    this._sessionSearchSeq += 1;
    if (this._searchTimer) clearTimeout(this._searchTimer);
    this.$nextTick(() => this.restoreModalFocus());
  },

  onCommandQuery() {
    this.commandIndex = 0;
    this.fileSearchResults = [];
    this.fileSearchLoading = false;
    this.remoteSessionResults = [];
    this.sessionSearchLoading = false;
    this._searchSeq += 1;
    this._sessionSearchSeq += 1;
    if (this._searchTimer) clearTimeout(this._searchTimer);
    const parsed = parseCommandQuery(this.commandQuery);
    if (!this.threadId) return;
    if (parsed.kind === 'file' && parsed.query.length >= 1) {
      this._searchTimer = setTimeout(() => {
        void this.searchFiles(parsed.query);
      }, 180);
      return;
    }
    if (parsed.kind === 'session' && parsed.query.length >= 2) {
      this._searchTimer = setTimeout(() => {
        void this.searchSessionsRemote(parsed.query);
      }, 200);
      return;
    }
    if (parsed.kind === 'all' && parsed.query.length >= 2) {
      this._searchTimer = setTimeout(() => {
        void this.searchFiles(parsed.query);
      }, 220);
    }
  },

  async searchSessionsRemote(query: string) {
    const seq = ++this._sessionSearchSeq;
    this.sessionSearchLoading = true;
    this.fileSearchLoading = true;
    try {
      const params = new URLSearchParams({
        q: query,
        limit: '20',
      });
      const response = await authFetch(
        `/api/sessions/search?${params.toString()}`,
      );
      const data = await readJson(response, '搜索会话失败');
      if (seq !== this._sessionSearchSeq) return;
      if (data.enabled === false) {
        this.remoteSessionResults = [];
        return;
      }
      this.remoteSessionResults = (data.results || []).map(
        (row, index) => ({
          thread_id: String(row.thread_id || ''),
          title: String(row.title || row.thread_id || ''),
          snippet: String(row.snippet || ''),
          _key: `${row.thread_id}:${index}`,
        }),
      );
    } catch (err) {
      if (seq !== this._sessionSearchSeq) return;
      this.remoteSessionResults = [];
    } finally {
      if (seq === this._sessionSearchSeq) {
        this.sessionSearchLoading = false;
        this.fileSearchLoading = false;
      }
    }
  },

  async searchFiles(query: string) {
    if (!this.threadId) return;
    const threadId = this.threadId;
    const seq = ++this._searchSeq;
    this.fileSearchLoading = true;
    try {
      const params = new URLSearchParams({
        thread_id: threadId,
        q: query,
        limit: '30',
      });
      const response = await authFetch(`/api/search?${params.toString()}`);
      const data = await readJson(response, '搜索文件失败');
      if (seq !== this._searchSeq || threadId !== this.threadId) return;
      this.fileSearchResults = (data.results || []).map((row, index) => ({
        ...row,
        _key: `${row.path}:${row.line}:${index}`,
        _hint: row.kind === 'content'
          ? this.t('commands.groups.fileContent')
          : this.t('commands.groups.filePath'),
      }));
    } catch (err) {
      if (seq !== this._searchSeq || threadId !== this.threadId) return;
      this.fileSearchResults = [];
      this.toastMessage(String(err));
    } finally {
      if (seq === this._searchSeq) this.fileSearchLoading = false;
    }
  },

  commandText(id, field, fallback) {
    const key = `commands.${id}.${field}`;
    const value = this.t(key);
    return value === key ? fallback : value;
  },

  commandItems() {
    const commands = [
      { id: 'quick-open', title: '快速打开文件…', hint: '文件', shortcut: 'Ctrl+P', run: () => this.openQuickOpen() },
      { id: 'new-session', title: '新建会话', hint: '会话', run: () => this.newSession() },
      { id: 'settings', title: '打开设置', hint: '界面', run: () => this.toggleSettings() },
      { id: 'recovery', title: '打开恢复中心', hint: '界面', run: () => this.openRecovery() },
      { id: 'notifications', title: '通知中心', hint: '界面', run: () => this.toggleNotifications() },
      { id: 'shortcuts', title: '查看快捷键', hint: '帮助', run: () => this.openShortcuts() },
      { id: 'search-conversation', title: '搜索当前对话', hint: '导航', shortcut: 'Ctrl+Shift+F', run: () => { this.showChat(); this.openMessageSearch(); } },
      { id: 'split-session', title: '当前会话分屏', hint: '视图', run: () => this.openSplitSession(this.threadId) },
      { id: 'compare-runs', title: '运行对比', hint: '会话', run: () => this.openCompare(this.threadId) },
      { id: 'share-session', title: '分享当前会话（只读）', hint: '会话', run: () => this.openShare(this.threadId) },
      { id: 'jump-latest', title: '跳到最新消息', hint: '导航', run: () => this.scrollBottom(true) },
      { id: 'toggle-sidebar', title: '切换侧栏', hint: '布局', run: () => this.toggleSidebar() },
      { id: 'toggle-inspector', title: '切换右侧检查器', hint: '布局', run: () => this.toggleInspector() },
      { id: 'view-sessions', title: '视图：会话', hint: '导航', run: () => this.setView('sessions') },
      { id: 'view-files', title: '视图：文件改动', hint: '导航', run: () => this.setView('files') },
      { id: 'view-tree', title: '视图：文件树', hint: '导航', run: () => this.setView('tree') },
      { id: 'view-git', title: '视图：Git', hint: '导航', run: () => this.setView('git') },
      { id: 'view-tasks', title: '视图：任务', hint: '导航', run: () => this.setView('tasks') },
      { id: 'view-memory', title: '视图：记忆', hint: '导航', run: () => this.setView('memory') },
      { id: 'view-audit', title: '视图：审计', hint: '导航', run: () => this.setView('audit') },
      { id: 'view-problems', title: '视图：问题', hint: '导航', run: () => this.setView('problems') },
      { id: 'tab-plan', title: '检查器：计划', hint: '导航', run: () => this.setTab('plan') },
      { id: 'tab-tools', title: '检查器：工具', hint: '导航', run: () => this.setTab('tools') },
      { id: 'tab-timeline', title: '检查器：时间线', hint: '导航', run: () => this.setTab('timeline') },
      { id: 'close-editor', title: '关闭当前标签', hint: '标签', run: () => this.closeActiveEditorTab() },
      { id: 'close-other-editors', title: '关闭其他标签', hint: '标签', run: () => this.closeOtherEditorTabs() },
      { id: 'close-all-editors', title: '关闭所有文件标签', hint: '标签', run: () => this.closeAllEditorTabs() },
      { id: 'refresh-files', title: '刷新文件快照', hint: '文件', run: () => this.loadSnapshots() },
      { id: 'refresh-tree', title: '刷新文件树', hint: '文件', run: () => this.refreshTree() },
      { id: 'git-diff', title: '查看 Git diff', hint: 'Git', run: () => this.loadGit(true) },
      { id: 'theme-dark', title: '主题：深色', hint: '外观', run: () => this.applyTheme('dark') },
      { id: 'theme-light', title: '主题：浅色', hint: '外观', run: () => this.applyTheme('light') },
      { id: 'theme-auto', title: '主题：跟随系统', hint: '外观', run: () => this.applyTheme('auto') },
      {
        id: 'reasoning',
        title: this.showReasoning ? '隐藏模型推理' : '显示模型推理',
        hint: '外观',
        run: () => this.toggleReasoning(),
      },
      { id: 'reset-layout', title: '重置布局宽度', hint: '布局', run: () => this.resetLayout() },
    ].map((item) => ({
      ...item,
      title: this.commandText(item.id, 'title', item.title),
      hint: this.commandText(item.id, 'hint', item.hint),
      kind: 'command',
      group: this.t('commands.groups.commands'),
    }));
    if (this.threadId && !this.busy) {
      commands.push({
        id: 'compact',
        title: this.commandText('compact', 'title', '压缩上下文并分叉'),
        hint: this.commandText('compact', 'hint', 'Agent'),
        kind: 'command',
        group: this.t('commands.groups.commands'),
        run: () => this.compactContext(),
      });
    }
    const current = this.currentSession();
    if (current) {
      commands.push(
        {
          id: 'pin-session',
          title: this.commandText(
            current.pinned ? 'unpin' : 'pin',
            'title',
            current.pinned ? '取消置顶当前会话' : '置顶当前会话',
          ),
          hint: this.commandText('pin', 'hint', '会话'),
          kind: 'command',
          group: this.t('commands.groups.commands'),
          run: () => this.toggleSessionPin(current),
        },
        {
          id: 'archive-session',
          title: this.commandText(
            current.archived ? 'unarchive' : 'archive',
            'title',
            current.archived ? '取消归档当前会话' : '归档当前会话',
          ),
          hint: this.commandText('archive', 'hint', '会话'),
          kind: 'command',
          group: this.t('commands.groups.commands'),
          run: () => this.toggleSessionArchive(current),
        },
        {
          id: 'tag-session',
          title: this.commandText('tag', 'title', '编辑当前会话标签'),
          hint: this.commandText('tag', 'hint', '会话'),
          kind: 'command',
          group: this.t('commands.groups.commands'),
          run: () => this.editSessionTags(current),
        },
      );
    }
    const parsedQuery = parseCommandQuery(this.commandQuery);
    const remote = (
      parsedQuery.kind === 'session' && parsedQuery.query
    ) ? this.remoteSessionResults : [];
    const sessionRows = remote.length
      ? remote.map((row) => ({
          thread_id: row.thread_id,
          title: row.title,
          detail: row.snippet,
        }))
      : (this.sessions || []).slice(0, 20).map((session) => ({
          thread_id: session.thread_id,
          title: session.title || session.thread_id,
          detail: '',
        }));
    const sessions = sessionRows.map((session) => ({
      id: `session:${session.thread_id}`,
      title: session.title,
      detail: session.detail,
      hint: this.t('commands.groups.session'),
      kind: 'session',
      group: this.t('commands.groups.session'),
      run: () => this.openSession(session.thread_id),
    }));
    const files = (this.fileSearchResults || []).map((row) => ({
      id: `file:${row._key}`,
      title: row.line ? `${row.path}:${row.line}` : row.path,
      hint: row._hint || this.t('commands.groups.file'),
      detail: row.preview || '',
      kind: 'file',
      group: this.t('commands.groups.file'),
      run: () => this.openFile(row.path, row.line),
    }));
    const items = [...commands, ...sessions, ...files];
    if (!String(this.commandQuery || '').trim()) {
      const byId = new Map(commands.map((item) => [item.id, item]));
      const recent = this.recentCommands
        .map((id) => byId.get(id))
        .filter(Boolean)
        .map((item) => ({
          ...item,
          id: `recent:${item.id}`,
          group: this.t('commands.groups.recent'),
        }));
      return [...recent, ...items];
    }
    return items;
  },

  filteredCommands() {
    const parsed = parseCommandQuery(this.commandQuery);
    const items = this.commandItems().filter(
      (item) => parsed.kind === 'all' || item.kind === parsed.kind,
    );
    if (!parsed.query) return items.slice(0, 40);
    const order = this.commandGroupOrder();
    return items
      .map((item) => ({
        item,
        score: Math.max(
          fuzzyScore(item.title, parsed.query),
          fuzzyScore(item.hint, parsed.query),
          fuzzyScore(item.detail || '', parsed.query),
          fuzzyScore(item.id, parsed.query),
        ),
      }))
      .filter((entry) => entry.score >= 0)
      .sort((left, right) => {
        const leftGroup = order.indexOf(left.item.group);
        const rightGroup = order.indexOf(right.item.group);
        if (leftGroup !== rightGroup) return leftGroup - rightGroup;
        return right.score - left.score;
      })
      .map((entry) => entry.item)
      .slice(0, 40);
  },

  commandClass(index: number) {
    return index === this.commandIndex ? 'selected' : '';
  },

  hoverCommand(index: number) {
    this.commandIndex = index;
  },

  runCommand(command: any) {
    if (!command) return;
    if (command.kind === 'command') this.recordCommand(command.id);
    this.closeCommandPalette();
    command.run();
  },

  onCommandKeydown(event: KeyboardEvent) {
    const items = this.filteredCommands();
    if (event.key === 'Escape') {
      event.preventDefault();
      this.closeCommandPalette();
      return;
    }
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      if (items.length) this.commandIndex = (this.commandIndex + 1) % items.length;
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      if (items.length) {
        this.commandIndex = (this.commandIndex - 1 + items.length) % items.length;
      }
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      this.runCommand(items[this.commandIndex]);
    }
  },
};
