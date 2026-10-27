/* Alpine mixin: layout。
 *
 * 负责 IDE 外壳：活动栏视图、可拖拽侧栏/检查器、状态栏与命令面板。
 * 只改界面状态，不触碰运行/审批/工作区事实。
 */

import { authFetch, readJson } from '../api/http';

const LAYOUT_KEY = 'codingagent.layout';
const ACTIVITY_WIDTH = 52;
const MIN_SIDEBAR = 220;
const MAX_SIDEBAR = 460;
const MIN_INSPECTOR = 260;
const MAX_INSPECTOR = 560;

function clamp(value: number, min: number, max: number) {
  return Math.min(max, Math.max(min, value));
}

const VIEW_TITLES: Record<string, string> = {
  sessions: '会话',
  files: '文件改动',
  tree: '文件树',
  git: 'Git',
  tasks: '任务',
  memory: '记忆',
  audit: '审计',
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
    this.sidebarCollapsed = false;
    this.inspectorCollapsed = false;
    this.saveLayout();
  },

  layoutStyle() {
    const sidebar = this.sidebarCollapsed ? 0 : this.sidebarWidth;
    const inspector = this.inspectorCollapsed ? 0 : this.inspectorWidth;
    return `--sidebar-width: ${sidebar}px; --inspector-width: ${inspector}px;`;
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
    }
  },

  applyViewClasses() {
    const next: Record<string, string> = {};
    for (const name of Object.keys(VIEW_TITLES)) {
      next[name] = this.view === name ? 'active' : '';
    }
    this.viewClasses = next;
  },

  setView(name: string, load = true, reveal = true) {
    this.view = name;
    this.applyViewClasses();
    if (reveal && this.sidebarCollapsed) this.sidebarCollapsed = false;
    if (name === 'files') this.filesDirty = false;
    if (name === 'tasks') this.tasksDirty = false;
    if (load) {
      if (name === 'files') this.loadSnapshots();
      if (name === 'tree') this.loadTree(this.treePath);
      if (name === 'git') this.loadGit(false);
      if (name === 'memory') this.loadMemory();
      if (name === 'audit') this.loadAudit();
    }
    if (reveal) this.saveLayout();
  },

  viewTitle() {
    return VIEW_TITLES[this.view] || '会话';
  },

  currentSessionTitle() {
    const current = (this.sessions || []).find(
      (item) => item.thread_id === this.threadId,
    );
    if (current) return current.title || current.thread_id;
    return this.threadId || '新会话';
  },

  modelLabel() {
    if (this.model) return this.model;
    if (this.provider) return this.provider;
    return '默认模型';
  },

  connectionClass() {
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
    this.commandQuery = '';
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

  closeCommandPalette() {
    this.commandOpen = false;
    this.commandQuery = '';
    this.commandIndex = 0;
    this.fileSearchResults = [];
    this.fileSearchLoading = false;
    this._searchSeq += 1;
    if (this._searchTimer) clearTimeout(this._searchTimer);
  },

  onCommandQuery() {
    this.commandIndex = 0;
    this.fileSearchResults = [];
    this.fileSearchLoading = false;
    this._searchSeq += 1;
    if (this._searchTimer) clearTimeout(this._searchTimer);
    const query = String(this.commandQuery || '').trim();
    if (query.length < 2 || !this.threadId) return;
    this._searchTimer = setTimeout(() => {
      void this.searchFiles(query);
    }, 220);
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
        _hint: row.kind === 'content' ? '文件内容' : '文件路径',
      }));
    } catch (err) {
      if (seq !== this._searchSeq || threadId !== this.threadId) return;
      this.fileSearchResults = [];
      this.toastMessage(String(err));
    } finally {
      if (seq === this._searchSeq) this.fileSearchLoading = false;
    }
  },

  commandItems() {
    const items: any[] = [
      { id: 'new-session', title: '新建会话', hint: '会话', run: () => this.newSession() },
      { id: 'settings', title: '打开设置', hint: '界面', run: () => this.toggleSettings() },
      { id: 'recovery', title: '打开恢复中心', hint: '界面', run: () => this.openRecovery() },
      { id: 'notifications', title: '通知中心', hint: '界面', run: () => this.toggleNotifications() },
      { id: 'shortcuts', title: '查看快捷键', hint: '帮助', run: () => { this.shortcutsOpen = true; } },
      { id: 'toggle-sidebar', title: '切换侧栏', hint: '布局', run: () => this.toggleSidebar() },
      { id: 'toggle-inspector', title: '切换右侧检查器', hint: '布局', run: () => this.toggleInspector() },
      { id: 'view-sessions', title: '视图：会话', hint: '导航', run: () => this.setView('sessions') },
      { id: 'view-files', title: '视图：文件改动', hint: '导航', run: () => this.setView('files') },
      { id: 'view-tree', title: '视图：文件树', hint: '导航', run: () => this.setView('tree') },
      { id: 'view-git', title: '视图：Git', hint: '导航', run: () => this.setView('git') },
      { id: 'view-tasks', title: '视图：任务', hint: '导航', run: () => this.setView('tasks') },
      { id: 'view-memory', title: '视图：记忆', hint: '导航', run: () => this.setView('memory') },
      { id: 'view-audit', title: '视图：审计', hint: '导航', run: () => this.setView('audit') },
      { id: 'tab-plan', title: '检查器：计划', hint: '导航', run: () => this.setTab('plan') },
      { id: 'tab-tools', title: '检查器：工具', hint: '导航', run: () => this.setTab('tools') },
      { id: 'tab-timeline', title: '检查器：时间线', hint: '导航', run: () => this.setTab('timeline') },
      { id: 'refresh-files', title: '刷新文件快照', hint: '文件', run: () => this.loadSnapshots() },
      { id: 'refresh-tree', title: '刷新文件树', hint: '文件', run: () => this.loadTree(this.treePath) },
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
    ];
    if (this.threadId && !this.busy) {
      items.push({
        id: 'compact',
        title: '压缩上下文并分叉',
        hint: 'Agent',
        run: () => this.compactContext(),
      });
    }
    for (const session of (this.sessions || []).slice(0, 20)) {
      items.push({
        id: `session:${session.thread_id}`,
        title: session.title || session.thread_id,
        hint: '会话',
        run: () => this.openSession(session.thread_id),
      });
    }
    for (const row of this.fileSearchResults || []) {
      items.push({
        id: `file:${row._key}`,
        title: row.line ? `${row.path}:${row.line}` : row.path,
        hint: row._hint || '文件',
        detail: row.preview || '',
        run: () => this.openFile(row.path, row.line),
      });
    }
    return items;
  },

  filteredCommands() {
    const query = String(this.commandQuery || '').trim().toLowerCase();
    const items = this.commandItems();
    if (!query) return items.slice(0, 40);
    const tokens = query.split(/\s+/).filter(Boolean);
    return items
      .filter((item) => {
        const haystack = (
          `${item.title} ${item.hint} ${item.id} ${item.detail || ''}`
        ).toLowerCase();
        return tokens.every((token) => haystack.includes(token));
      })
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
