/* Alpine mixin: steering。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
  readJson,
} from '../api/http';

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
    const view = this.activeView;
    if (!view.threadId) return;
    view.busy = true;
    view.stateText = this.t('status.resuming');
    view.canReconnect = false;
    view.connectionState = 'streaming';
    view._terminal = false;
    view._suspended = false;
    view._retryCount = 0;
    const controller = new AbortController();
    view._abort = controller;
    try {
      const response = await authFetch(
        `/api/resume_steering/${encodeURIComponent(view.threadId)}`,
        { method: 'POST', signal: controller.signal },
      );
      if (await this.handleRunConflict(response, view)) return;
      await this.consume(response, view);
    } catch (err) {
      if (!controller.signal.aborted) this.failRun(err, view);
    } finally {
      if (view._abort === controller) view._abort = null;
      await this.afterStream(view);
    }
  },

};
