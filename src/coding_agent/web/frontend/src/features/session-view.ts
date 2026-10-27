/* Alpine mixin: session view（阶段 1 兼容层）。
 *
 * 把会话运行态从 app.ts 搬到 activeView；旧模板继续用 this.messages 等，
 * 由 getter/setter 转发。applyEvent 暂时以“切换 activeView → 调用现有
 * handleEvent → 还原”的方式工作，保证行为不变；阶段 3 会改成显式上下文。
 */

import { createSessionView } from '../state/session-view';

export const SESSION_VIEW_FIELDS = [
  'threadId',
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

const mixin: Record<string, any> = {
  activateSessionView(threadId = '') {
    this.activeView = createSessionView(String(threadId || ''));
  },

  createSessionView(threadId = '') {
    return createSessionView(String(threadId || ''));
  },

  applyEvent(view, event) {
    if (!view || !event) return;
    const previous = this.activeView;
    this.activeView = view;
    try {
      this.handleEvent(event);
    } finally {
      this.activeView = previous;
    }
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
