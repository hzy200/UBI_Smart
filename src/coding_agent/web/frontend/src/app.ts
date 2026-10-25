/* CodingAgent GUI entry.
 *
 * 只消费后端领域事件，不接触 LangGraph / 工具 / 沙箱。
 * Alpine 使用 CSP 构建：模板里尽量只调用组件方法，避免 eval。
 */

import { consumeSse } from './sse';

const TOKEN_KEY = 'codingagent.token';
const SETTINGS_KEY = 'codingagent.settings';
const SHORTCUTS = [
  { keys: 'Ctrl / Cmd + K', action: '聚焦会话搜索' },
  { keys: 'Ctrl / Cmd + Enter', action: '发送任务' },
  { keys: 'Shift + Enter', action: '输入框内换行' },
  { keys: 'Esc', action: '关闭设置 / 恢复中心 / 快捷键' },
  { keys: '?', action: '打开快捷键帮助' },
];

function getToken() {
  return sessionStorage.getItem(TOKEN_KEY) || '';
}

function setToken(value) {
  if (value) sessionStorage.setItem(TOKEN_KEY, value);
  else sessionStorage.removeItem(TOKEN_KEY);
}

function parseHashToken() {
  const match = location.hash.match(/token=([^&]+)/);
  if (!match) return '';
  const token = decodeURIComponent(match[1]);
  history.replaceState(null, '', location.pathname + location.search);
  return token;
}

function escapeHtml(text) {
  return String(text || '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function formatDuration(ms) {
  const value = Number(ms || 0);
  if (!value) return '';
  if (value < 1000) return `${value}ms`;
  return `${(value / 1000).toFixed(1)}s`;
}

function formatTokens(input, output) {
  const inTokens = Number(input || 0);
  const outTokens = Number(output || 0);
  if (!inTokens && !outTokens) return '';
  return `${inTokens} in / ${outTokens} out tokens`;
}

function renderMarkdown(text) {
  const source = String(text || '');
  if (!source) return '';
  try {
    if (window.marked && window.DOMPurify) {
      return window.DOMPurify.sanitize(window.marked.parse(source), {
        USE_PROFILES: { html: true },
      });
    }
  } catch (e) { /* 降级为纯文本 */ }
  return escapeHtml(source);
}

class AuthError extends Error {}

function newIdempotencyKey() {
  const cryptoApi = window.crypto as Crypto & { randomUUID?: () => string };
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    return cryptoApi.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

async function authFetch(url: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers || {});
  const token = getToken();
  if (token) headers.set('Authorization', `Bearer ${token}`);
  const response = await fetch(url, { ...options, headers });
  if (response.status === 401) throw new AuthError('需要认证');
  return response;
}

async function readJson(response, fallbackMessage) {
  if (!response.ok) {
    let detail = '';
    try { detail = await response.text(); } catch (e) { /* ignore */ }
    throw new Error(detail || fallbackMessage || `HTTP ${response.status}`);
  }
  return response.json();
}

const agentApp = () => ({
  ready: false,
  loginError: '',
  tokenInput: '',
  healthText: '连接中…',
  workspace: '',
  workspaceInput: '',
  accessMode: 'read_only',
  model: '',
  provider: '',
  providers: [],
  capabilities: {
    read_only: true,
    server_write_enabled: false,
    multi_user: false,
    desktop_mode: false,
    workspace_registry: false,
    workspace_upload_max_bytes: 0,
  },
  project: null,
  projectReport: '正在探测工作区…',
  projectWarnings: '',
  projectHasWarnings: false,
  projectLoading: false,
  projects: [],
  projectId: '',
  projectFile: null,
  projectFileName: '',
  projectBusy: false,
  projectMessage: '',
  projectJobKind: '',
  projectJobId: '',
  projectJobWorkspaceId: '',
  projectDropActive: false,
  revisions: [],
  revisionBusy: false,
  desktopAvailable: false,
  canCopyWorkspace: false,
  copyBusy: false,
  copyMessage: '',
  threadId: '',
  stateText: '就绪',
  costText: '',
  busy: false,
  paused: false,
  reasoningBlocks: [],
  showReasoning: false,
  lastSeq: 0,
  canReconnect: false,
  connectionState: 'idle',
  shortcutsOpen: false,
  _terminal: false,
  _suspended: false,
  _retryCount: 0,
  _retryTimer: null,
  _lastPrompt: '',
  messages: [],
  plan: [],
  kinds: [],
  planRows: [],
  stepIdx: -1,
  planDegraded: false,
  tools: [],
  files: [],
  checkpoints: [],
  diffText: '',
  tasks: [],
  memory: [],
  memoryText: '',
  audit: [],
  uploads: [],
  sessions: [],
  sessionRows: [],
  sessionQuery: '',
  treePath: '',
  treeRows: [],
  treeLoading: false,
  fileView: { path: '', size: 0, _text: '' },
  gitInfo: {
    is_repo: false, _state: '', _branch: '',
    _changes: [], _commits: [],
  },
  gitDiffText: '',
  gitLoading: false,
  diffDisplay: '',
  diffTruncated: false,
  diffExpanded: false,
  updateText: '',
  updateInfo: null,
  diagnosticsPath: '',
  diagnosticsBusy: false,
  uploadQuota: '',
  showRecovery: false,
  recoveryItems: [],
  profiles: [],
  profilesEnabled: false,
  profileName: '',
  profileBaseUrl: '',
  profileModel: '',
  modelOptions: [],
  modelOptionsText: '',
  notifyEnabled: false,
  limitsText: '',
  approvals: [],
  approval: null,
  approvalPosition: '',
  decisions: {},
  tab: 'plan',
  showSettings: false,
  providerName: 'deepseek',
  providerKeyInput: '',
  theme: 'dark',
  density: 'normal',
  fontSize: 'normal',
  fontFamily: 'system',
  bodyClasses: 'theme-dark density-normal font-normal family-system',
  tabClasses: { plan: 'active', tools: '', files: '', tree: '', git: '', tasks: '', memory: '', audit: '' },
  badgeClasses: 'badge badge-read',
  toast: '',
  _toolMap: new Map(),
  _msgSeq: 0,
  _currentNode: '',
  _currentMessage: null,
  _toastTimer: null,
  _renderTimers: new Map(),

  async init() {
    const hashToken = parseHashToken();
    if (hashToken) setToken(hashToken);
    this.tokenInput = getToken();
    this.desktopAvailable = Boolean(window.pywebview && window.pywebview.api);
    // pywebview 可能在 Alpine 初始化之后才注入 API；以事件为准再更新一次。
    window.addEventListener('pywebviewready', () => {
      this.desktopAvailable = true;
      this.probeProject();
    });
    this.loadSettings();
    this.applyBodyClasses();
    this.refreshBadgeClass();
    window.addEventListener('keydown', (event) => this.onGlobalKeydown(event));
    try {
      await this.boot();
      await this.probeProject();
    } catch (err) {
      this.showLogin(err instanceof AuthError ? 'token 无效或已过期' : String(err));
    }
  },

  async boot() {
    await this.refreshHealth();
    await Promise.all([
      this.loadCapabilities(),
      this.loadProviders(),
      this.loadSessions(),
      this.loadWorkspace(),
      this.loadProfiles(),
    ]);
    this.ready = true;
  },

  showLogin(message) {
    this.ready = false;
    this.loginError = message || '';
  },

  async saveToken() {
    setToken(this.tokenInput.trim());
    try {
      await this.boot();
    } catch (err) {
      this.showLogin(err instanceof AuthError ? 'token 无效或已过期' : String(err));
    }
  },

  loadSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
      if (saved.theme) this.theme = saved.theme;
      if (saved.density) this.density = saved.density;
      if (saved.fontSize) this.fontSize = saved.fontSize;
      if (saved.fontFamily) this.fontFamily = saved.fontFamily;
      if (typeof saved.showReasoning === 'boolean') {
        this.showReasoning = saved.showReasoning;
      }
      if (saved.workspace) this.workspaceInput = saved.workspace;
      if (saved.notify) this.notifyEnabled = true;
    } catch (e) { /* ignore */ }
  },

  saveSettings() {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({
      theme: this.theme,
      density: this.density,
      fontSize: this.fontSize,
      fontFamily: this.fontFamily,
      showReasoning: this.showReasoning,
      workspace: this.workspaceInput,
      notify: this.notifyEnabled,
    }));
    this.applyBodyClasses();
  },

  rememberWorkspace() {
    try {
      const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
      saved.workspace = this.workspaceInput;
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(saved));
    } catch (e) { /* ignore */ }
  },

  async probeProject() {
    const workspace = (this.workspaceInput || this.workspace || '').trim();
    const query = workspace
      ? `?workspace=${encodeURIComponent(workspace)}`
      : '';
    try {
      const response = await authFetch(`/api/project${query}`);
      const data = await readJson(response, '无法读取项目环境');
      this.project = data;
      const info = data.project || {};
      const mode = data.mode === 'mnt'
        ? '/mnt（Windows 盘）'
        : data.mode === 'native' ? 'WSL 原生' : '未知';
      const source = info.source && info.source !== 'system' ? ` · ${info.source}` : '';
      this.projectReport =
        `工作区：${data.workspace || '（未选择）'}（${mode}）\n` +
        `项目 Python：${info.python || '未检测到'}${info.version ? ` · ${info.version}` : ''}${source}\n` +
        `Agent Python：${data.agent_python} · ${data.agent_version}\n` +
        `包管理器：${data.package_manager || '未检测到'}` +
        `${data.lockfile ? ` · ${data.lockfile}` : ''}\n` +
        `测试命令：${data.test_command || '未检测到 pytest'}`;
      this.projectWarnings = (data.warnings || []).join('\n');
      this.projectHasWarnings = Boolean(this.projectWarnings);
      this.canCopyWorkspace =
        Boolean(this.desktopAvailable) && data.mode === 'mnt';
      if (!this.workspace && data.workspace) this.workspace = data.workspace;
      if (!this.workspaceInput && data.workspace) {
        this.workspaceInput = data.workspace;
      }
    } catch (err) {
      this.projectReport = `项目环境探测失败：${err}`;
      this.projectWarnings = '';
      this.projectHasWarnings = false;
    }
  },

  async pickWorkspace() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge) {
      this.toastMessage('浏览器模式不支持原生文件夹选择');
      return;
    }
    try {
      const path = await bridge.pick_folder();
      if (!path) return;
      this.workspaceInput = path;
      this.rememberWorkspace();
      await this.probeProject();
      this.toastMessage(`工作区：${path}`);
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async openInVscode() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge) {
      this.toastMessage('浏览器模式不支持在 VS Code 打开');
      return;
    }
    const path = (this.workspaceInput || this.workspace || '').trim();
    if (!path) return;
    try {
      const result = await bridge.open_in_vscode(path);
      this.toastMessage(result.ok ? '已在 VS Code 打开' : result.error);
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async copyWorkspace() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge) return;
    const source = (this.workspaceInput || '').trim();
    if (!source) return;
    const name = source.replace(/\/+$/, '').split('/').pop();
    const target = `~/.coding-agent/workspaces/${name}`;
    if (!window.confirm(`复制工作区到 WSL 原生目录？\n源：${source}\n目标：${target}`)) {
      return;
    }
    try {
      const result = await bridge.copy_to_wsl(source, name);
      if (!result.ok) {
        this.toastMessage(result.error);
        return;
      }
      this.copyBusy = true;
      this.copyMessage = `复制中…目标：${result.target}`;
      await this.waitCopy(result.job_id);
    } catch (err) {
      this.copyBusy = false;
      this.toastMessage(String(err));
    }
  },

  async waitCopy(jobId) {
    const bridge = window.pywebview && window.pywebview.api;
    for (;;) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const status = await bridge.copy_status(jobId);
      if (status.state === 'done') {
        this.copyBusy = false;
        this.copyMessage = '已复制到 WSL 原生目录';
        if (status.target) this.workspaceInput = status.target;
        this.rememberWorkspace();
        await this.probeProject();
        return;
      }
      if (status.state === 'error' || status.state === 'unknown') {
        this.copyBusy = false;
        this.copyMessage = status.error || '复制失败';
        this.toastMessage(this.copyMessage);
        return;
      }
    }
  },

  projectLabel(project) {
    const name = project.display_name || project.workspace_id;
    const degraded = project.degraded_fs ? ' · 挂载' : '';
    return `${name} · ${project.status}${degraded}`;
  },

  revisionLabel(revision) {
    const when = revision.created_at || '';
    const commit = revision.git_commit
      ? ` · ${String(revision.git_commit).slice(0, 8)}`
      : '';
    const pin = revision.pinned ? ' · pinned' : '';
    return `${when}${commit} · ${revision.status}${pin} · ${revision.scan_status || ''}`;
  },

  pinLabel(revision) {
    return revision.pinned ? '取消 pin' : 'pin';
  },

  selectedProject() {
    return this.projects.find(
      (item) => item.workspace_id === this.projectId,
    ) || null;
  },

  sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  },

  async loadProjects() {
    if (!this.capabilities.workspace_registry) return;
    try {
      const response = await authFetch('/api/workspaces');
      const data = await readJson(response, '读取项目失败');
      this.projects = data.workspaces || [];
      if (
        this.projectId &&
        !this.projects.some((p) => p.workspace_id === this.projectId)
      ) {
        this.projectId = '';
      }
    } catch (err) {
      this.projectMessage = `项目列表失败：${err}`;
    }
  },

  async refreshProjects() {
    await this.loadProjects();
  },

  async loadRevisions() {
    if (!this.projectId) {
      this.revisions = [];
      return;
    }
    try {
      const response = await authFetch(
        `/api/workspaces/${encodeURIComponent(this.projectId)}/revisions`,
      );
      const data = await readJson(response, '读取 revision 失败');
      this.revisions = data.revisions || [];
    } catch (err) {
      this.projectMessage = `revision 读取失败：${err}`;
    }
  },

  async selectProject(workspaceId) {
    this.projectId = workspaceId || '';
    const project = this.selectedProject();
    if (project) {
      this.workspace = project.root_path;
      this.workspaceInput = project.root_path;
      this.rememberWorkspace();
    }
    await this.loadRevisions();
  },

  async pollProjectJob(kind, workspaceId, jobId) {
    const label = kind === 'uploads' ? '上传' : '导入';
    for (let index = 0; index < 900; index += 1) {
      const response = await authFetch(
        `/api/workspaces/${encodeURIComponent(workspaceId)}/` +
        `${kind}/${encodeURIComponent(jobId)}`,
      );
      const data = await readJson(response, '读取任务状态失败');
      const scan = data.scan_status ? ` · 扫描 ${data.scan_status}` : '';
      this.projectMessage = `${label}：${data.status}${scan}`;
      if (['active', 'failed', 'cancelled'].includes(data.status)) {
        this.projectJobId = '';
        this.projectJobKind = '';
        this.projectJobWorkspaceId = '';
        if (data.status === 'active') {
          this.projectMessage =
            `项目已就绪 · ${data.files || 0} 个文件 · ` +
            `${this.formatBytes(data.bytes || 0)}`;
          await this.loadProjects();
          await this.selectProject(workspaceId);
          this.toastMessage('项目已就绪');
        } else {
          this.projectMessage =
            `${label}${data.status}：${data.error || ''}`;
          await this.loadProjects();
          this.toastMessage(this.projectMessage);
        }
        return data;
      }
      await this.sleep(1000);
    }
    throw new Error(`${label}任务超时`);
  },

  async importLocalProject() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge) {
      this.toastMessage('浏览器模式不支持选择本地文件夹；请上传归档');
      return;
    }
    try {
      const path = await bridge.pick_folder();
      if (!path) return;
      const name = path.split('/').filter(Boolean).pop() || 'project';
      this.projectBusy = true;
      this.projectMessage = `正在导入 ${path} …`;
      const created = await readJson(
        await authFetch('/api/workspaces', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': newIdempotencyKey(),
          },
          body: JSON.stringify({
            display_name: name,
            source: 'local_import',
          }),
        }),
        '创建项目失败',
      );
      const job = await readJson(
        await authFetch(
          `/api/workspaces/${encodeURIComponent(created.workspace_id)}/imports`,
          {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'Idempotency-Key': newIdempotencyKey(),
            },
            body: JSON.stringify({ source_path: path }),
          },
        ),
        '启动导入失败',
      );
      this.projectJobKind = 'imports';
      this.projectJobWorkspaceId = created.workspace_id;
      this.projectJobId = job.job_id;
      await this.pollProjectJob(
        'imports', created.workspace_id, job.job_id,
      );
    } catch (err) {
      this.projectMessage = `导入失败：${err}`;
      this.toastMessage(this.projectMessage);
    } finally {
      this.projectBusy = false;
    }
  },

  onProjectFile(event) {
    const file = event.target.files && event.target.files[0];
    this.projectFile = file || null;
    this.projectFileName = file ? file.name : '';
  },

  onProjectDrop(event) {
    this.projectDropActive = false;
    const files = event.dataTransfer && event.dataTransfer.files;
    const file = files && files[0];
    if (!file) return;
    this.projectFile = file;
    this.projectFileName = file.name;
    this.toastMessage(`已选择归档：${file.name}`);
  },

  async cancelProjectJob() {
    if (!this.projectJobId || !this.projectJobWorkspaceId) return;
    if (!window.confirm('取消当前导入/上传任务？已复制的内容会被清理。')) {
      return;
    }
    try {
      await authFetch(
        `/api/workspaces/${encodeURIComponent(this.projectJobWorkspaceId)}` +
        `/${this.projectJobKind}/${encodeURIComponent(this.projectJobId)}`,
        { method: 'DELETE' },
      );
      this.projectMessage = '已请求取消，等待任务收尾…';
      this.toastMessage('已请求取消');
    } catch (err) {
      this.toastMessage(`取消失败：${err}`);
    }
  },

  async uploadProject() {
    if (!this.projectFile) {
      this.toastMessage('请选择 zip / tar / tar.gz 归档');
      return;
    }
    const file = this.projectFile;
    const name = file.name.replace(
      /\.(zip|tar|tar\.gz|tgz)$/i, '',
    ) || 'project';
    try {
      this.projectBusy = true;
      this.projectMessage = `正在上传 ${file.name} …`;
      const created = await readJson(
        await authFetch('/api/workspaces', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Idempotency-Key': newIdempotencyKey(),
          },
          body: JSON.stringify({ display_name: name, source: 'upload' }),
        }),
        '创建项目失败',
      );
      const form = new FormData();
      form.append('archive', file, file.name);
      const job = await readJson(
        await authFetch(
          `/api/workspaces/${encodeURIComponent(created.workspace_id)}/uploads`,
          {
            method: 'POST',
            headers: { 'Idempotency-Key': newIdempotencyKey() },
            body: form,
          },
        ),
        '启动上传失败',
      );
      this.projectJobKind = 'uploads';
      this.projectJobWorkspaceId = created.workspace_id;
      this.projectJobId = job.job_id;
      await this.pollProjectJob(
        'uploads', created.workspace_id, job.job_id,
      );
    } catch (err) {
      this.projectMessage = `上传失败：${err}`;
      this.toastMessage(this.projectMessage);
    } finally {
      this.projectBusy = false;
      this.projectFile = null;
      this.projectFileName = '';
    }
  },

  async rollbackRevision(revision) {
    if (!this.projectId || this.revisionBusy) return;
    if (!window.confirm(
      `回滚到 ${revision.revision_id}？当前状态会先自动留底。`,
    )) return;
    try {
      this.revisionBusy = true;
      await readJson(
        await authFetch(
          `/api/workspaces/${encodeURIComponent(this.projectId)}/rollback`,
          {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'Idempotency-Key': newIdempotencyKey(),
            },
            body: JSON.stringify({ revision_id: revision.revision_id }),
          },
        ),
        '回滚失败',
      );
      this.projectMessage = `已回滚到 ${revision.revision_id.slice(0, 8)}`;
      await this.loadProjects();
      await this.loadRevisions();
    } catch (err) {
      this.toastMessage(`回滚失败：${err}`);
    } finally {
      this.revisionBusy = false;
    }
  },

  async restoreRevision(revision) {
    if (!this.projectId || this.revisionBusy) return;
    if (!revision.archive_path) {
      this.toastMessage('该 revision 没有归档文件');
      return;
    }
    if (!window.confirm(
      `从归档恢复 ${revision.revision_id.slice(0, 8)}？` +
      '当前状态会先自动留底。',
    )) return;
    try {
      this.revisionBusy = true;
      await readJson(
        await authFetch(
          `/api/workspaces/${encodeURIComponent(this.projectId)}` +
          `/revisions/${encodeURIComponent(revision.revision_id)}/restore`,
          {
            method: 'POST',
            headers: {
              'Idempotency-Key': newIdempotencyKey(),
            },
          },
        ),
        '从归档恢复失败',
      );
      this.projectMessage =
        `已从归档恢复 ${revision.revision_id.slice(0, 8)}`;
      await this.loadProjects();
      await this.loadRevisions();
    } catch (err) {
      this.toastMessage(`恢复失败：${err}`);
    } finally {
      this.revisionBusy = false;
    }
  },

  async pinRevision(revision) {
    if (!this.projectId || this.revisionBusy) return;
    try {
      this.revisionBusy = true;
      await readJson(
        await authFetch(
          `/api/workspaces/${encodeURIComponent(this.projectId)}` +
          `/revisions/${encodeURIComponent(revision.revision_id)}/pin`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ pinned: !revision.pinned }),
          },
        ),
        'pin 失败',
      );
      await this.loadRevisions();
    } catch (err) {
      this.toastMessage(`pin 失败：${err}`);
    } finally {
      this.revisionBusy = false;
    }
  },

  async releaseLease() {
    if (!this.threadId) return;
    try {
      const data = await readJson(
        await authFetch(
          `/api/ops/leases/${encodeURIComponent(this.threadId)}/release`,
          { method: 'POST' },
        ),
        '释放租约失败',
      );
      this.toastMessage(
        data.released ? '当前会话租约已释放' : '当前会话没有活跃租约',
      );
    } catch (err) {
      this.toastMessage(`释放租约失败：${err}`);
    }
  },

  async purgeMaintenance() {
    try {
      const data = await readJson(
        await authFetch('/api/ops/maintenance/purge', { method: 'POST' }),
        '清理失败',
      );
      this.toastMessage(
        `已清理过期租约 ${data.leases || 0} 个、幂等键 ${data.idempotency || 0} 个`,
      );
    } catch (err) {
      this.toastMessage(`清理失败：${err}`);
    }
  },

  async pruneRevisions() {
    if (!this.projectId || this.revisionBusy) return;
    try {
      this.revisionBusy = true;
      const data = await readJson(
        await authFetch(
          `/api/workspaces/${encodeURIComponent(this.projectId)}/revisions/prune`,
          { method: 'POST' },
        ),
        '清理失败',
      );
      this.projectMessage = `已清理 ${data.pruned ? data.pruned.length : 0} 个旧归档`;
      await this.loadRevisions();
    } catch (err) {
      this.toastMessage(`清理失败：${err}`);
    } finally {
      this.revisionBusy = false;
    }
  },

  applyBodyClasses() {
    this.bodyClasses = `theme-${this.theme} density-${this.density} font-${this.fontSize} family-${this.fontFamily}`;
  },

  refreshBadgeClass() {
    this.badgeClasses = this.capabilities.read_only
      ? 'badge badge-read'
      : 'badge badge-write';
  },

  bodyClass() {
    return `theme-${this.theme} density-${this.density} font-${this.fontSize} family-${this.fontFamily}`;
  },

  tabClass(name) { return this.tab === name ? 'active' : ''; },
  sessionClass(id) { return id === this.threadId ? 'active' : ''; },
  sessionTitle(session) { return session.title || session.thread_id; },
  sessionMeta(session) {
    return `${session.thread_id} · ${session.last_active || ''}`;
  },
  toolClass(tool) { return `tool-${tool.state || 'running'}`; },
  taskClass(task) { return `task-${task.status || 'pending'}`; },
  auditText(record) { return record.tool || record.detail || ''; },
  loginMessage() { return this.loginError || '输入访问 token（桌面端会自动填入）'; },
  workspaceLabel() { return this.workspace || '未选择工作区'; },
  providerKey(provider) { return `${provider.name}:${provider.model}`; },
  providerLabel(provider) { return `${provider.name} / ${provider.model}`; },
  syncModel() {
    const found = this.providers.find((item) => item.model === this.model);
    this.provider = found ? found.name : '';
  },
  fileKey(file) { return `${file.snapshot_id}:${file.path}`; },
  planEmpty() { return this.plan.length === 0; },
  messagesEmpty() { return this.messages.length === 0; },
  filesEmpty() { return this.files.length === 0 && !this.diffText; },
  planClass(index) {
    const classes = [];
    if (index === this.stepIdx) classes.push('current');
    if (index < this.stepIdx) classes.push('done');
    return classes.join(' ');
  },
  buildPlanRows(steps, kinds, current) {
    return (steps || []).map((text, index) => ({
      text,
      kind: (kinds || [])[index] || '',
      number: index + 1,
      _cls: index === current ? 'current' : (index < current ? 'done' : ''),
    }));
  },
  decorateTask(task) {
    const status = task.status || 'pending';
    const depends = Array.isArray(task.depends_on) ? task.depends_on : [];
    return {
      ...task,
      status,
      depends_on: depends,
      worker_id: task.worker_id || '',
      duration_ms: Number(task.duration_ms || 0),
      input_tokens: Number(task.input_tokens || 0),
      output_tokens: Number(task.output_tokens || 0),
      tool_calls: Number(task.tool_calls || 0),
      attempt: Number(task.attempt || 1),
      merge_error: task.merge_error || '',
      _status: status,
      _cls: `task-${status}`,
      _text: `${task.id || task.task_id || ''} ${task.goal || ''}`,
      _depends: depends.length ? `依赖 ${depends.join(', ')}` : '',
      _worker: task.worker_id || '',
      _timing: formatDuration(task.duration_ms),
      _tokens: formatTokens(task.input_tokens, task.output_tokens),
    };
  },
  kindLabel(index) { return this.kinds[index] || ''; },
  textOrEmpty(value) { return value || ''; },
  taskStatus(task) { return task.status || 'pending'; },
  taskText(task) { return `${task.id || task.task_id || ''} ${task.goal || ''}`; },
  stepNumber(index) { return index + 1; },
  roleLabel(role) {
    if (role === 'user') return '你';
    if (role === 'assistant') return '助手';
    return '系统';
  },
  msgClass(message) { return `msg-${message.role}`; },
  sendDisabled() {
    return (
      this.busy ||
      this.paused ||
      this.approvals.length > 0 ||
      !this.prompt.trim()
    );
  },
  isTab(name) { return this.tab === name; },
  setTab(name) {
    this.tab = name;
    const next = {
      plan: '', tools: '', files: '', tree: '', git: '',
      tasks: '', memory: '', audit: '',
    };
    if (name in next) next[name] = 'active';
    this.tabClasses = next;
    if (name === 'tree') this.loadTree(this.treePath);
    if (name === 'git') this.loadGit(false);
  },
  toggleSettings() {
    this.showSettings = !this.showSettings;
    if (this.showSettings) {
      this.probeProject();
      this.loadProfiles();
    }
  },
  openMemory() { this.tab = 'memory'; this.loadMemory(); },
  openAudit() { this.tab = 'audit'; this.loadAudit(); },
  approvalField(name) { return this.approval ? (this.approval[name] || '') : ''; },
  approvalTool() { return this.approval ? `工具：${this.approval.tool}` : ''; },
  badgeClass() {
    return this.capabilities.read_only ? 'badge-read' : 'badge-write';
  },
  accessLabel() {
    if (this.capabilities.read_only) return '只读';
    return this.accessMode === 'write_auto' ? '自动写' : '审批写';
  },
  accessDisabled() {
    return !this.capabilities.server_write_enabled;
  },
  escapeHtml(text) { return escapeHtml(text); },
  renderMarkdown(text) { return renderMarkdown(text); },

  async refreshHealth() {
    const response = await authFetch('/api/health');
    const data = await readJson(response, '无法读取服务状态');
    this.healthText = data.reachable
      ? `${data.model} · ${data.user || ''}`
      : `API 不可用：${data.error || '未配置 Key'}`;
    if (!this.model) this.model = data.model || '';
    if (!this.workspace && data.workspace) {
      this.workspace = data.workspace;
      this.workspaceInput = data.workspace;
    }
  },

  async loadWorkspace() {
    const response = await authFetch('/api/workspace');
    const data = await readJson(response, '无法读取工作区');
    if (data.workspace) {
      this.workspace = data.workspace;
      this.workspaceInput = this.workspaceInput || data.workspace;
    }
  },

  async loadCapabilities() {
    const query = this.threadId
      ? `?thread_id=${encodeURIComponent(this.threadId)}`
      : '';
    const response = await authFetch(`/api/capabilities${query}`);
    this.capabilities = await readJson(response, '无法读取能力');
    const budget = Number(this.capabilities.run_cost_budget_usd || 0);
    const maxUpload = Number(this.capabilities.max_upload_bytes || 0);
    const projectUpload = Number(
      this.capabilities.workspace_upload_max_bytes || 0,
    );
    this.limitsText =
      `单次运行成本预算：${budget > 0 ? '$' + budget.toFixed(2) : '未设置'} · ` +
      `单文件上传上限：${this.formatBytes(maxUpload)}` +
      (projectUpload
        ? ` · 项目归档上限：${this.formatBytes(projectUpload)}`
        : '');
    this.refreshBadgeClass();
    if (this.capabilities.workspace_registry) {
      await this.loadProjects();
      await this.loadRevisions();
    }
  },

  async loadProviders() {
    try {
      const response = await authFetch('/api/providers');
      if (!response.ok) return;
      const data = await response.json();
      this.providers = (data.providers || []).map((item) => ({
        ...item,
        _key: `${item.name}:${item.model}`,
        _label: `${item.name} / ${item.model}`,
      }));
      if (this.providers.length && !this.providers.some((p) => p.name === this.providerName)) {
        this.providerName = this.providers[0].name;
      }
    } catch (e) { /* provider 端点是 P1，可缺省 */ }
  },

  async saveProviderKey() {
    const key = this.providerKeyInput.trim();
    if (!key) return;
    try {
      const response = await authFetch(
        `/api/providers/${encodeURIComponent(this.providerName)}/key`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ api_key: key }),
        },
      );
      await readJson(response, '保存密钥失败');
      this.providerKeyInput = '';
      this.toastMessage('密钥已保存到系统密钥环');
      await this.loadProviders();
      await this.refreshHealth();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async testProvider() {
    try {
      const response = await authFetch('/api/providers/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: this.providerName }),
      });
      const data = await readJson(response, '测试连接失败');
      if (data.ok) {
        this.toastMessage(`连接成功：${data.model || data.status_code}`);
      } else {
        this.toastMessage(`连接失败：${data.error || data.status_code}`);
      }
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async deleteProviderKey() {
    try {
      const response = await authFetch(
        `/api/providers/${encodeURIComponent(this.providerName)}/key`,
        { method: 'DELETE' },
      );
      await readJson(response, '删除密钥失败');
      this.toastMessage('密钥已删除');
      await this.loadProviders();
    } catch (err) {
      this.toastMessage(String(err));
    }
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

  filterSessions() {
    const q = this.sessionQuery.trim().toLowerCase();
    this.sessionRows = q
      ? this.sessions.filter((row) =>
          row._title.toLowerCase().includes(q) ||
          row.thread_id.toLowerCase().includes(q) ||
          String(row.workspace || '').toLowerCase().includes(q))
      : this.sessions.slice();
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
    this.workspace = data.workspace;
    this.workspaceInput = data.workspace;
    if (data.workspace_id) this.projectId = data.workspace_id;
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
    this._toolMap = new Map();
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
    if (row) {
      if (row.workspace_id) this.projectId = row.workspace_id;
      if (row.workspace) {
        this.workspace = row.workspace;
        this.workspaceInput = row.workspace;
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
      for (const item of data.messages || []) {
        this.pushMessage(item.role, item.text, true);
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

  pushMessage(role, text, done = true) {
    const message = {
      id: ++this._msgSeq,
      role,
      text,
      html: renderMarkdown(text),
      done,
      _cls: `msg-${role}`,
    };
    this.messages.push(message);
    this.scrollBottom();
    this.$nextTick(() => this.decorateTranscript());
    return message;
  },

  decorateTranscript() {
    const root = this.$refs.transcript;
    if (!root) return;
    root.querySelectorAll('.msg-body pre').forEach((pre) => {
      const code = pre.querySelector('code');
      if (window.hljs && code && !code.dataset.highlighted) {
        try { window.hljs.highlightElement(code); } catch (e) { /* ignore */ }
      }
      if (pre.dataset.copyReady) return;
      pre.dataset.copyReady = '1';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'copy-code';
      button.textContent = '复制代码';
      button.setAttribute('aria-label', '复制代码块');
      button.addEventListener('click', () => this.copyCode(pre));
      pre.insertAdjacentElement('beforebegin', button);
    });
  },

  async copyText(text) {
    const value = String(text || '');
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
      this.toastMessage('已复制');
    } catch (err) {
      this.toastMessage('复制失败：当前环境不支持剪贴板');
    }
  },

  async copyCode(pre) {
    const code = pre.querySelector('code');
    await this.copyText((code ? code.textContent : pre.textContent) || '');
  },

  appendAssistantToken(node, text) {
    if (!this._currentMessage || this._currentNode !== node) {
      this._currentNode = node;
      this._currentMessage = {
        id: ++this._msgSeq,
        role: 'assistant',
        text: '',
        html: '',
        done: false,
        _cls: 'msg-assistant',
      };
      this.messages.push(this._currentMessage);
    }
    this._currentMessage.text += text;
    const id = this._currentMessage.id;
    clearTimeout(this._renderTimers.get(id));
    this._renderTimers.set(id, setTimeout(() => {
      const target = this.messages.find((item) => item.id === id);
      if (target) target.html = renderMarkdown(target.text);
      this.scrollBottom();
    }, 80));
  },

  finishAssistant() {
    if (this._currentMessage) {
      this._currentMessage.done = true;
      this._currentMessage.html = renderMarkdown(this._currentMessage.text);
    }
    this._currentMessage = null;
    this._currentNode = '';
    this.$nextTick(() => this.decorateTranscript());
  },

  scrollBottom() {
    this.$nextTick(() => {
      const element = this.$refs.transcript;
      if (element) element.scrollTop = element.scrollHeight;
    });
  },

  async send(override = '', appendUser = true) {
    const text = (
      typeof override === 'string' && override ? override : (this.prompt || '')
    ).trim();
    if (!text || this.busy) return;
    if (this.approvals.length || this.paused) {
      this.toastMessage('请先处理挂起的审批/暂停，或取消当前任务');
      return;
    }
    try {
      await this.ensureSession();
    } catch (e) {
      this.toastMessage(String(e));
      return;
    }
    this.prompt = '';
    if (appendUser) this.pushMessage('user', text);
    this._lastPrompt = text;
    this.busy = true;
    this.paused = false;
    this.stateText = '运行中…';
    this.connectionState = 'streaming';
    this._terminal = false;
    this._suspended = false;
    this._retryCount = 0;
    this.reasoningBlocks = [];
    this.plan = [];
    this.kinds = [];
    this.planRows = [];
    this.stepIdx = -1;
    this.planDegraded = false;
    this.tools = [];
    this._toolMap = new Map();
    this.diffText = '';
    this.lastSeq = 0;
    this.canReconnect = false;
    this._currentMessage = null;
    this._currentNode = '';
    try {
      const response = await authFetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: text, thread_id: this.threadId }),
      });
      if (await this.handleRunConflict(response)) return;
      await this.consume(response);
    } catch (err) {
      this.failRun(err);
    } finally {
      this.afterStream();
    }
  },

  prompt: '',

  async consume(response) {
    this.connectionState = 'streaming';
    await consumeSse(
      response,
      ({ event, seq }) => {
        if (Number.isFinite(seq)) this.lastSeq = Math.max(this.lastSeq, seq);
        this.handleEvent(event);
      },
      { getLastSeq: () => this.lastSeq },
    );
  },

  async handleRunConflict(response) {
    if (response.status !== 409) return false;
    let payload: any = {};
    try {
      payload = await response.json();
    } catch (err) {
      throw new Error('该会话正在运行');
    }
    const detail = payload.detail;
    if (detail && detail.code === 'run_in_progress') {
      this.toastMessage('该会话已在运行，正在连接事件流…');
      await this.reconnect();
      return true;
    }
    throw new Error(
      typeof detail === 'string' ? detail : JSON.stringify(detail || payload),
    );
  },

  async reconnect() {
    if (!this.threadId) return;
    if (this._retryTimer) {
      clearTimeout(this._retryTimer);
      this._retryTimer = null;
    }
    this.busy = true;
    this.stateText = '重连中…';
    this.connectionState = 'reconnecting';
    this.canReconnect = false;
    try {
      const response = await authFetch(
        `/api/stream/${encodeURIComponent(this.threadId)}?after=${this.lastSeq}`,
      );
      await this.consume(response);
    } catch (err) {
      this.toastMessage(`重连失败：${err}`);
      this.canReconnect = true;
    } finally {
      await this.afterStream();
    }
  },

  scheduleReconnect() {
    if (this._retryTimer) return;
    this._retryCount += 1;
    const delay = Math.min(8000, 500 * 2 ** this._retryCount);
    this.stateText = `连接中断，${Math.round(delay / 1000)}s 后重连…`;
    this.connectionState = 'reconnecting';
    this._retryTimer = setTimeout(() => {
      this._retryTimer = null;
      this.reconnect();
    }, delay);
  },

  retry() {
    if (this.busy || !this._lastPrompt) return;
    this.canReconnect = false;
    this.connectionState = 'idle';
    this.send(this._lastPrompt, false);
  },

  connectionText() {
    if (this.connectionState === 'streaming') return '流式连接中';
    if (this.connectionState === 'reconnecting') return '重连中…';
    if (this.connectionState === 'closed') return '连接已关闭';
    return '';
  },

  async afterStream() {
    this.finishAssistant();
    if (
      this.canReconnect &&
      !this._terminal &&
      !this._suspended &&
      this._retryCount < 3
    ) {
      this.scheduleReconnect();
      return;
    }
    this.connectionState = this._terminal ? 'idle' : 'closed';
    if (this.stateText === '连接中断') {
      this.busy = false;
      return;
    }
    if (this.approvals.length) {
      this.stateText = '等待审批';
      this.showNextApproval();
      this.busy = false;
      return;
    }
    if (this.paused) {
      this.stateText = '已暂停';
      this.busy = false;
      return;
    }
    this.busy = false;
    if (this.stateText === '运行中…') this.stateText = '就绪';
    this.canReconnect = false;
    await this.loadSessions();
  },

  failRun(err) {
    this.busy = false;
    this.finishAssistant();
    this.stateText = '连接中断';
    this.connectionState = 'closed';
    this.canReconnect = Boolean(this.threadId && this.lastSeq > 0);
    this.pushMessage('system', `运行失败：${err && err.message ? err.message : err}`);
  },

  handleEvent(event) {
    switch (event.type) {
      case 'run_started':
        this.stateText = '运行中…';
        this.connectionState = 'streaming';
        this._terminal = false;
        this._suspended = false;
        break;
      case 'plan_created':
        this.plan = event.steps || [];
        this.kinds = event.kinds || [];
        this.planRows = this.buildPlanRows(this.plan, this.kinds, 0);
        this.planDegraded = !!event.degraded;
        this.stepIdx = 0;
        this.tab = 'plan';
        break;
      case 'plan_revised':
        this.plan = event.steps || [];
        this.kinds = event.kinds || [];
        this.stepIdx = event.step_idx || 0;
        this.planRows = this.buildPlanRows(this.plan, this.kinds, this.stepIdx);
        this.pushMessage('system', '计划已修订');
        break;
      case 'step_started':
        this.stepIdx = event.index || 0;
        this.planRows = this.buildPlanRows(this.plan, this.kinds, this.stepIdx);
        if ((event.total || 1) > 1) {
          this.pushMessage('system', `第 ${event.index + 1}/${event.total} 步：${event.text}`);
        }
        break;
      case 'step_finished':
        if (event.cancelled) this.pushMessage('system', '该步骤被取消');
        else if (event.budget_exhausted) this.pushMessage('system', '该步骤工具预算耗尽');
        break;
      case 'assistant_token':
        this.appendAssistantToken(event.node, event.text);
        break;
      case 'reasoning_token': {
        const node = event.node || '';
        let block = this.reasoningBlocks[this.reasoningBlocks.length - 1];
        if (!block || block.node !== node) {
          block = { node, text: '' };
          this.reasoningBlocks.push(block);
        }
        block.text += event.text || '';
        break;
      }
      case 'tool_call_started':
        this.upsertTool(event);
        this.tab = 'tools';
        break;
      case 'tool_call_finished':
        this.finishTool(event);
        break;
      case 'file_changed':
        this.files.unshift({ ...event, _key: `${event.snapshot_id}:${event.path}` });
        this.tab = 'files';
        break;
      case 'verification':
        this.pushMessage('system', this.verificationText(event));
        break;
      case 'review_finished':
        this.pushMessage('system', this.reviewText(event));
        break;
      case 'repair_started':
        this.pushMessage('system', `第 ${event.attempt}/${event.limit} 次修复：${event.summary}`);
        break;
      case 'task_graph_created':
        this.tasks = (event.tasks || []).map((task) => this.decorateTask(task));
        this.tab = 'tasks';
        break;
      case 'task_graph_extended':
        this.tasks = this.tasks.concat((event.added || []).map((task) => this.decorateTask(task)));
        break;
      case 'worker_started':
        this.upsertWorker(event);
        break;
      case 'worker_finished':
        this.finishWorker(event);
        break;
      case 'subagent_started':
        this.upsertTool({
          call_id: event.agent_id,
          name: `subagent:${event.role}`,
          summary: event.goal,
          level: '只读',
        });
        break;
      case 'subagent_finished':
        this.finishTool({
          call_id: event.agent_id,
          name: 'subagent',
          ok: event.status === 'success',
          preview: event.summary,
        });
        break;
      case 'orchestration_finished':
        this.pushMessage('system', `编排完成：${event.completed} 成功 / ${event.failed} 失败`);
        break;
      case 'workflow_selected':
        this.pushMessage('system', `工作流：${event.workflow}${event.reason ? '（' + event.reason + '）' : ''}`);
        break;
      case 'context_trimmed':
        if (event.trimmed) {
          this.pushMessage('system', `上下文裁剪：${event.trimmed} 条，${event.before_tokens} → ${event.after_tokens} tokens`);
        }
        break;
      case 'cost_report':
        this.costText = this.formatCost(event);
        for (const alert of event.alerts || []) this.toastMessage(alert);
        break;
      case 'approval_requested':
        this.approvals.push(event);
        this.showNextApproval();
        this.stateText = '等待审批';
        this._suspended = true;
        this._terminal = false;
        this.notify('CodingAgent 需要审批', event.command || event.tool || '');
        break;
      case 'steering_applied':
        if (event.action === 'pause') {
          this.paused = true;
          this.stateText = '已暂停';
          this._suspended = true;
          this.pushMessage('system', `已暂停：${event.detail || ''}`);
        } else if (event.action === 'resume') {
          this.paused = false;
          this.stateText = '运行中…';
          this._suspended = false;
          this._terminal = false;
        } else {
          this.pushMessage('system', `steering：${event.action} ${event.detail || ''}`);
        }
        break;
      case 'run_finished':
        this.stateText = '就绪';
        this.busy = false;
        this._terminal = true;
        this._suspended = false;
        this.connectionState = 'idle';
        if (event.answer && !this._currentMessage) {
          this.pushMessage('assistant', event.answer, true);
        }
        this.notify('CodingAgent 运行完成', event.answer || '');
        break;
      case 'run_failed':
        this.stateText = '失败';
        this._terminal = true;
        this._suspended = false;
        this.connectionState = 'closed';
        this.pushMessage('system', `运行失败：${event.message}`);
        this.notify('CodingAgent 运行失败', event.message || '');
        break;
      default:
        break;
    }
  },

  verificationText(event) {
    if (event.ok === true) return `验证通过：${event.summary}`;
    if (event.ok === false) return `验证失败：${event.summary}`;
    return `未验证：${event.status} ${event.summary}`;
  },

  reviewText(event) {
    if (event.blocked) return `审查阻断：${event.summary}`;
    if (event.status === 'warned') return `审查告警：${event.summary}`;
    if (event.status === 'clean') return '审查通过';
    return `审查：${event.status}`;
  },

  formatCost(event) {
    let text = `$${Number(event.cost_usd || 0).toFixed(4)}`;
    if (event.budget_usd > 0) {
      text += ` / $${Number(event.budget_usd).toFixed(2)}（${Math.round((event.ratio || 0) * 100)}%）`;
    }
    return text;
  },

  upsertTool(event) {
    const key = event.call_id || `${event.name}-${this.tools.length}`;
    const existing = this._toolMap.get(key);
    if (existing) {
      existing.name = event.name || existing.name;
      existing.summary = event.summary || existing.summary;
      existing.level = event.level || existing.level;
      return;
    }
    const tool = {
      key,
      call_id: event.call_id || key,
      name: event.name || 'tool',
      summary: event.summary || '',
      level: event.level || '',
      state: 'running',
      preview: '',
      _cls: 'tool-running',
    };
    this._toolMap.set(key, tool);
    this.tools.unshift(tool);
  },

  finishTool(event) {
    const key = event.call_id || '';
    let tool = key ? this._toolMap.get(key) : null;
    if (!tool) {
      tool = {
        key: key || `${event.name}-${this.tools.length}`,
        call_id: key,
        name: event.name || 'tool',
        summary: '',
        level: event.level || '',
        state: 'running',
        preview: '',
      };
      this._toolMap.set(tool.key, tool);
      this.tools.unshift(tool);
    }
    if (event.rejected) tool.state = 'rejected';
    else if (event.skipped) tool.state = 'skipped';
    else if (event.interrupted) tool.state = 'interrupted';
    else if (event.ok) tool.state = 'ok';
    else tool.state = 'error';
    tool._cls = `tool-${tool.state}`;
    if (event.preview) tool.preview = event.preview;
    else if (event.exit_code !== null && event.exit_code !== undefined) {
      tool.preview = `exit=${event.exit_code}`;
    }
  },

  upsertWorker(event) {
    const id = event.task_id || event.worker_id;
    const existing = this.tasks.find((task) => (task.id || task.task_id) === id);
    if (existing) {
      existing.status = 'running';
      existing._status = 'running';
      existing._cls = 'task-running';
      existing.worker_id = event.worker_id || existing.worker_id;
      existing._worker = existing.worker_id || '';
      existing.attempt = Number(event.attempt || existing.attempt || 1);
      existing.queue_wait_ms = Number(event.queue_wait_ms || 0);
      return;
    }
    this.tasks.push(this.decorateTask({
      id,
      goal: event.goal,
      role: event.role,
      status: 'running',
      worker_id: event.worker_id,
      attempt: event.attempt,
      queue_wait_ms: event.queue_wait_ms,
    }));
  },

  finishWorker(event) {
    const id = event.task_id || event.worker_id;
    const existing = this.tasks.find((task) => (task.id || task.task_id) === id);
    if (!existing) return;
    existing.status = event.status || 'done';
    existing._status = existing.status;
    existing._cls = `task-${existing.status}`;
    existing.worker_id = event.worker_id || existing.worker_id || '';
    existing.merged = event.merged;
    existing.merge_error = event.merge_error || '';
    existing.summary = event.summary;
    existing.duration_ms = Number(event.duration_ms || existing.duration_ms || 0);
    existing.input_tokens = Number(event.input_tokens || 0);
    existing.output_tokens = Number(event.output_tokens || 0);
    existing.tool_calls = Number(event.tool_calls || 0);
    existing.attempt = Number(event.attempt || existing.attempt || 1);
    existing._worker = existing.worker_id;
    existing._timing = formatDuration(existing.duration_ms);
    existing._tokens = formatTokens(existing.input_tokens, existing.output_tokens);
  },

  showNextApproval() {
    this.approval = this.approvals[0] || null;
    this.approvalPosition = this.approvals.length > 1
      ? `还剩 ${this.approvals.length} 条待确认`
      : '';
  },

  async decide(granted) {
    if (!this.approval) return;
    this.decisions[this.approval.request_id] = !!granted;
    this.approvals.shift();
    if (this.approvals.length) {
      this.showNextApproval();
      return;
    }
    await this.resumeApprovals();
  },

  async decideAll(granted) {
    for (const item of this.approvals) {
      this.decisions[item.request_id] = !!granted;
    }
    this.approvals = [];
    await this.resumeApprovals();
  },

  async resumeApprovals() {
    this.approval = null;
    this.busy = true;
    this.stateText = '恢复中…';
    // 序号跨 resume 连续；保留 lastSeq，避免重放历史事件。
    this.canReconnect = false;
    this.connectionState = 'streaming';
    this._terminal = false;
    this._suspended = false;
    this._retryCount = 0;
    try {
      const response = await authFetch(`/api/resume/${encodeURIComponent(this.threadId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ decisions: this.decisions }),
      });
      this.decisions = {};
      if (await this.handleRunConflict(response)) return;
      await this.consume(response);
    } catch (err) {
      this.failRun(err);
    } finally {
      this.afterStream();
    }
  },

  async steer(action, text = '') {
    if (!this.threadId) return;
    try {
      const response = await authFetch(`/api/steer/${encodeURIComponent(this.threadId)}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, text, role: 'explore' }),
      });
      const data = await readJson(response, 'steering 失败');
      this.toastMessage(`${action} 已提交`);
      if (action === 'cancel' && this.paused) {
        // 暂停时 cancel 只是入队；必须触发恢复，节点边界才会消费它。
        await this.resumeSteering();
      } else if (this.paused && (action === 'add' || action === 'replan')) {
        this.toastMessage(`${action} 已排队，点击“继续”后生效`);
      }
      return data;
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async addTask() {
    const text = window.prompt('追加一个只读探索任务：');
    if (text && text.trim()) await this.steer('add', text.trim());
  },

  async replan() {
    await this.steer('replan');
  },

  async resumeSteering() {
    if (!this.threadId) return;
    this.busy = true;
    this.stateText = '恢复中…';
    this.canReconnect = false;
    this.connectionState = 'streaming';
    this._terminal = false;
    this._suspended = false;
    this._retryCount = 0;
    try {
      const response = await authFetch(`/api/resume_steering/${encodeURIComponent(this.threadId)}`, {
        method: 'POST',
      });
      if (await this.handleRunConflict(response)) return;
      await this.consume(response);
    } catch (err) {
      this.failRun(err);
    } finally {
      await this.afterStream();
    }
  },

  async uploadFiles(event) {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    await this.uploadFileList(files);
  },

  async uploadFileList(files) {
    if (!files.length) return;
    try {
      if (!this.threadId) await this.createSession();
      const form = new FormData();
      for (const file of files) form.append('files', file);
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(this.threadId)}/uploads`,
        { method: 'POST', body: form },
      );
      const data = await readJson(response, '上传失败');
      const paths = (data.uploads || []).map((item) => item.relative_path).join('\n');
      this.pushMessage('system', `已上传到工作区：\n${paths}`);
      this.toastMessage(`已上传 ${data.uploads.length} 个文件`);
      await this.loadUploads();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

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

  async loadUploads() {
    if (!this.threadId) {
      this.uploads = [];
      this.uploadQuota = '';
      return;
    }
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(this.threadId)}/uploads`,
      );
      const data = await readJson(response, '读取上传列表失败');
      this.uploads = (data.uploads || []).map((item) => ({
        ...item,
        _name: item.relative_path.split('/').pop(),
        _size: this.formatBytes(item.bytes),
      }));
      this.uploadQuota =
        `本会话 ${this.formatBytes(data.used_bytes)}` +
        ` · 工作区 ${this.formatBytes(data.workspace_bytes)}` +
        ` / ${this.formatBytes(data.max_total_bytes)}`;
    } catch (err) {
      this.uploadQuota = '';
    }
  },

  async deleteUpload(relativePath) {
    if (!this.threadId) return;
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(this.threadId)}/uploads`,
        {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ path: relativePath }),
        },
      );
      await readJson(response, '删除上传失败');
      await this.loadUploads();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async dropFiles(event) {
    await this.uploadFileList(Array.from(event.dataTransfer.files || []));
  },

  async openRecovery() {
    this.showRecovery = true;
    await this.loadRecovery();
  },

  async loadRecovery() {
    try {
      const response = await authFetch('/api/recovery');
      const data = await readJson(response, '读取恢复中心失败');
      const labels = {
        approval: '待审批',
        paused: '已暂停',
        running: '运行中',
        interrupted: '异常中断',
      };
      this.recoveryItems = (data.items || []).map((item) => ({
        ...item,
        _key: item.thread_id,
        _text: `${item.title} · ${item.last_active || ''}`,
        _status: labels[item.status] || item.status,
        _approvals: Number(item.approvals || 0)
          ? `待审批 ${Number(item.approvals)}`
          : '',
        _cls: item.status === 'interrupted' ? 'note-bad' : '',
      }));
    } catch (err) {
      this.recoveryItems = [];
      this.toastMessage(String(err));
    }
  },

  closeRecovery() {
    this.showRecovery = false;
  },

  async recoveryResume(item) {
    this.showRecovery = false;
    await this.openSession(item.thread_id);
    if (item.status === 'paused' && !Number(item.approvals || 0)) {
      await this.resumeSteering();
    } else if (
      item.status === 'approval' || Number(item.approvals || 0)
    ) {
      await this.loadApprovals();
    }
  },

  async loadProfiles() {
    try {
      const response = await authFetch('/api/provider-profiles');
      const data = await readJson(response, '读取 provider 档案失败');
      this.profiles = data.profiles || [];
      this.profilesEnabled = Boolean(data.profiles_enabled);
    } catch (err) {
      this.profiles = [];
    }
  },

  async saveProfile() {
    const payload = {
      name: this.profileName.trim(),
      base_url: this.profileBaseUrl.trim(),
      model: this.profileModel.trim(),
    };
    if (!payload.name || !payload.base_url || !payload.model) {
      this.toastMessage('provider 名称、base_url、model 都不能为空');
      return;
    }
    try {
      const response = await authFetch('/api/provider-profiles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await readJson(response, '保存 provider 失败');
      this.profiles = data.profiles || [];
      await this.loadProviders();
      this.toastMessage(`provider ${payload.name} 已保存；请设置密钥`);
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async deleteProfile(name) {
    if (!window.confirm(`删除 provider 档案 ${name}？密钥不会自动删除。`)) return;
    try {
      const response = await authFetch(
        `/api/provider-profiles/${encodeURIComponent(name)}`,
        { method: 'DELETE' },
      );
      const data = await readJson(response, '删除 provider 失败');
      this.profiles = data.profiles || [];
      await this.loadProviders();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  editProfile(profile) {
    this.profileName = profile.name;
    this.profileBaseUrl = profile.base_url;
    this.profileModel = profile.model;
  },

  async discoverModels() {
    try {
      const response = await authFetch('/api/providers/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: this.providerName }),
      });
      const data = await readJson(response, '模型发现失败');
      this.modelOptions = data.models || [];
      this.modelOptionsText = this.modelOptions.slice(0, 20).join(', ');
      this.toastMessage(
        data.ok
          ? `发现 ${this.modelOptions.length} 个模型`
          : (data.error || '模型发现失败'),
      );
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  useLocalPreset() {
    this.profileName = this.profileName || 'local';
    this.profileBaseUrl = 'http://127.0.0.1:11434/v1';
    if (!this.profileModel) this.profileModel = 'qwen2.5-coder';
    this.toastMessage('已填入 Ollama / LM Studio 兼容端点，按需修改');
  },

  async collectDiagnostics() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge || this.diagnosticsBusy) return;
    this.diagnosticsBusy = true;
    try {
      const result = await bridge.collect_diagnostics();
      if (result.ok) {
        this.diagnosticsPath = result.path;
        this.toastMessage('诊断包已生成');
      } else {
        this.toastMessage(result.error);
      }
    } catch (err) {
      this.toastMessage(String(err));
    } finally {
      this.diagnosticsBusy = false;
    }
  },

  async revealDiagnostics() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge || !this.diagnosticsPath) return;
    const result = await bridge.reveal_diagnostics(this.diagnosticsPath);
    if (!result.ok) this.toastMessage(result.error);
  },

  async checkUpdate() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge) return;
    try {
      const data = await bridge.check_update();
      this.updateInfo = data;
      if (!data.configured) {
        this.updateText = data.message || '未配置更新清单';
      } else if (!data.ok) {
        this.updateText = `检查失败：${data.error}`;
      } else if (data.available) {
        this.updateText = `可更新：${data.current} → ${data.latest}`;
      } else {
        this.updateText = `已是最新版本 ${data.current}`;
      }
    } catch (err) {
      this.updateText = String(err);
    }
  },

  async openUpdate() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge || !this.updateInfo || !this.updateInfo.download_url) return;
    const result = await bridge.open_url(this.updateInfo.download_url);
    if (!result.ok) this.toastMessage(result.error);
  },

  async toggleNotify() {
    this.notifyEnabled = !this.notifyEnabled;
    if (this.notifyEnabled && 'Notification' in window) {
      try {
        const permission = await Notification.requestPermission();
        if (permission !== 'granted') {
          this.notifyEnabled = false;
          this.toastMessage('浏览器未授予通知权限');
        }
      } catch (err) {
        this.notifyEnabled = false;
      }
    }
    this.saveSettings();
  },

  notify(title, body) {
    if (!this.notifyEnabled) return;
    if (!('Notification' in window) || Notification.permission !== 'granted') {
      return;
    }
    try {
      new Notification(title, { body: String(body || '').slice(0, 120) });
    } catch (err) { /* ignore */ }
  },

  async loadMemory() {
    try {
      const response = await authFetch('/api/memory?scope=project');
      const data = await readJson(response, '读取记忆失败');
      this.memory = data.facts || [];
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async remember() {
    const text = this.memoryText.trim();
    if (!text) return;
    try {
      const response = await authFetch('/api/memory', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'remember', text, thread_id: this.threadId }),
      });
      const data = await readJson(response, '写入记忆失败');
      this.memory = data.facts || [];
      this.memoryText = '';
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async forget(index) {
    try {
      const response = await authFetch('/api/memory', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'forget', index, thread_id: this.threadId }),
      });
      const data = await readJson(response, '删除记忆失败');
      this.memory = data.facts || [];
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async loadAudit() {
    try {
      const params = new URLSearchParams({ limit: '100' });
      if (this.threadId) params.set('thread_id', this.threadId);
      const response = await authFetch(`/api/audit?${params.toString()}`);
      const data = await readJson(response, '读取审计失败');
      this.audit = (data.records || []).slice().reverse().map((item) => ({
        ...item,
        _text: item.tool || item.detail || '',
      }));
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  toastMessage(text) {
    this.toast = text;
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => { this.toast = ''; }, 3200);
  },

  toggleReasoning() {
    this.showReasoning = !this.showReasoning;
    this.saveSettings();
  },

  shortcuts() {
    return SHORTCUTS;
  },

  onGlobalKeydown(event) {
    if (event.defaultPrevented) return;
    const target = event.target as HTMLElement | null;
    const typing = Boolean(target && (
      target.tagName === 'INPUT'
      || target.tagName === 'TEXTAREA'
      || target.isContentEditable
    ));
    const mod = event.ctrlKey || event.metaKey;
    if (mod && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      const search = document.querySelector('.session-search') as HTMLElement | null;
      if (search) search.focus();
      return;
    }
    if (mod && event.key === 'Enter') {
      event.preventDefault();
      this.send();
      return;
    }
    if (event.key === 'Escape') {
      if (this.shortcutsOpen) { this.shortcutsOpen = false; return; }
      if (this.showSettings) { this.showSettings = false; return; }
      if (this.showRecovery) { this.showRecovery = false; return; }
    }
    if (!typing && event.key === '?') {
      event.preventDefault();
      this.shortcutsOpen = !this.shortcutsOpen;
    }
  },

  onEnter(event) {
    if (event.shiftKey) return;
    event.preventDefault();
    this.send();
  },

  openNewSettings() {
    this.addTask();
  },
});

const registerAgentApp = () => {
  window.Alpine.data('agentApp', agentApp);
};

if (window.Alpine) {
  registerAgentApp();
  if (document.readyState !== 'loading') window.Alpine.initTree(document.body);
} else {
  document.addEventListener('alpine:init', registerAgentApp);
}
