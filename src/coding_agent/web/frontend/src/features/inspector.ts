/* Alpine mixin: inspector。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
  readJson,
} from '../api/http';
import { escapeHtml } from '../ui/format';
import { diffRowsFromText } from '../ui/diff';
import { includesQuery } from '../ui/search';

export const inspectorMixin = {
  async compactContext() {
    if (!this.threadId || this.busy) return;
    if (!window.confirm('把较早的对话压缩成摘要，并分叉出一个新会话？原会话会保留。')) {
      return;
    }
    try {
      const response = await authFetch(
        `/api/context/${encodeURIComponent(this.threadId)}/compact`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ keep_recent: 6 }),
        },
      );
      const data = await readJson(response, '压缩失败');
      await this.openSession(data.thread_id);
      this.toastMessage(`已压缩并分叉到 ${data.thread_id}`);
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async loadSnapshots() {
    if (!this.threadId) return;
    try {
      const response = await authFetch(`/api/snapshots?thread_id=${encodeURIComponent(this.threadId)}&limit=50`);
      const data = await readJson(response, '读取快照失败');
      this.files = data.snapshots || [];
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  checkpointText(checkpoint) {
    return `step ${checkpoint.step} · ${checkpoint.checkpoint_id} · ${checkpoint.preview || ''}`;
  },

  async loadCheckpoints() {
    if (!this.threadId) return;
    try {
      const response = await authFetch(
        `/api/checkpoints/${encodeURIComponent(this.threadId)}?limit=20`,
      );
      const data = await readJson(response, '读取 checkpoint 失败');
      this.checkpoints = data.checkpoints || [];
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async forkCheckpoint(checkpointId) {
    if (!this.threadId) return;
    if (!window.confirm('从该 checkpoint 分叉出一个新会话？')) return;
    try {
      const params = new URLSearchParams();
      if (checkpointId) params.set('checkpoint_id', checkpointId);
      const response = await authFetch(
        `/api/fork/${encodeURIComponent(this.threadId)}?${params.toString()}`,
        { method: 'POST' },
      );
      const data = await readJson(response, '分叉失败');
      await this.openSession(data.thread_id);
      this.toastMessage(`已分叉到 ${data.thread_id}`);
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async rewindTo(checkpointId) {
    if (!this.threadId) return;
    if (!window.confirm('回退到该 checkpoint？历史不会被删除，会新建分支。')) return;
    try {
      const params = new URLSearchParams({ steps: '1' });
      if (checkpointId) params.set('checkpoint_id', checkpointId);
      const response = await authFetch(
        `/api/rewind/${encodeURIComponent(this.threadId)}?${params.toString()}`,
        { method: 'POST' },
      );
      await readJson(response, '回退失败');
      this.toastMessage('已回退，历史仍保留');
      await this.openSession(this.threadId);
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async restoreRun() {
    if (!this.threadId) return;
    try {
      const started = await authFetch(
        `/api/run_started/${encodeURIComponent(this.threadId)}`,
      );
      const data = await readJson(started, '读取本轮起点失败');
      if (!data.prefix) {
        this.toastMessage('没有找到本轮快照起点');
        return;
      }
      const previewResponse = await authFetch('/api/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          thread_id: this.threadId,
          prefix: data.prefix,
          dry_run: true,
        }),
      });
      const preview = await readJson(previewResponse, '回滚预览失败');
      const paths = (preview.results || []).map((item) => item.path).join(', ');
      if (!window.confirm(`将回滚本轮改动的 ${paths || 0} 个文件：\n${paths}\n继续？`)) {
        return;
      }
      const response = await authFetch('/api/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ thread_id: this.threadId, prefix: data.prefix }),
      });
      const result = await readJson(response, '回滚本轮失败');
      this.toastMessage(`已回滚 ${(result.results || []).length} 个文件`);
      await this.loadSnapshots();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async loadDiff(snapshotId, path) {
    if (!this.threadId) return;
    const id = `diff:${snapshotId || ''}:${path || ''}`;
    if (this.editorTabs.some((tab) => tab.id === id)) {
      this.activateEditorTab(id);
    }
    const params = new URLSearchParams({ thread_id: this.threadId });
    if (snapshotId) params.set('snapshot_id', snapshotId);
    if (path) params.set('path', path);
    try {
      const response = await authFetch(`/api/diff?${params.toString()}`);
      const data = await readJson(response, '读取 diff 失败');
      this.openDiffTab(
        id,
        path ? `Diff · ${path}` : 'Diff · 本轮改动',
        path || '',
        data.diff || '（没有差异）',
      );
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  openDiffTab(id, title, path, text) {
    const diffText = String(text || '（没有差异）');
    const lines = diffText.split('\n');
    const truncated = lines.length > 200;
    const display = truncated
      ? `${lines.slice(0, 200).join('\n')}\n…（共 ${lines.length} 行，已折叠）`
      : diffText;
    const payload = {
      id,
      kind: 'diff',
      title,
      path,
      diffText,
      diffDisplay: display,
      diffTruncated: truncated,
      diffExpanded: !truncated,
      diffPath: path,
    };
    const existing = this.editorTabs.find((tab) => tab.id === id);
    if (existing) Object.assign(existing, payload);
    else this.editorTabs = [...this.editorTabs, payload];
    this.activateEditorTab(id);
  },

  async expandDiff() {
    this.diffExpanded = true;
    this.diffDisplay = this.diffText;
    this.syncActiveEditor();
  },

  activeEditorTab() {
    return this.editorTabs.find((tab) => tab.id === this.activeEditorId) || null;
  },

  editorTabClass(id) {
    return this.mainTab !== 'chat' && this.activeEditorId === id ? 'active' : '';
  },

  activateEditorTab(id) {
    const tab = this.editorTabs.find((item) => item.id === id);
    if (!tab) return;
    this.activeEditorId = id;
    if (tab.kind === 'file') {
      this.fileView = tab.fileView;
      this.mainTab = 'file';
      return;
    }
    this.diffText = tab.diffText;
    this.diffDisplay = tab.diffDisplay;
    this.diffTruncated = tab.diffTruncated;
    this.diffExpanded = tab.diffExpanded;
    this.diffPath = tab.diffPath;
    this.mainTab = 'diff';
  },

  syncActiveEditor() {
    const tab = this.editorTabs.find((item) => item.id === this.activeEditorId);
    if (!tab) return;
    if (tab.kind === 'file') {
      tab.fileView = this.fileView;
      return;
    }
    tab.diffText = this.diffText;
    tab.diffDisplay = this.diffDisplay;
    tab.diffTruncated = this.diffTruncated;
    tab.diffExpanded = this.diffExpanded;
    tab.diffPath = this.diffPath;
  },

  closeEditorTab(id) {
    const index = this.editorTabs.findIndex((tab) => tab.id === id);
    if (index < 0) return;
    const wasActive = this.activeEditorId === id;
    this.editorTabs = this.editorTabs.filter((tab) => tab.id !== id);
    if (this.mainTab === 'chat') {
      if (wasActive) this.activeEditorId = '';
      return;
    }
    if (!wasActive) return;
    const next = this.editorTabs[Math.min(index, this.editorTabs.length - 1)];
    if (next) {
      this.activateEditorTab(next.id);
      return;
    }
    this.activeEditorId = '';
    this.mainTab = 'chat';
    this.fileView = { path: '', size: 0, _text: '', _html: '' };
    this.diffText = '';
    this.diffDisplay = '';
    this.diffPath = '';
    this.diffTruncated = false;
    this.diffExpanded = false;
  },

  closeActiveEditorTab() {
    if (this.mainTab === 'chat') return;
    if (this.activeEditorId) this.closeEditorTab(this.activeEditorId);
  },

  closeOtherEditorTabs() {
    if (this.mainTab === 'chat') {
      this.closeAllEditorTabs();
      return;
    }
    const active = this.activeEditorTab();
    this.editorTabs = active ? [active] : [];
  },

  closeAllEditorTabs() {
    this.editorTabs = [];
    this.activeEditorId = '';
    this.mainTab = 'chat';
    this.fileView = { path: '', size: 0, _text: '', _html: '' };
    this.diffText = '';
    this.diffDisplay = '';
    this.diffPath = '';
    this.diffTruncated = false;
    this.diffExpanded = false;
  },

  async restoreSnapshot(snapshotId, path) {
    if (!this.threadId) return;
    if (!window.confirm(`回滚 ${path}？回滚前会为当前内容留底。`)) return;
    try {
      const response = await authFetch('/api/restore', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          thread_id: this.threadId,
          path,
          snapshot_id: snapshotId,
        }),
      });
      const data = await readJson(response, '回滚失败');
      this.toastMessage(data.ok ? `已回滚：${data.path}` : (data.message || '回滚失败'));
      await this.loadSnapshots();
      await this.loadDiff('', path);
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  acceptFile(item) {
    this.files = this.files.filter((row) => row._key !== item._key);
    this.toastMessage(`已接受（保留当前内容）：${item.path}`);
  },

  async loadTreeDir(path = '') {
    if (!this.threadId) {
      this.toastMessage('请先创建会话');
      return;
    }
    this.treeLoadingMap = { ...this.treeLoadingMap, [path]: true };
    try {
      const params = new URLSearchParams({
        thread_id: this.threadId,
        path,
      });
      const response = await authFetch(`/api/files?${params.toString()}`);
      const data = await readJson(response, '读取文件树失败');
      const entries = (data.entries || []).map((entry) => ({
        ...entry,
        _key: entry.path,
        _kind: entry.is_dir ? '目录' : '文件',
        _meta: entry.is_dir ? '' : this.formatBytes(entry.size),
        _cls: entry.is_dir ? 'tree-dir' : 'tree-file',
      }));
      this.treeChildren = { ...this.treeChildren, [path]: entries };
    } catch (err) {
      this.toastMessage(String(err));
    } finally {
      this.treeLoadingMap = { ...this.treeLoadingMap, [path]: false };
    }
  },

  async loadTree(path = '') {
    if (!this.threadId) {
      this.toastMessage('请先创建会话');
      return;
    }
    if (!this.treeChildren['']) await this.loadTreeDir('');
    if (path) {
      this.treeExpanded = { ...this.treeExpanded, [path]: true };
      if (!this.treeChildren[path]) await this.loadTreeDir(path);
    }
  },

  async refreshTree() {
    if (!this.threadId) {
      this.toastMessage('请先创建会话');
      return;
    }
    const expanded = Object.keys(this.treeExpanded).filter(
      (path) => this.treeExpanded[path],
    );
    this.treeChildren = {};
    this.treeLoadingMap = {};
    await this.loadTreeDir('');
    for (const path of expanded) {
      await this.loadTreeDir(path);
    }
  },

  async toggleTreeDir(entry) {
    if (!entry || !entry.is_dir) return;
    const path = entry.path;
    const next = !this.treeExpanded[path];
    this.treeExpanded = { ...this.treeExpanded, [path]: next };
    if (next && !this.treeChildren[path]) {
      await this.loadTreeDir(path);
    }
  },

  collapseAllTree() {
    this.treeExpanded = {};
  },

  treeVisibleRows() {
    const rows: any[] = [];
    const walk = (path: string, depth: number) => {
      for (const entry of this.treeChildren[path] || []) {
        const expanded = Boolean(this.treeExpanded[entry.path]);
        const loading = Boolean(this.treeLoadingMap[entry.path]);
        rows.push({
          ...entry,
          _depth: depth,
          _expanded: expanded,
          _kind: entry.is_dir ? (loading ? '…' : (expanded ? '▾' : '▸')) : '·',
        });
        if (entry.is_dir && expanded) walk(entry.path, depth + 1);
      }
    };
    walk('', 0);
    return rows;
  },

  filteredTreeRows() {
    if (!String(this.treeQuery || '').trim()) return this.treeVisibleRows();
    const all: any[] = [];
    const walk = (path: string) => {
      for (const entry of this.treeChildren[path] || []) {
        all.push({ ...entry, _depth: 0, _kind: entry.is_dir ? '目录' : '·' });
        if (entry.is_dir) walk(entry.path);
      }
    };
    walk('');
    return all.filter((entry) => (
      includesQuery(`${entry.name} ${entry.path}`, this.treeQuery)
    ));
  },

  treeRootLoading() {
    return Boolean(this.treeLoadingMap['']);
  },

  canOpenFileInVscode() {
    const root = String(this.workspaceInput || this.workspace || '').trim();
    return Boolean(
      this.desktopAvailable && this.fileView.path && root.startsWith('/')
    );
  },

  async openFileInVscode() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge) {
      this.toastMessage('浏览器模式不支持在 VS Code 打开');
      return;
    }
    const root = String(this.workspaceInput || this.workspace || '').replace(/\/+$/, '');
    if (!root.startsWith('/') || !this.fileView.path) {
      this.toastMessage('仅支持 WSL 工作区中的文件');
      return;
    }
    try {
      const result = await bridge.open_in_vscode(`${root}/${this.fileView.path}`);
      this.toastMessage(result.ok ? '已在 VS Code 打开' : result.error);
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async openTreeEntry(entry) {
    if (entry.is_dir) await this.toggleTreeDir(entry);
    else await this.openFile(entry.path);
  },

  async openFile(path, line = 0, force = false) {
    if (!this.threadId) return;
    const id = `file:${path}`;
    const existing = this.editorTabs.find((tab) => tab.id === id);
    if (existing && !force) {
      this.activateEditorTab(id);
      this.$nextTick(() => this.scrollFileToLine(line));
      return;
    }
    try {
      const params = new URLSearchParams({
        thread_id: this.threadId,
        path,
      });
      const response = await authFetch(`/api/file?${params.toString()}`);
      const data = await readJson(response, '读取文件失败');
      const text = data.binary
        ? '（二进制文件，不展示内容）'
        : data.content + (data.truncated ? '\n…（已截断）' : '');
      const fileView = {
        ...data,
        _size: this.formatBytes(data.size),
        _lang: this.fileLanguage(path),
        _text: text,
        _html: this.highlightCode(path, text, data.binary),
      };
      if (existing) {
        existing.fileView = fileView;
        existing.path = data.path || path;
        existing.title = path;
        existing.stale = false;
      } else {
        this.editorTabs = [...this.editorTabs, {
          id,
          kind: 'file',
          path: data.path || path,
          title: path,
          fileView,
          stale: false,
        }];
      }
      this.activateEditorTab(id);
      this.$nextTick(() => this.scrollFileToLine(line));
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async refreshActiveFile() {
    if (this.mainTab !== 'file') return;
    const tab = this.activeEditorTab();
    if (tab && tab.kind === 'file') {
      await this.openFile(tab.path, 0, true);
    }
  },

  scrollFileToLine(line) {
    if (!line) return;
    const view = document.querySelector('.editor-code') as HTMLElement | null;
    if (!view) return;
    const style = window.getComputedStyle(view);
    const lineHeight = Number.parseFloat(style.lineHeight) || 18;
    view.scrollTop = Math.max(0, (Number(line) - 3) * lineHeight);
  },

  fileLanguage(path) {
    const ext = String(path || '').split('.').pop().toLowerCase();
    const languages: Record<string, string> = {
      py: 'python',
      js: 'javascript',
      mjs: 'javascript',
      cjs: 'javascript',
      ts: 'typescript',
      tsx: 'typescript',
      jsx: 'javascript',
      json: 'json',
      yaml: 'yaml',
      yml: 'yaml',
      toml: 'ini',
      md: 'markdown',
      html: 'xml',
      htm: 'xml',
      xml: 'xml',
      css: 'css',
      scss: 'scss',
      sh: 'bash',
      bash: 'bash',
      ps1: 'powershell',
      java: 'java',
      kt: 'kotlin',
      go: 'go',
      rs: 'rust',
      c: 'c',
      h: 'c',
      cpp: 'cpp',
      hpp: 'cpp',
      sql: 'sql',
      dockerfile: 'dockerfile',
    };
    return languages[ext] || '';
  },

  highlightCode(path, text, binary = false) {
    const source = String(text || '');
    if (!source || binary) return escapeHtml(source);
    const language = this.fileLanguage(path);
    try {
      if (
        language
        && window.hljs
        && typeof window.hljs.getLanguage === 'function'
        && window.hljs.getLanguage(language)
      ) {
        return window.hljs.highlight(source, { language }).value;
      }
    } catch (err) {
      // 高亮失败时退回纯文本，不能影响预览。
    }
    return escapeHtml(source);
  },

  mainTabClass(name) {
    return this.mainTab === name ? 'active' : '';
  },

  showChat() {
    this.mainTab = 'chat';
  },

  closeFileTab() {
    this.closeActiveEditorTab();
  },

  closeDiffTab() {
    this.closeActiveEditorTab();
  },

  diffRows() {
    return diffRowsFromText(this.diffDisplay);
  },

  async loadGit(diff = false) {
    if (!this.threadId) {
      this.toastMessage('请先创建会话');
      return;
    }
    this.gitLoading = true;
    try {
      const params = new URLSearchParams({
        thread_id: this.threadId,
        diff: diff ? 'true' : 'false',
      });
      const response = await authFetch(`/api/git?${params.toString()}`);
      const data = await readJson(response, '读取 Git 状态失败');
      this.gitInfo = data.is_repo
        ? {
            ...data,
            _state: data.changes.length
              ? `${data.changes.length} 个未提交改动`
              : '工作区干净',
            _branch: `${data.branch || '(detached)'}` +
              (data.ahead || data.behind
                ? ` · ahead ${data.ahead} / behind ${data.behind}`
                : ''),
            _changes: (data.changes || []).map((row, i) => ({
              ...row,
              _key: `${i}:${row.path}`,
              _text: `${row.status || '?'} ${row.path}`,
            })),
            _commits: (data.commits || []).map((row) => ({
              ...row,
              _key: row.hash,
              _text: `${row.hash} ${row.date} ${row.subject}`,
            })),
          }
        : { ...data, _state: data.error || '不是 Git 仓库', _changes: [], _commits: [] };
      this.gitDiffText = diff
        ? `${data.diff || ''}\n${data.staged_diff || ''}`.trim() ||
          (data.diff_error
            ? `diff 读取失败：${data.diff_error}`
            : '（没有差异）')
        : '';
      if (diff) {
        this.openDiffTab('diff:git', 'Git diff', 'Git', this.gitDiffText);
      }
    } catch (err) {
      this.toastMessage(String(err));
    } finally {
      this.gitLoading = false;
    }
  },

};
