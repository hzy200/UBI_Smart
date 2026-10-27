/* Alpine mixin: view-model。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  escapeHtml,
  formatDuration,
  formatTokens,
  renderMarkdown,
} from '../ui/format';

export const view_modelMixin = {
  tabClass(name) { return this.tab === name ? 'active' : ''; },
  sessionClass(id) { return id === this.threadId ? 'active' : ''; },
  sessionTitle(session) { return session.title || session.thread_id; },
  sessionMeta(session) {
    return `${session.thread_id} · ${session.last_active || ''}`;
  },
  toolClass(tool) { return `tool-${tool.state || 'running'}`; },
  taskClass(task) { return `task-${task.status || 'pending'}`; },
  auditText(record) { return record.tool || record.detail || ''; },
  loginMessage() {
    if (this.authMode === 'bff-cookie') {
      return this.loginError || '会话已失效，请重新登录。';
    }
    return this.loginError || '输入访问 token（桌面端会自动填入）';
  },
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
  setTab(name, open = true) {
    this.tab = name;
    this.tabClasses = {
      plan: name === 'plan' ? 'active' : '',
      tools: name === 'tools' ? 'active' : '',
    };
    if (open && this.inspectorCollapsed) this.inspectorCollapsed = false;
  },
  toggleSettings() {
    this.showSettings = !this.showSettings;
    if (this.showSettings) {
      this.probeProject();
      this.loadProfiles();
    }
  },
  openMemory() { this.setView('memory'); },
  openAudit() { this.setView('audit'); },
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

};
