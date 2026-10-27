/* Alpine mixin: run。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
} from '../api/http';
import {
  toolMap,
} from '../state/runtime';
import {
  currentRuntimeConfig,
} from '../runtime-config';
import {
  consumeSse,
} from '../sse';

export const runMixin = {
  clearViewToolEntries(view) {
    const prefix = `${(view && view.threadId) || '-'}::`;
    for (const key of Array.from(toolMap.keys())) {
      if (key.startsWith(prefix)) toolMap.delete(key);
    }
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
    // ensureSession 可能新建并切换视图，之后再绑定本次流。
    const view = this.activeView;
    view.prompt = '';
    if (appendUser) this.pushMessage('user', text);
    view._lastPrompt = text;
    view.busy = true;
    view.paused = false;
    view.stateText = '运行中…';
    view.connectionState = 'streaming';
    view._terminal = false;
    view._suspended = false;
    view._retryCount = 0;
    view.reasoningBlocks = [];
    view.plan = [];
    view.kinds = [];
    view.planRows = [];
    view.stepIdx = -1;
    view.planDegraded = false;
    view.tools = [];
    view._toolsVersion += 1;
    this.clearViewToolEntries(view);
    this.diffText = '';
    view.lastSeq = 0;
    view.canReconnect = false;
    view._currentMessage = null;
    view._currentNode = '';
    const controller = new AbortController();
    view._abort = controller;
    try {
      const response = await authFetch('/api/run', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompt: text, thread_id: view.threadId }),
        signal: controller.signal,
      });
      if (await this.handleRunConflict(response, view)) return;
      await this.consume(response, view);
    } catch (err) {
      if (!controller.signal.aborted) this.failRun(err, view);
    } finally {
      if (view._abort === controller) view._abort = null;
      await this.afterStream(view);
    }
  },

  async consume(response, view = this.activeView) {
    view.connectionState = 'streaming';
    await consumeSse(
      response,
      ({ event, seq }) => {
        if (Number.isFinite(seq)) {
          view.lastSeq = Math.max(view.lastSeq, seq);
        }
        // 事件始终落到发起该流的视图，而不是当前激活视图。
        this.applyEvent(view, event);
      },
      {
        getLastSeq: () => view.lastSeq,
        idleTimeoutMs: currentRuntimeConfig()?.sse.idleTimeoutMs,
      },
    );
  },

  async handleRunConflict(response, view = this.activeView) {
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
      if (this.activeView === view) {
        this.toastMessage('该会话已在运行，正在连接事件流…');
      }
      await this.reconnect(view);
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

  async reconnect(view = this.activeView) {
    if (!view || !view.threadId) return;
    if (view._retryTimer) {
      clearTimeout(view._retryTimer);
      view._retryTimer = null;
    }
    view.busy = true;
    view.stateText = '重连中…';
    view.connectionState = 'reconnecting';
    view.canReconnect = false;
    const controller = new AbortController();
    view._abort = controller;
    try {
      const response = await authFetch(
        `/api/stream/${encodeURIComponent(view.threadId)}?after=${view.lastSeq}`,
        { signal: controller.signal },
      );
      await this.consume(response, view);
    } catch (err) {
      if (!controller.signal.aborted) {
        if (this.activeView === view) {
          this.toastMessage(`重连失败：${err}`);
        }
        view.canReconnect = true;
      }
    } finally {
      if (view._abort === controller) view._abort = null;
      await this.afterStream(view);
    }
  },

  scheduleReconnect(view = this.activeView) {
    if (!view || view._retryTimer) return;
    view._retryCount += 1;
    const delay = Math.min(8000, 500 * 2 ** view._retryCount);
    view.stateText = `连接中断，${Math.round(delay / 1000)}s 后重连…`;
    view.connectionState = 'reconnecting';
    view._retryTimer = setTimeout(() => {
      view._retryTimer = null;
      if (view.threadId && this.sessionViews[view.threadId] !== view) {
        return;
      }
      void this.reconnect(view);
    }, delay);
  },

  retry() {
    if (this.busy || !this._lastPrompt) return;
    this.canReconnect = false;
    this.connectionState = 'idle';
    void this.send(this._lastPrompt, false);
  },

  connectionText() {
    if (this.connectionState === 'streaming') return '流式连接中';
    if (this.connectionState === 'reconnecting') return '重连中…';
    if (this.connectionState === 'closed') return '连接已关闭';
    return '';
  },

  async afterStream(view = this.activeView) {
    this.finishAssistant(view);
    if (
      view.canReconnect &&
      !view._terminal &&
      !view._suspended &&
      view._retryCount < 3
    ) {
      this.scheduleReconnect(view);
      return;
    }
    view.connectionState = view._terminal ? 'idle' : 'closed';
    if (view.stateText === '连接中断') {
      view.busy = false;
      return;
    }
    if (view.approvals.length) {
      view.stateText = '等待审批';
      view.approval = view.approvals[0] || null;
      view.approvalPosition = view.approvals.length > 1
        ? `还剩 ${view.approvals.length} 条待确认`
        : '';
      view.busy = false;
      return;
    }
    if (view.paused) {
      view.stateText = '已暂停';
      view.busy = false;
      return;
    }
    view.busy = false;
    if (view.stateText === '运行中…') view.stateText = '就绪';
    view.canReconnect = false;
    await this.loadSessions();
  },

  failRun(err, view = this.activeView) {
    view.busy = false;
    this.finishAssistant(view);
    view.stateText = '连接中断';
    view.connectionState = 'closed';
    view.canReconnect = Boolean(view.threadId && view.lastSeq > 0);
    this.withView(view, () => {
      this.pushMessage(
        'system',
        `运行失败：${err && err.message ? err.message : err}`,
      );
    });
  },

};
