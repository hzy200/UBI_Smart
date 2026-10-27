/* Alpine mixin: workspace。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  AuthError,
  ProblemError,
  applyAuthHeaders,
  authFetch,
  authUrl,
  clearStoredToken,
  getToken,
  newIdempotencyKey,
  parseHashToken,
  readJson,
  restoreDesktopToken,
  setToken,
} from '../api/http';
import { escapeHtml, formatDuration, formatTokens, renderMarkdown } from '../ui/format';
import { renderTimers, tokenBuffers, toolMap } from '../state/runtime';
import { ConfigError, apiUrl, currentRuntimeConfig, loadRuntimeConfig } from '../runtime-config';
import { consumeSse } from '../sse';
import { FRONTEND_VERSION, VersionError, compareSemver } from '../version';

export const workspaceMixin = {
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

  projectDisplayPercent() {
    if (
      this.projectTransferPercent > 0 &&
      this.projectTransferPercent < 100
    ) {
      return this.projectTransferPercent;
    }
    return Math.max(0, Math.min(100, Number(this.projectPercent || 0)));
  },

  projectBarStyle() {
    return `width: ${this.projectDisplayPercent()}%;`;
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
      this.filterSessions();
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
    const next = workspaceId || '';
    // 会话的工作区绑定不可改：切换项目后必须新建会话，否则 send()
    // 会复用旧 thread，仍按旧工作区读取文件。
    // 用 _boundProjectId 而不是 projectId：x-model 可能在 @change 前
    // 已经改写了 projectId。
    const changed =
      Boolean(this.threadId) && this._boundProjectId !== next;
    this.projectId = next;
    const project = this.selectedProject();
    if (project) {
      const label = project.root_path ||
        project.display_name || project.workspace_id;
      this.workspace = label;
      this.workspaceInput = label;
      this.rememberWorkspace();
    }
    if (changed) {
      this.threadId = '';
      this._boundProjectId = '';
      this.resetConversation();
      this.toastMessage('已切换项目；发送时会新建会话');
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
      const pct = data.progress;
      if (typeof pct === 'number') {
        this.projectPercent = Math.max(0, Math.min(100, pct));
      }
      const phase = data.progress_phase || data.status;
      const done = Number(data.processed_bytes || 0);
      const total = Number(data.total_bytes || 0);
      this.projectProgressText =
        `${phase}` +
        (total
          ? ` · ${this.formatBytes(done)} / ${this.formatBytes(total)}`
          : '') +
        (typeof pct === 'number' ? ` · ${pct}%` : '');
      if (['active', 'failed', 'cancelled'].includes(data.status)) {
        this.projectPercent = data.status === 'active' ? 100 : 0;
        this.projectTransferPercent = 100;
        this.projectProgressText =
          data.status === 'active'
            ? `完成 · ${this.formatBytes(data.bytes || 0)}`
            : `${data.status}${data.error ? ' · ' + data.error : ''}`;
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

  async registerHostWorkspace() {
    this.projectBusy = true;
    this.projectMessage = '正在请求本机选择工作区目录…';
    try {
      const created = await readJson(
        await authFetch('/api/workspaces/host', { method: 'POST' }),
        '注册本机工作区失败',
      );
      await this.loadProjects();
      await this.selectProject(created.workspace_id);
      this.projectMessage =
        `已添加本机工作区：${created.display_name || created.workspace_id}`;
      this.toastMessage(this.projectMessage);
    } catch (err) {
      const message =
        err instanceof Error ? err.message : String(err);
      if (message.includes('取消选择') || message.includes('CANCELLED')) {
        this.projectMessage = '已取消添加本机工作区';
        return;
      }
      this.projectMessage = message;
      this.toastMessage(this.projectMessage);
    } finally {
      this.projectBusy = false;
    }
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
      this.projectPercent = 0;
      this.projectTransferPercent = 0;
      this.projectProgressText = '';
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
      this.projectPercent = 0;
      this.projectTransferPercent = 0;
      this.projectProgressText = '';
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
      const job: any = await new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open(
          'POST',
          apiUrl(
            `/api/workspaces/${encodeURIComponent(created.workspace_id)}/uploads`,
          ),
        );
        const headers = new Headers();
        applyAuthHeaders(headers, 'POST');
        headers.set('Idempotency-Key', newIdempotencyKey());
        headers.forEach((value, key) => xhr.setRequestHeader(key, value));
        xhr.withCredentials =
          currentRuntimeConfig()?.authMode === 'bff-cookie';
        xhr.upload.onprogress = (event) => {
          if (!event.lengthComputable) return;
          this.projectTransferPercent = Math.max(
            0,
            Math.min(100, Math.round((event.loaded / event.total) * 100)),
          );
          this.projectMessage =
            `上传中 ${this.projectTransferPercent}%`;
        };
        xhr.onload = () => {
          let payload: any = {};
          try {
            payload = JSON.parse(xhr.responseText || '{}');
          } catch (err) {
            payload = {};
          }
          if (xhr.status >= 400) {
            const detail = payload.detail;
            reject(new Error(
              typeof detail === 'string'
                ? detail
                : JSON.stringify(detail || payload),
            ));
            return;
          }
          resolve(payload);
        };
        xhr.onerror = () => reject(new Error('上传网络错误'));
        xhr.send(form);
      });
      this.projectTransferPercent = 100;
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

};
