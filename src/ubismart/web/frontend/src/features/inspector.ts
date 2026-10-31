/* Alpine mixin: inspector。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
  readJson,
} from '../api/http';
import { escapeHtml } from '../ui/format';
import {
  diffRowsFromText,
  splitDiffRows,
  wordDiffMarkers,
} from '../ui/diff';
import { isWindowsPath, isWslPath } from '../ui/paths';
import { includesQuery } from '../ui/search';
import { fileIcon, treeGuides } from '../ui/file-icons';
import { buildGitStatusMap, gitTreeFields } from '../ui/git';
import { detectViewerKind } from './viewer-kind';
import { buildFileView as buildViewerFileView } from './viewers/registry';
import { PDF_DOCS, XLSX_BOOKS } from './viewers/state';
import {
  applySheetWindow,
  selectXlsxCellData,
  syncSpacerHeights,
  XLSX_DEFAULT_VIEWPORT,
} from './viewers/table';

function escapeXmlAttr(text: string) {
  return String(text || '')
    .replace(/&/g, '&amp;')
    .replace(/"/g, '&quot;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

function pdfTextSegments(text: string, query: string) {
  const source = String(text || '');
  const needle = String(query || '').toLowerCase();
  if (!needle) return [{ text: source, hit: false }];
  const lower = source.toLowerCase();
  const parts: Array<{ text: string; hit: boolean }> = [];
  let start = 0;
  while (start < source.length) {
    const index = lower.indexOf(needle, start);
    if (index < 0) {
      parts.push({ text: source.slice(start), hit: false });
      break;
    }
    if (index > start) {
      parts.push({ text: source.slice(start, index), hit: false });
    }
    parts.push({ text: source.slice(index, index + needle.length), hit: true });
    start = index + needle.length;
  }
  return parts.length ? parts : [{ text: source, hit: false }];
}

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
      _isFile: false,
      _isDiff: true,
      _active: false,
      _cls: '',
    };
    const existing = this.editorTabs.find((tab) => tab.id === id);
    if (existing) Object.assign(existing, payload);
    else this.editorTabs = [...this.editorTabs, payload];
    this.activateEditorTab(id);
  },

  async expandDiff() {
    this.diffExpanded = true;
    this.diffDisplay = this.diffText;
    this.rebuildDiffRows();
    this.syncActiveEditor();
  },

  setDiffView(name) {
    this.diffView = name;
    this.diffViewFlags = {
      unified: name === 'unified',
      split: name === 'split',
    };
    this.diffViewClasses = {
      unified: name === 'unified' ? 'active' : '',
      split: name === 'split' ? 'active' : '',
    };
  },

  renderMarkedDiff(marked, path) {
    const html = this.highlightCode(path, marked);
    const replace = (value) => value
      .replaceAll('\u0001', '<mark class="diff-word">')
      .replaceAll('\u0002', '</mark>');
    if (html.includes('\u0001') || html.includes('\u0002')) {
      return replace(html);
    }
    return replace(escapeHtml(marked));
  },

  rebuildDiffRows() {
    const raw = diffRowsFromText(this.diffDisplay);
    const path = this.diffPath && this.diffPath !== 'Git'
      ? this.diffPath
      : '';
    let index = 0;
    while (index < raw.length) {
      const row = raw[index];
      if (row._kind === 'del') {
        const dels = [];
        const adds = [];
        while (index < raw.length && raw[index]._kind === 'del') {
          dels.push(raw[index]);
          index += 1;
        }
        while (index < raw.length && raw[index]._kind === 'add') {
          adds.push(raw[index]);
          index += 1;
        }
        const count = Math.max(dels.length, adds.length);
        for (let i = 0; i < count; i += 1) {
          const left = dels[i];
          const right = adds[i];
          if (left && right) {
            const marks = wordDiffMarkers(left._code, right._code);
            left._html = this.renderMarkedDiff(marks.old, path);
            right._html = this.renderMarkedDiff(marks.new, path);
          } else if (left) {
            left._html = this.highlightCode(path, left._code);
          } else if (right) {
            right._html = this.highlightCode(path, right._code);
          }
        }
        continue;
      }
      if (row._kind === 'add' || row._kind === 'ctx') {
        row._html = this.highlightCode(path, row._code);
      } else {
        row._html = escapeHtml(row.text);
      }
      index += 1;
    }
    this.diffRowsData = raw;
    this.splitDiffRowsData = splitDiffRows(raw);
  },

  goToFileLine() {
    const line = Number.parseInt(this.fileLineInput, 10);
    if (!Number.isFinite(line) || line <= 0) {
      this.toastMessage('请输入有效行号');
      return;
    }
    this.scrollFileToLine(line);
  },

  activeEditorTab() {
    return this.editorTabs.find((tab) => tab.id === this.activeEditorId) || null;
  },

  editorTabClass(id) {
    return this.mainTab !== 'chat' && this.activeEditorId === id ? 'active' : '';
  },

  setMainTabFlags(name) {
    this.mainTabFlags = {
      chat: name === 'chat',
      file: name === 'file',
      diff: name === 'diff',
    };
    this.mainTabClasses = {
      chat: name === 'chat' ? 'active' : '',
      file: name === 'file' ? 'active' : '',
      diff: name === 'diff' ? 'active' : '',
    };
    for (const tab of this.editorTabs) {
      tab._active = name !== 'chat' && tab.id === this.activeEditorId;
      tab._cls = tab._active ? 'active' : '';
      tab._tabindex = tab._active ? 0 : -1;
    }
  },

  onMainTabKeydown(event) {
    const tabs = Array.from(
      document.querySelectorAll('.main-tab'),
    ) as HTMLElement[];
    if (!tabs.length) return;
    const index = tabs.indexOf(document.activeElement as HTMLElement);
    if (index < 0) return;
    const key = event.key;
    if (
      key === 'ArrowRight'
      || key === 'ArrowLeft'
      || key === 'Home'
      || key === 'End'
    ) {
      event.preventDefault();
      let next = index;
      if (key === 'ArrowRight') next = (index + 1) % tabs.length;
      if (key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length;
      if (key === 'Home') next = 0;
      if (key === 'End') next = tabs.length - 1;
      tabs[next].focus();
      tabs[next].click();
      return;
    }
    if (key === 'Delete' || key === 'Backspace') {
      const id = tabs[index].getAttribute('data-id') || '';
      if (!id) return;
      event.preventDefault();
      this.closeEditorTab(id);
      this.$nextTick(() => {
        const remaining = Array.from(
          document.querySelectorAll('.main-tab'),
        ) as HTMLElement[];
        const target = remaining[Math.min(index, remaining.length - 1)];
        if (target) target.focus();
      });
    }
  },

  activateEditorTab(id) {
    const tab = this.editorTabs.find((item) => item.id === id);
    if (!tab) return;
    this.activeEditorId = id;
    if (tab.kind === 'file') {
      this.fileView = tab.fileView;
      this.mainTab = 'file';
      this.setMainTabFlags('file');
      this.$nextTick(() => this.xlsxSyncSpacers());
      return;
    }
    this.diffText = tab.diffText;
    this.diffDisplay = tab.diffDisplay;
    this.diffTruncated = tab.diffTruncated;
    this.diffExpanded = tab.diffExpanded;
    this.diffPath = tab.diffPath;
    this.rebuildDiffRows();
    this.mainTab = 'diff';
    this.setMainTabFlags('diff');
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

  releasePdfView(view) {
    const key = view && view._pdfKey;
    if (!key) return;
    const doc = PDF_DOCS.get(key);
    PDF_DOCS.delete(key);
    try {
      doc?.destroy?.();
    } catch (err) {
      // 文档已经释放时忽略。
    }
  },

  closeEditorTab(id) {
    const index = this.editorTabs.findIndex((tab) => tab.id === id);
    if (index < 0) return;
    const wasActive = this.activeEditorId === id;
    const closing = this.editorTabs[index];
    if (closing && closing.kind === 'file') {
      this.releasePdfView(closing.fileView);
      const xlsxKey = closing.fileView && closing.fileView._xlsxKey;
      if (xlsxKey) XLSX_BOOKS.delete(xlsxKey);
    }
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
    this.setMainTabFlags('chat');
    this.fileView = {
      path: '', size: 0, _text: '', _html: '',
      _lang: '', _size: '', _lineNumbers: '', _crumb: '',
      _isImage: false, _isText: true, _imageNote: '',
      _viewKind: 'code', _rawUrl: '', _note: '', blocks: [],
      _pdfKey: '', _pdfPage: 0, _pdfPages: 0, _pdfImage: '',
      _pdfTextSvg: '', _pdfScale: 1.5,
      _pdfOutline: [], _pdfMeta: {},
      _pdfSearch: '', _pdfSearchHits: [], _pdfSearchIndex: 0,
      _pdfContinuous: false, _pdfContinuousPages: [],
      _pdfContinuousLoading: false,
      _slides: [],
      _docxExtras: '',
      _xlsxKey: '', _xlsxSheets: [], _activeSheet: 0, _xlsxFormula: null,
      data_url: '', mime: '',
    };
    this.diffText = '';
    this.diffDisplay = '';
    this.diffPath = '';
    this.diffTruncated = false;
    this.diffExpanded = false;
    this.diffRowsData = [];
    this.splitDiffRowsData = [];
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
    for (const tab of this.editorTabs) {
      if (tab.kind !== 'file') continue;
      this.releasePdfView(tab.fileView);
      const xlsxKey = tab.fileView && tab.fileView._xlsxKey;
      if (xlsxKey) XLSX_BOOKS.delete(xlsxKey);
    }
    this.editorTabs = [];
    this.activeEditorId = '';
    this.mainTab = 'chat';
    this.setMainTabFlags('chat');
    this.fileView = {
      path: '', size: 0, _text: '', _html: '',
      _lang: '', _size: '', _lineNumbers: '', _crumb: '',
      _isImage: false, _isText: true, _imageNote: '',
      _viewKind: 'code', _rawUrl: '', _note: '', blocks: [],
      _pdfKey: '', _pdfPage: 0, _pdfPages: 0, _pdfImage: '',
      _pdfTextSvg: '', _pdfScale: 1.5,
      _pdfOutline: [], _pdfMeta: {},
      _pdfSearch: '', _pdfSearchHits: [], _pdfSearchIndex: 0,
      _pdfContinuous: false, _pdfContinuousPages: [],
      _pdfContinuousLoading: false,
      _slides: [],
      _docxExtras: '',
      _xlsxKey: '', _xlsxSheets: [], _activeSheet: 0, _xlsxFormula: null,
      data_url: '', mime: '',
    };
    this.diffText = '';
    this.diffDisplay = '';
    this.diffPath = '';
    this.diffTruncated = false;
    this.diffExpanded = false;
    this.diffRowsData = [];
    this.splitDiffRowsData = [];
    this.fileLineInput = '';
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
      this.reportError(`读取文件树失败：${err}`, 'tree');
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
    if (!this._treeGitLoaded) void this.loadGitStatus();
  },

  async loadGitStatus() {
    if (!this.threadId) {
      this.treeGit = {};
      this.treeGitState = '';
      this._treeGitLoaded = true;
      return;
    }
    const threadId = this.threadId;
    try {
      const params = new URLSearchParams({
        thread_id: threadId,
        diff: 'false',
      });
      const response = await authFetch(`/api/git?${params.toString()}`);
      const data = await readJson(response, '读取 Git 状态失败');
      if (this.threadId !== threadId) return;
      this.treeGit = data && data.is_repo ? buildGitStatusMap(data.changes) : {};
      this.treeGitState = data && data.is_repo
        ? ''
        : (data && data.error) || '不是 Git 仓库';
    } catch (err) {
      if (this.threadId !== threadId) return;
      this.treeGit = {};
      this.treeGitState = String(err);
    } finally {
      if (this.threadId === threadId) this._treeGitLoaded = true;
    }
  },

  markTreeGitDirty(path) {
    const normalized = String(path || '').replace(/\\/g, '/');
    if (!normalized) return;
    this.treeGit = { ...this.treeGit, [normalized]: 'M' };
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
    await this.loadGitStatus();
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
        const indent = 8 + depth * 14;
        rows.push({
          ...entry,
          ...gitTreeFields(this.treeGit, entry.path, entry.is_dir),
          ...fileIcon(entry.name, entry.is_dir, expanded),
          _guides: treeGuides(depth),
          _depth: depth,
          _level: depth + 1,
          _expanded: expanded,
          _expandAttr: entry.is_dir ? (expanded ? 'true' : 'false') : '',
          _tabindex: entry.path === this.treeActivePath ? 0 : -1,
          _kind: entry.is_dir ? (loading ? '…' : (expanded ? '▾' : '▸')) : '·',
          _style: `padding-left:${indent}px`,
        });
        if (entry.is_dir && expanded) walk(entry.path, depth + 1);
      }
    };
    walk('', 0);
    if (rows.length && !rows.some((row) => row._tabindex === 0)) {
      rows[0]._tabindex = 0;
    }
    return rows;
  },

  filteredTreeRows() {
    if (!String(this.treeQuery || '').trim()) return this.treeVisibleRows();
    const all: any[] = [];
    const walk = (path: string) => {
      for (const entry of this.treeChildren[path] || []) {
        all.push({
          ...entry,
          ...gitTreeFields(this.treeGit, entry.path, entry.is_dir),
          ...fileIcon(entry.name, entry.is_dir, Boolean(this.treeExpanded[entry.path])),
          _guides: [],
          _depth: 0,
          _level: 1,
          _expanded: Boolean(this.treeExpanded[entry.path]),
          _expandAttr: entry.is_dir
            ? (this.treeExpanded[entry.path] ? 'true' : 'false')
            : '',
          _tabindex: entry.path === this.treeActivePath ? 0 : -1,
          _kind: entry.is_dir ? '目录' : '·',
          _style: 'padding-left:8px',
        });
        if (entry.is_dir) walk(entry.path);
      }
    };
    walk('');
    const rows = all.filter((entry) => (
      includesQuery(`${entry.name} ${entry.path}`, this.treeQuery)
    ));
    if (rows.length && !rows.some((row) => row._tabindex === 0)) {
      rows[0]._tabindex = 0;
    }
    return rows;
  },

  treeRootLoading() {
    return Boolean(this.treeLoadingMap['']);
  },

  canOpenFileInVscode() {
    const root = String(this.workspaceInput || this.workspace || '').trim();
    return Boolean(
      this.desktopAvailable
      && this.fileView.path
      && (isWslPath(root) || isWindowsPath(root))
    );
  },

  canRevealInExplorer() {
    const root = String(this.workspaceInput || this.workspace || '').trim();
    return Boolean(
      this.desktopAvailable && this.fileView.path && isWindowsPath(root)
    );
  },

  async openFileInVscode() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge) {
      this.toastMessage(this.t('main.browserNoEditor'));
      return;
    }
    const root = String(this.workspaceInput || this.workspace || '').replace(/\/+$/, '');
    if (!this.fileView.path || (!isWslPath(root) && !isWindowsPath(root))) {
      this.toastMessage(this.t('main.editorPathUnsupported'));
      return;
    }
    try {
      const result = isWslPath(root)
        ? await bridge.open_in_vscode(`${root}/${this.fileView.path}`)
        : await bridge.open_host_in_vscode(root, this.fileView.path);
      this.toastMessage(
        result.ok
          ? this.t('main.openedInEditor')
          : this.t('main.editorOpenFailed', { error: result.error }),
      );
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async revealFileInExplorer() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge) {
      this.toastMessage(this.t('main.browserNoEditor'));
      return;
    }
    const root = String(this.workspaceInput || this.workspace || '').trim();
    if (!isWindowsPath(root) || !this.fileView.path) {
      this.toastMessage(this.t('main.revealUnsupported'));
      return;
    }
    try {
      const result = await bridge.reveal_in_explorer(root, this.fileView.path);
      this.toastMessage(
        result.ok
          ? this.t('main.revealedInExplorer')
          : this.t('main.editorOpenFailed', { error: result.error }),
      );
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async openTreeEntry(entry) {
    if (entry.is_dir) await this.toggleTreeDir(entry);
    else await this.openFile(entry.path);
  },

  onTreeFocus(event) {
    const path = (event.currentTarget && event.currentTarget.dataset.path) || '';
    if (path && path !== this.treeActivePath) this.treeActivePath = path;
  },

  focusTreeRow(path) {
    if (!path) return;
    this.treeActivePath = path;
    this.$nextTick(() => {
      const rows = Array.from(document.querySelectorAll('.tree-row'));
      const target = rows.find(
        (row) => row.getAttribute('data-path') === path,
      ) as HTMLElement | undefined;
      if (target && typeof target.focus === 'function') target.focus();
    });
  },

  async onTreeKeydown(event) {
    const source = event.target as HTMLElement;
    const path = (source && source.dataset.path) || '';
    if (!path) return;
    const rows = this.filteredTreeRows();
    const index = rows.findIndex((row) => row.path === path);
    if (index < 0) return;
    const row = rows[index];
    const key = event.key;
    if (
      key === 'ArrowDown'
      || key === 'ArrowUp'
      || key === 'Home'
      || key === 'End'
    ) {
      event.preventDefault();
      let next = index;
      if (key === 'ArrowDown') next = Math.min(index + 1, rows.length - 1);
      if (key === 'ArrowUp') next = Math.max(index - 1, 0);
      if (key === 'Home') next = 0;
      if (key === 'End') next = rows.length - 1;
      this.focusTreeRow(rows[next].path);
      return;
    }
    if (key === 'ArrowRight') {
      event.preventDefault();
      if (row.is_dir && !this.treeExpanded[row.path]) {
        await this.toggleTreeDir(row);
      }
      return;
    }
    if (key === 'ArrowLeft') {
      event.preventDefault();
      if (row.is_dir && this.treeExpanded[row.path]) {
        await this.toggleTreeDir(row);
        return;
      }
      const parent = String(row.path).includes('/')
        ? String(row.path).split('/').slice(0, -1).join('/')
        : '';
      if (parent) this.focusTreeRow(parent);
      else this.focusTreeRow(rows[0].path);
      return;
    }
    if (key === 'Enter' || key === ' ') {
      event.preventDefault();
      await this.openTreeEntry(row);
    }
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
    this.fileLoading = true;
    try {
      const params = new URLSearchParams({
        thread_id: this.threadId,
        path,
      });
      const response = await authFetch(`/api/file?${params.toString()}`);
      const data = await readJson(response, '读取文件失败');
      const fileView = await this.buildFileView(path, data);
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
          _isFile: true,
          _isDiff: false,
          _active: false,
          _cls: '',
        }];
      }
      this.activateEditorTab(id);
      this.$nextTick(() => this.scrollFileToLine(line));
    } catch (err) {
      this.reportError(`读取文件失败：${err}`, 'file', path);
    } finally {
      this.fileLoading = false;
    }
  },

  viewerKind(path, data) {
    return detectViewerKind(path, data);
  },

  async buildFileView(path, data) {
    return buildViewerFileView(this, path, data);
  },

  async fetchRawBlob(path) {
    const params = new URLSearchParams({
      thread_id: this.threadId,
      path,
    });
    const response = await authFetch(`/api/file/raw?${params.toString()}`);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }
    return response.blob();
  },

  async fetchRawUrl(path) {
    return URL.createObjectURL(await this.fetchRawBlob(path));
  },

  async fetchRawBuffer(path) {
    return (await this.fetchRawBlob(path)).arrayBuffer();
  },

  async renderPdfPageData(pdfDoc, page, scale, query = '') {
    const pdfPage = await pdfDoc.getPage(page);
    const viewport = pdfPage.getViewport({ scale });
    const canvas = document.createElement('canvas');
    canvas.width = Math.floor(viewport.width);
    canvas.height = Math.floor(viewport.height);
    await pdfPage.render({
      canvasContext: canvas.getContext('2d'),
      viewport,
    }).promise;
    const content = await pdfPage.getTextContent();
    const styles = content.styles || {};
    const texts: string[] = [
      `<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" `
      + `viewBox="0 0 ${canvas.width} ${canvas.height}" `
      + 'preserveAspectRatio="xMidYMid meet" xml:space="preserve">'
      + '<g fill="transparent">',
    ];
    for (const item of content.items || []) {
      if (!item || typeof item.str !== 'string' || !item.str) continue;
      const tx = window.pdfjsLib.Util.transform(
        viewport.transform,
        item.transform,
      );
      const height = Math.hypot(tx[2], tx[3]);
      const width = Math.max(1, Number(item.width || 0) * scale);
      const matrix = (
        `matrix(${tx[0].toFixed(4)},${tx[1].toFixed(4)},`
        + `${tx[2].toFixed(4)},${tx[3].toFixed(4)},`
        + `${tx[4].toFixed(2)},${tx[5].toFixed(2)})`
      );
      const fontFamily = String(
        (styles[item.fontName] && styles[item.fontName].fontFamily) || '',
      ).trim();
      const segments = pdfTextSegments(item.str, query);
      const body = segments.length === 1 && !segments[0].hit
        ? escapeHtml(item.str)
        : segments.map((segment) => (
          segment.hit
            ? `<tspan class="pdf-hit">${escapeHtml(segment.text)}</tspan>`
            : `<tspan>${escapeHtml(segment.text)}</tspan>`
        )).join('');
      texts.push(
        `<text transform="${matrix}" x="0" y="0" `
        + `font-size="${height.toFixed(2)}" `
        + (fontFamily ? `font-family="${escapeXmlAttr(fontFamily)}" ` : '')
        + `textLength="${width.toFixed(2)}" `
        + `lengthAdjust="spacing">${body}</text>`,
      );
    }
    texts.push('</g></svg>');
    return {
      image: canvas.toDataURL('image/png'),
      svg: texts.join(''),
      width: canvas.width,
      height: canvas.height,
    };
  },

  async renderPdfPage(view, page) {
    const pdfDoc = view && PDF_DOCS.get(view._pdfKey);
    if (!pdfDoc) return;
    try {
      const target = Math.max(1, Math.min(view._pdfPages, Number(page) || 1));
      const scale = Number(view._pdfScale) || 1.5;
      const data = await this.renderPdfPageData(
        pdfDoc, target, scale, String(view._pdfSearch || ''),
      );
      view._pdfImage = data.image;
      view._pdfTextSvg = data.svg;
      view._pdfPage = target;
    } catch (err) {
      view._pdfImage = '';
      view._pdfTextSvg = '';
    }
  },

  async pdfZoom(delta) {
    const view = this.fileView;
    if (!view || !PDF_DOCS.has(view._pdfKey)) return;
    const next = Math.max(0.75, Math.min(3, Number(view._pdfScale || 1.5) + delta));
    view._pdfScale = next;
    await this.renderPdfPage(view, Number(view._pdfPage || 1));
    this.fileView = { ...view };
  },

  async pdfZoomIn() {
    await this.pdfZoom(0.25);
  },

  async pdfZoomOut() {
    await this.pdfZoom(-0.25);
  },

  async pdfGo(delta) {
    const view = this.fileView;
    if (!view || !PDF_DOCS.has(view._pdfKey)) return;
    await this.renderPdfPage(view, Number(view._pdfPage || 1) + delta);
    this.fileView = { ...view };
  },

  async pdfPrev() {
    await this.pdfGo(-1);
  },

  async pdfNext() {
    await this.pdfGo(1);
  },

  async goToPdfPage(page) {
    const view = this.fileView;
    if (!view || !PDF_DOCS.has(view._pdfKey)) return;
    const target = Number(page) || 1;
    if (view._pdfContinuous) {
      while ((view._pdfContinuousPages || []).length < target) {
        await this.pdfLoadMoreContinuous();
      }
      view._pdfPage = target;
      this.fileView = { ...view };
      this.$nextTick(() => {
        const element = document.querySelector(
          `.pdf-continuous .pdf-page[data-page="${target}"]`,
        ) as HTMLElement | null;
        element?.scrollIntoView({ block: 'start' });
      });
      return;
    }
    await this.renderPdfPage(view, target);
    this.fileView = { ...view };
  },

  onPdfSearchInput() {
    const input = document.querySelector(
      'input[data-role="pdf-search"]',
    ) as HTMLInputElement | null;
    this.pdfSearchQuery = input ? input.value : '';
  },

  async pdfSearch() {
    const input = document.querySelector(
      'input[data-role="pdf-search"]',
    ) as HTMLInputElement | null;
    if (input) this.pdfSearchQuery = input.value;
    const view = this.fileView;
    const query = String(this.pdfSearchQuery || '').trim().toLowerCase();
    const pdf = view && PDF_DOCS.get(view._pdfKey);
    if (!pdf || !query) return;
    const hits: number[] = [];
    const limit = Number(view._pdfPages || 0);
    for (let pageNumber = 1; pageNumber <= limit; pageNumber += 1) {
      try {
        const page = await pdf.getPage(pageNumber);
        const content = await page.getTextContent();
        const text = (content.items || [])
          .map((item) => String(item.str || ''))
          .join(' ')
          .toLowerCase();
        if (text.includes(query)) hits.push(pageNumber);
      } catch (err) {
        // 单页搜索失败跳过。
      }
    }
    view._pdfSearch = query;
    view._pdfSearchHits = hits;
    view._pdfSearchIndex = 0;
    if (view._pdfContinuous && (view._pdfContinuousPages || []).length) {
      const scale = Number(view._pdfScale) || 1.5;
      for (const page of view._pdfContinuousPages) {
        try {
          const data = await this.renderPdfPageData(
            pdf, page.page, scale, query,
          );
          page.image = data.image;
          page.svg = data.svg;
        } catch (err) {
          // 单页高亮失败保留原文本层。
        }
      }
    }
    if (hits.length) {
      await this.goToPdfPage(hits[0]);
    } else {
      this.fileView = { ...view };
    }
  },

  async pdfSearchNext() {
    const view = this.fileView;
    const hits = (view && view._pdfSearchHits) || [];
    if (!hits.length) return;
    const next = ((Number(view._pdfSearchIndex || 0) + 1) % hits.length);
    view._pdfSearchIndex = next;
    await this.goToPdfPage(hits[next]);
  },

  async pdfToggleContinuous() {
    const view = this.fileView;
    const pdf = view && PDF_DOCS.get(view._pdfKey);
    if (!pdf) return;
    if (view._pdfContinuous) {
      view._pdfContinuous = false;
      view._pdfContinuousPages = [];
      view._pdfContinuousLoading = false;
      this.fileView = { ...view };
      return;
    }
    view._pdfContinuous = true;
    view._pdfContinuousPages = [];
    view._pdfContinuousLoading = false;
    this.fileView = { ...view };
    await this.pdfLoadMoreContinuous();
  },

  async pdfLoadMoreContinuous() {
    const view = this.fileView;
    if (!view || !view._pdfContinuous || view._pdfContinuousLoading) return;
    const pdf = PDF_DOCS.get(view._pdfKey);
    if (!pdf) return;
    const pages = view._pdfContinuousPages || [];
    const total = Number(view._pdfPages || 0);
    if (pages.length >= total) return;
    const start = pages.length + 1;
    const end = Math.min(total, start + 4);
    const scale = Number(view._pdfScale) || 1.5;
    const query = String(view._pdfSearch || '');
    view._pdfContinuousLoading = true;
    this.fileView = { ...view };
    for (let pageNumber = start; pageNumber <= end; pageNumber += 1) {
      try {
        const data = await this.renderPdfPageData(
          pdf, pageNumber, scale, query,
        );
        view._pdfContinuousPages.push({
          page: pageNumber,
          image: data.image,
          svg: data.svg,
          _key: pageNumber,
        });
        this.fileView = { ...view };
      } catch (err) {
        // 单页失败时继续下一批。
      }
    }
    view._pdfContinuousLoading = false;
    this.fileView = { ...view };
    this.$nextTick(() => this.pdfContinuousMaybeFill());
  },

  pdfContinuousMaybeFill() {
    const view = this.fileView;
    if (!view || !view._pdfContinuous) return;
    const container = document.querySelector(
      '.pdf-continuous',
    ) as HTMLElement | null;
    if (
      container
      && container.scrollHeight <= container.clientHeight + 20
      && (view._pdfContinuousPages || []).length < Number(view._pdfPages || 0)
    ) {
      void this.pdfLoadMoreContinuous();
    }
  },

  pdfContinuousScroll(event) {
    const element = event.target as HTMLElement | null;
    if (!element) return;
    if (
      element.scrollTop + element.clientHeight
      >= element.scrollHeight - 400
    ) {
      void this.pdfLoadMoreContinuous();
    }
  },

  async fetchDocument(path, includeImages = false, includeLayout = false) {
    const params = new URLSearchParams({
      thread_id: this.threadId,
      path,
    });
    if (includeImages) params.set('include_images', 'true');
    if (includeLayout) params.set('include_layout', 'true');
    const response = await authFetch(
      `/api/file/document?${params.toString()}`,
    );
    return readJson(response, '读取文档失败');
  },

  get fileViewIsMarkdown() {
    return Boolean(this.fileView && this.fileView._viewKind === 'markdown');
  },

  get fileViewIsPdf() {
    return Boolean(this.fileView && this.fileView._viewKind === 'pdf');
  },

  get fileViewIsDocument() {
    return Boolean(this.fileView && this.fileView._viewKind === 'document');
  },

  get fileViewIsHtml() {
    return Boolean(this.fileView && this.fileView._viewKind === 'html');
  },

  get fileViewIsSlides() {
    return Boolean(this.fileView && this.fileView._viewKind === 'slides');
  },

  get pdfHasImage() {
    return Boolean(this.fileView && this.fileView._pdfImage);
  },

  get pdfHasFallback() {
    return Boolean(
      this.fileView
      && this.fileView._viewKind === 'pdf'
      && !this.fileView._pdfImage,
    );
  },

  get pdfSearchLabel() {
    if (!this.fileView || !String(this.pdfSearchQuery || '').trim()) {
      return '';
    }
    const hits = this.fileView._pdfSearchHits || [];
    if (!hits.length) return '未找到';
    return `${Number(this.fileView._pdfSearchIndex || 0) + 1}/${hits.length} 个命中`;
  },

  get pdfSearchHasHits() {
    return Boolean(
      this.fileView && this.fileView._pdfSearchHits
      && this.fileView._pdfSearchHits.length,
    );
  },

  get pdfContinuousLabel() {
    return this.fileView && this.fileView._pdfContinuous ? '单页' : '连续';
  },

  get pdfContinuousProgress() {
    if (!this.fileView || !this.fileView._pdfContinuous) return '';
    const loaded = (this.fileView._pdfContinuousPages || []).length;
    const total = Number(this.fileView._pdfPages || 0);
    return (
      `已加载 ${loaded}/${total} 页`
      + (this.fileView._pdfContinuousLoading ? ' · 加载中…' : '')
    );
  },

  get pdfSingleVisible() {
    return Boolean(this.fileView && this.fileView._pdfImage
      && !this.fileView._pdfContinuous);
  },

  get pdfHasOutline() {
    return Boolean(
      this.fileView && this.fileView._pdfOutline
      && this.fileView._pdfOutline.length,
    );
  },

  get pdfOutlineItems() {
    const items = (this.fileView && this.fileView._pdfOutline) || [];
    const current = (this.fileView && this.fileView._pdfPage) || 0;
    return items.map((item, index) => ({
      ...item,
      _key: `${index}:${item.title}`,
      _cls: item.page === current ? 'active' : '',
    }));
  },

  get pdfMetaText() {
    const meta = (this.fileView && this.fileView._pdfMeta) || {};
    return [
      meta.title,
      meta.author ? `作者：${meta.author}` : '',
      meta.subject ? `主题：${meta.subject}` : '',
      meta.keywords ? `关键词：${meta.keywords}` : '',
    ].filter(Boolean).join(' · ');
  },

  get pdfPageLabel() {
    if (!this.fileView || !this.fileView._pdfPages) return '';
    return `${this.fileView._pdfPage || 1} / ${this.fileView._pdfPages}`;
  },


  get fileViewIsUnsupported() {
    return Boolean(this.fileView && this.fileView._viewKind === 'unsupported');
  },

  get documentBlocks() {
    if (!this.fileView) return [];
    const sheets = this.fileView._xlsxSheets || [];
    if (sheets.length) {
      const sheet = sheets[this.fileView._activeSheet || 0] || sheets[0];
      return sheet.blocks || [];
    }
    return this.fileView.blocks || [];
  },

  get fileViewIsXlsx() {
    return Boolean(
      this.fileView
      && this.fileView._xlsxSheets
      && this.fileView._xlsxSheets.length > 1,
    );
  },

  get xlsxSheetTabs() {
    const sheets = (this.fileView && this.fileView._xlsxSheets) || [];
    const active = (this.fileView && this.fileView._activeSheet) || 0;
    return sheets.map((sheet, index) => ({
      ...sheet,
      _cls: index === active ? 'active' : '',
    }));
  },

  selectSheet(index) {
    const view = this.fileView;
    if (!view || !view._xlsxSheets || !view._xlsxSheets[index]) return;
    view._activeSheet = index;
    view._xlsxFormula = null;
    const sheet = view._xlsxSheets[index];
    const block = sheet && sheet.blocks && sheet.blocks[0];
    if (block && block._xlsxWindow) {
      applySheetWindow(block, 0, XLSX_DEFAULT_VIEWPORT);
    }
    this.fileView = { ...view };
    this.$nextTick(() => this.xlsxSyncSpacers());
  },

  selectXlsxCell(row, col) {
    const view = this.fileView;
    const info = selectXlsxCellData(view, row, col);
    if (!info) return;
    view._xlsxFormula = info;
    this.fileView = { ...view };
  },

  xlsxScroll(event) {
    const view = this.fileView;
    if (!view || !view._xlsxSheets) return;
    const sheet = view._xlsxSheets[view._activeSheet || 0];
    const block = sheet && sheet.blocks && sheet.blocks[0];
    if (!block || !block._xlsxWindow) return;
    const target = event && event.currentTarget;
    const scrollTop = target ? Number(target.scrollTop) || 0 : 0;
    const height = target && target.clientHeight
      ? Number(target.clientHeight)
      : XLSX_DEFAULT_VIEWPORT;
    const changed = applySheetWindow(block, scrollTop, height);
    syncSpacerHeights(block, target);
    if (changed) {
      this.fileView = { ...view };
    }
  },

  xlsxSyncSpacers() {
    const view = this.fileView;
    if (!view || !view._xlsxSheets) return;
    const sheet = view._xlsxSheets[view._activeSheet || 0];
    const block = sheet && sheet.blocks && sheet.blocks[0];
    const wrap = document.querySelector('.document-table-wrap');
    if (!block || !block._xlsxWindow || !wrap) return;
    applySheetWindow(
      block,
      wrap.scrollTop,
      wrap.clientHeight || XLSX_DEFAULT_VIEWPORT,
    );
    syncSpacerHeights(block, wrap);
  },

  get xlsxFormulaText() {
    const info = this.fileView && this.fileView._xlsxFormula;
    if (!info) return '';
    return `${info.ref}  ${info.formula || info.value}`;
  },

  get fileViewNote() {
    return (this.fileView && this.fileView._note) || '';
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
    this.setMainTabFlags('chat');
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
