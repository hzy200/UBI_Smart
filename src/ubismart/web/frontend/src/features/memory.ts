/* Alpine mixin: memory。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
  readJson,
} from '../api/http';

export const memoryMixin = {
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

};
