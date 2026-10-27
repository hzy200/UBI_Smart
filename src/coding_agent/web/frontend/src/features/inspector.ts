/* Alpine mixin: inspector。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
  readJson,
} from '../api/http';

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
    const params = new URLSearchParams({ thread_id: this.threadId });
    if (snapshotId) params.set('snapshot_id', snapshotId);
    if (path) params.set('path', path);
    try {
      const response = await authFetch(`/api/diff?${params.toString()}`);
      const data = await readJson(response, '读取 diff 失败');
      this.diffText = data.diff || '（没有差异）';
      const lines = this.diffText.split('\n');
      this.diffTruncated = lines.length > 200;
      this.diffExpanded = false;
      this.diffDisplay = this.diffTruncated
        ? `${lines.slice(0, 200).join('\n')}\n…（共 ${lines.length} 行，已折叠）`
        : this.diffText;
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  expandDiff() {
    this.diffExpanded = true;
    this.diffDisplay = this.diffText;
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

  async loadTree(path = '') {
    if (!this.threadId) {
      this.toastMessage('请先创建会话');
      return;
    }
    this.treeLoading = true;
    try {
      const params = new URLSearchParams({
        thread_id: this.threadId,
        path,
      });
      const response = await authFetch(`/api/files?${params.toString()}`);
      const data = await readJson(response, '读取文件树失败');
      this.treePath = data.path || '';
      this.treeTruncated = Boolean(data.truncated);
      this.treeRows = (data.entries || []).map((entry) => ({
        ...entry,
        _key: entry.path,
        _kind: entry.is_dir ? '目录' : '文件',
        _meta: entry.is_dir ? '' : this.formatBytes(entry.size),
        _cls: entry.is_dir ? 'tree-dir' : 'tree-file',
      }));
      this.fileView = { path: '', size: 0, _text: '' };
    } catch (err) {
      this.toastMessage(String(err));
    } finally {
      this.treeLoading = false;
    }
  },

  async openTreeEntry(entry) {
    if (entry.is_dir) {
      await this.loadTree(entry.path);
    } else {
      await this.openFile(entry.path);
    }
  },

  async treeUp() {
    if (!this.treePath) return;
    const parts = this.treePath.split('/');
    parts.pop();
    await this.loadTree(parts.join('/'));
  },

  async openFile(path) {
    if (!this.threadId) return;
    try {
      const params = new URLSearchParams({
        thread_id: this.threadId,
        path,
      });
      const response = await authFetch(`/api/file?${params.toString()}`);
      const data = await readJson(response, '读取文件失败');
      this.fileView = {
        ...data,
        _size: this.formatBytes(data.size),
        _text: data.binary
          ? '（二进制文件，不展示内容）'
          : data.content + (data.truncated ? '\n…（已截断）' : ''),
      };
    } catch (err) {
      this.toastMessage(String(err));
    }
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
    } catch (err) {
      this.toastMessage(String(err));
    } finally {
      this.gitLoading = false;
    }
  },

};
