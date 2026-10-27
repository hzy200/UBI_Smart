/* Alpine mixin: approvals。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
} from '../api/http';

export const approvalsMixin = {
  showNextApproval(view = this.activeView) {
    view.approval = view.approvals[0] || null;
    view.approvalPosition = view.approvals.length > 1
      ? this.t('approval.remaining', { count: view.approvals.length })
      : '';
    if (view === this.activeView && view.approval) {
      // 首次打开才记录返回焦点；后续审批只移动焦点，避免把返回目标
      // 记成即将被移除的按钮。用真实可见性判断，避免旧的 approval
      // 状态让首次打开被误判为“已打开”。
      const modal = document.querySelector('.approval-modal') as HTMLElement | null;
      const modalOpen = Boolean(
        modal && !modal.hasAttribute('x-cloak') && modal.style.display !== 'none'
      );
      this.$nextTick(() => this.focusModal('.approval-modal', !modalOpen));
    }
  },

  async decide(granted) {
    const view = this.activeView;
    if (!view.approval) return;
    view.decisions[view.approval.request_id] = !!granted;
    view.approvals.shift();
    if (view.approvals.length) {
      this.showNextApproval(view);
      return;
    }
    await this.resumeApprovals(view);
  },

  async decideAll(granted) {
    const view = this.activeView;
    for (const item of view.approvals) {
      view.decisions[item.request_id] = !!granted;
    }
    view.approvals = [];
    await this.resumeApprovals(view);
  },

  async resumeApprovals(view = this.activeView) {
    view.approval = null;
    if (view === this.activeView) {
      this.$nextTick(() => this.restoreModalFocus());
    }
    view.busy = true;
    view.stateText = this.t('status.resuming');
    // 序号跨 resume 连续；保留 lastSeq，避免重放历史事件。
    view.canReconnect = false;
    view.connectionState = 'streaming';
    view._terminal = false;
    view._suspended = false;
    view._retryCount = 0;
    const controller = new AbortController();
    view._abort = controller;
    try {
      const response = await authFetch(
        `/api/resume/${encodeURIComponent(view.threadId)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ decisions: view.decisions }),
          signal: controller.signal,
        },
      );
      view.decisions = {};
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
