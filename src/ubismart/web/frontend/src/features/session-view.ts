/* Alpine mixin: session view（阶段 1 兼容层）。
 *
 * 把会话运行态从 app.ts 搬到 activeView；旧模板继续用 this.messages 等，
 * 由 getter/setter 转发。applyEvent 暂时以“切换 activeView → 调用现有
 * handleEvent → 还原”的方式工作，保证行为不变；阶段 3 会改成显式上下文。
 */

import { createSessionView } from '../state/session-view';
import {
  clearDerivedCaches,
  markdownCaches,
  messageHeights,
  renderTimers,
  tokenBuffers,
  toolMap,
} from '../state/runtime';

export const SESSION_VIEW_FIELDS = [
  'threadId',
  'historyHasMore',
  'historyLoading',
  'stateText',
  'costText',
  'usage',
  'busy',
  'paused',
  'reasoningBlocks',
  'lastSeq',
  'canReconnect',
  'connectionState',
  '_terminal',
  '_suspended',
  '_retryCount',
  '_retryTimer',
  '_lastPrompt',
  '_currentMessage',
  '_currentNode',
  'prompt',
  'mainTab',
  'mainTabFlags',
  'mainTabClasses',
  'editorTabs',
  'activeEditorId',
  'diffPath',
  'diffText',
  'diffDisplay',
  'diffTruncated',
  'diffExpanded',
  'diffRowsData',
  'splitDiffRowsData',
  'diffView',
  'diffViewFlags',
  'diffViewClasses',
  'fileView',
  'fileLineInput',
  'pdfSearchQuery',
  'fileLoading',
  'messages',
  'virtualStart',
  'virtualEnd',
  'virtualTop',
  'virtualBottom',
  '_virtualRaf',
  'messageSearchOpen',
  'messageQuery',
  'messageMatches',
  'messageMatchIndex',
  'showJumpLatest',
  'plan',
  'kinds',
  'planRows',
  'timeline',
  'stepIdx',
  'planDegraded',
  'tools',
  'toolFilter',
  'toolFilterClasses',
  'toolQuery',
  '_toolsVersion',
  'files',
  'checkpoints',
  'filesDirty',
  'tasks',
  'tasksDirty',
  'uploads',
  'uploadQuota',
  'approvals',
  'approval',
  'approvalPosition',
  'decisions',
  'problems',
  'problemExpanded',
];

const MAX_SESSION_VIEWS = 6;

const mixin: Record<string, any> = {
  ensureSessionView(threadId = '', fresh = false) {
    const key = String(threadId || '');
    if (fresh || !this.sessionViews[key]) {
      this.sessionViews = {
        ...this.sessionViews,
        [key]: createSessionView(key),
      };
    }
    const view = this.sessionViews[key];
    view.lastActiveAt = Date.now();
    this.pruneSessionViews(key);
    return view;
  },

  activateSessionView(threadId = '', fresh = false) {
    const view = this.ensureSessionView(threadId, fresh);
    this.activeView = view;
    if (typeof this.restoreDraft === 'function') {
      this.restoreDraft(view.threadId);
    }
    this.pruneSessionViews(String(threadId || ''));
    return view;
  },

  createSessionView(threadId = '') {
    return createSessionView(String(threadId || ''));
  },

  disposeSessionView(threadId = '') {
    const key = String(threadId || '');
    const active = this.activeView && this.activeView.threadId === key;
    const view = this.sessionViews[key];
    if (view && view._abort) view._abort.abort();
    if (view && view._retryTimer) clearTimeout(view._retryTimer);
    const next = { ...this.sessionViews };
    delete next[key];
    this.sessionViews = next;
    if (active) this.activateSessionView('', true);
  },

  resetSessionViews() {
    const views = Object.values(this.sessionViews) as any[];
    for (const view of views) {
      if (view && view._abort) view._abort.abort();
      if (view && view._retryTimer) clearTimeout(view._retryTimer);
    }
    if (this.activeView && this.activeView._abort) {
      this.activeView._abort.abort();
    }
    renderTimers.forEach((timer) => clearTimeout(timer));
    renderTimers.clear();
    tokenBuffers.clear();
    markdownCaches.clear();
    messageHeights.clear();
    toolMap.clear();
    clearDerivedCaches();
    this.sessionViews = {};
    this.activeView = createSessionView('');
    this.splitView = null;
    this.splitOpen = false;
    this.splitSessionId = '';
    this.splitTitle = '';
    this.splitInput = '';
    this.splitError = '';
    if (typeof this.syncUrlState === 'function') this.syncUrlState();
  },

  pruneSessionViews(keepId = '') {
    const keys = Object.keys(this.sessionViews);
    if (keys.length <= MAX_SESSION_VIEWS) return;
    const removable = keys
      .filter((key) => {
        if (key === keepId) return false;
        if (this.activeView && key === this.activeView.threadId) return false;
        if (this.splitView && key === this.splitView.threadId) return false;
        const view = this.sessionViews[key];
        // 后台仍在跑流的视图不能被淘汰，否则事件会写进孤立对象。
        return !view.busy && !view._retryTimer;
      })
      .sort((a, b) => (
        Number(this.sessionViews[a].lastActiveAt || 0)
        - Number(this.sessionViews[b].lastActiveAt || 0)
      ));
    const next = { ...this.sessionViews };
    while (
      Object.keys(next).length > MAX_SESSION_VIEWS
      && removable.length
    ) {
      delete next[removable.shift()];
    }
    this.sessionViews = next;
  },

  withView(view, fn) {
    const previous = this.activeView;
    const previousBackground = this._isBackgroundApply;
    this.activeView = view;
    this._isBackgroundApply = previous !== view;
    try {
      return fn();
    } finally {
      this.activeView = previous;
      this._isBackgroundApply = previousBackground;
    }
  },

  applyEvent(view, event) {
    if (!view || !event) return;
    this.withView(view, () => this.handleEvent(event));
  },
};

for (const field of SESSION_VIEW_FIELDS) {
  Object.defineProperty(mixin, field, {
    configurable: true,
    enumerable: true,
    get() {
      return this.activeView ? this.activeView[field] : undefined;
    },
    set(value) {
      if (this.activeView) this.activeView[field] = value;
    },
  });
}

export const sessionViewMixin = mixin;
