/* Alpine mixin: 窄屏三屏模式（会话 / 对话 / 审批）。 */

export const mobileMixin = {
  isMobileLayout() {
    try {
      return window.matchMedia('(max-width: 760px)').matches;
    } catch (err) {
      return false;
    }
  },

  setMobilePane(pane) {
    const next = ['sessions', 'chat', 'approvals'].includes(pane)
      ? pane
      : 'chat';
    this.mobilePane = next === 'approvals' ? 'chat' : next;
    this.mobilePaneClasses = {
      sessions: next === 'sessions' ? 'active' : '',
      chat: next === 'chat' ? 'active' : '',
      approvals: next === 'approvals' ? 'active' : '',
    };
    if (next === 'sessions') {
      this.setView('sessions');
      this.sidebarVisible = true;
      return;
    }
    this.sidebarVisible = false;
    if (next === 'chat') {
      this.showChat();
      return;
    }
    // 审批可能挂在后台会话视图上；优先切到第一个有待审批的会话。
    const views: any[] = Object.values(this.sessionViews || {});
    const pending = views.find(
      (view) => view && view.approvals && view.approvals.length,
    );
    if (pending) {
      if (pending.threadId && pending.threadId !== this.threadId) {
        this.activateSessionView(pending.threadId);
      }
      this.showNextApproval(pending);
      return;
    }
    const view = this.activeView;
    if (view && view.approvals.length) {
      this.showNextApproval(view);
    } else {
      this.toastMessage(this.t('mobile.noApprovals'));
    }
  },

  get mobilePaneClass() {
    return `mobile-pane-${this.mobilePane}`;
  },

  get unreadSessionCount() {
    return this.sessions.filter((row) => row.unread).length;
  },

  get mobileApprovalCount() {
    const views: any[] = Object.values(this.sessionViews || {});
    return views.reduce(
      (sum, view) => sum + ((view && view.approvals)
        ? view.approvals.length
        : 0),
      0,
    );
  },
};
