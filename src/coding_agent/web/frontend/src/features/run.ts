/* Alpine mixin: run。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
} from '../api/http';
import {
  renderTimers,
  tokenBuffers,
  toolMap,
} from '../state/runtime';
import {
  currentRuntimeConfig,
} from '../runtime-config';
import {
  consumeSse,
} from '../sse';

export const runMixin = {
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
    toolMap.clear();
    renderTimers.forEach((timer) => clearTimeout(timer));
    renderTimers.clear();
    tokenBuffers.clear();
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

  async consume(response) {
    this.connectionState = 'streaming';
    await consumeSse(
      response,
      ({ event, seq }) => {
        if (Number.isFinite(seq)) this.lastSeq = Math.max(this.lastSeq, seq);
        this.applyEvent(this.activeView, event);
      },
      {
        getLastSeq: () => this.lastSeq,
        idleTimeoutMs: currentRuntimeConfig()?.sse.idleTimeoutMs,
      },
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
    const nested = detail && typeof detail === 'object' ? detail : {};
    const code = payload.code || nested.code || '';
    if (code === 'run_in_progress') {
      this.toastMessage('该会话已在运行，正在连接事件流…');
      await this.reconnect();
      return true;
    }
    const message = typeof detail === 'string' && detail
      ? detail
      : JSON.stringify(payload.details || nested || payload);
    throw new Error(
      `${message}（${code || 'run.conflict'}` +
      `${payload.request_id ? ` · request_id=${payload.request_id}` : ''}）`,
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

};
