/* Alpine mixin: approvals。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
} from '../api/http';

export const approvalsMixin = {
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

};
