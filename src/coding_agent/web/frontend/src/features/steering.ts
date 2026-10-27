/* Alpine mixin: steering。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

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

export const steeringMixin = {
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

};
