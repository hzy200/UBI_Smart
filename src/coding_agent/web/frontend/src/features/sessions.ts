/* Alpine mixin: sessions。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
  readJson,
} from '../api/http';
import {
  renderMarkdown,
} from '../ui/format';
import {
  renderTimers,
  tokenBuffers,
  toolMap,
} from '../state/runtime';

export const sessionsMixin = {
  async saveTranscript() {
    if (!this.threadId || this.messages.length === 0) return;
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
    try {
      const response = await authFetch(
        `/api/thread/${encodeURIComponent(this.threadId)}/transcript`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ messages }),
        },
      );
      await readJson(response, '保存对话轨迹失败');
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
        this.messages.push({
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
        });
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
      this.messages.push({
        id: ++this._msgSeq,
        role,
        phase,
        text: item.text || '',
        html: renderMarkdown(item.text || ''),
        done: true,
        _expanded: false,
        _cls: cls,
      });
    }
    // 兼容旧轨迹：即使保存时尾部有过程提示，也把最终回答放回最后。
    this.moveFinalAnswerLast();
    this.$nextTick(() => this.decorateTranscript());
  },

  async loadSessions() {
    try {
      const response = await authFetch('/api/sessions?limit=50');
      const data = await readJson(response, '无法读取会话');
      this.sessions = (data.sessions || []).map((item) => ({
        ...item,
        _title: item.title || item.thread_id,
        _meta: `${item.thread_id} · ${item.last_active || ''}` +
          (item.interrupted ? ' · 异常中断' : ''),
        _cls: item.thread_id === this.threadId ? 'active' : '',
      }));
      this.filterSessions();
    } catch (e) { /* ignore */ }
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
    const filtered = q
      ? this.sessions.filter((row) =>
          row._title.toLowerCase().includes(q) ||
          row.thread_id.toLowerCase().includes(q) ||
          String(row.workspace || '').toLowerCase().includes(q) ||
          this.sessionWorkspaceLabel(row).toLowerCase().includes(q))
      : this.sessions.slice();
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
        .sort((a, b) =>
          String(b.last_active || '').localeCompare(
            String(a.last_active || ''),
          ));
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
    this.threadId = data.thread_id;
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
    this.toastMessage(`会话已创建：${data.thread_id}`);
    await this.loadSessions();
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
      await this.loadSessions();
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
      if (this.threadId === id) {
        this.threadId = '';
        this._boundProjectId = '';
        this.resetConversation();
      }
      await this.loadSessions();
      this.toastMessage('会话已删除');
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async exportSession(id) {
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(id)}/export?format=markdown`,
      );
      const data = await readJson(response, '导出失败');
      const blob = new Blob([data.content || ''], { type: 'text/markdown' });
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `${(data.title || id).replace(/[\\/:*?"<>|]/g, '_')}.md`;
      link.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async ensureSession() {
    if (this.threadId) return this.threadId;
    return this.createSession();
  },

  async newSession() {
    if (this.busy || this.approvals.length || this.paused) {
      this.toastMessage('当前任务运行中或挂起中，先处理审批/暂停');
      return;
    }
    this.threadId = '';
    this._boundProjectId = '';
    this.resetConversation();
    await this.loadSessions();
    this.toastMessage('已切换到新会话');
  },

  resetConversation() {
    this.messages = [];
    this.reasoningBlocks = [];
    this.lastSeq = 0;
    this.canReconnect = false;
    this.connectionState = 'idle';
    this._terminal = false;
    this._suspended = false;
    this._retryCount = 0;
    this.plan = [];
    this.kinds = [];
    this.planRows = [];
    this.stepIdx = -1;
    this.planDegraded = false;
    this.tools = [];
    toolMap.clear();
    renderTimers.forEach((timer) => clearTimeout(timer));
    renderTimers.clear();
    tokenBuffers.clear();
    this.files = [];
    this.checkpoints = [];
    this.diffText = '';
    this.diffDisplay = '';
    this.diffTruncated = false;
    this.diffExpanded = false;
    this.tasks = [];
    this.audit = [];
    this.uploads = [];
    this.approvals = [];
    this.approval = null;
    this.decisions = {};
    this.costText = '';
    this.paused = false;
    this.stateText = '就绪';
  },

  async openSession(id) {
    if (this.busy || this.approvals.length || this.paused) {
      this.toastMessage('当前任务运行中或挂起中，先处理审批/暂停');
      return;
    }
    this.threadId = id;
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
    this.resetConversation();
    await this.loadCapabilities();
    try {
      const response = await authFetch(`/api/thread/${encodeURIComponent(id)}`);
      const data = await readJson(response, '读取历史失败');
      if (Array.isArray(data.transcript) && data.transcript.length) {
        // 桌面 UI 保存过完整轨迹：包含工具卡片、步骤和最终回答。
        this.restoreTranscript(data.transcript);
      } else {
        for (const item of data.messages || []) {
          this.pushMessage(item.role, item.text, true);
        }
      }
    } catch (e) {
      this.toastMessage(String(e));
    }
    await this.loadUploads();
    await this.loadApprovals();
    this.treePath = '';
    this.treeRows = [];
    this.fileView = { path: '', size: 0, _text: '' };
    this.gitInfo = {
      is_repo: false, _state: '', _branch: '',
      _changes: [], _commits: [],
    };
    this.gitDiffText = '';
    await this.loadSessions();
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
        this.stateText = '等待审批';
      }
    } catch (e) {
      this.approvals = [];
    }
  },

};
