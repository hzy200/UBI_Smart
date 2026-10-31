/* Alpine mixin: 输入草稿。
 *
 * 输入变化后 debounce 写 localStorage；切换会话时恢复。草稿是纯本地
 * 增强能力，任何存储异常都不能阻断发送。
 */

import {
  clearAllDrafts as clearStoredDrafts,
  clearDraft,
  draftText,
  saveDraft,
} from '../state/drafts';

const DRAFT_DEBOUNCE_MS = 350;

export const draftsMixin = {
  _draftTimer: null,
  _draftPending: null,

  onPromptInput(event) {
    // 不依赖 x-model 的监听顺序：直接读 DOM 当前值。
    const value = (event && event.target && 'value' in event.target)
      ? String(event.target.value || '')
      : String(this.prompt || '');
    if (this.prompt !== value) this.prompt = value;
    this.saveDraftSoon(this.threadId, value);
  },

  onSplitInput(event) {
    this.splitInput = (event && event.target && 'value' in event.target)
      ? String(event.target.value || '')
      : String(this.splitInput || '');
  },

  saveDraftSoon(threadId, text) {
    this._draftPending = {
      threadId: String(threadId || ''),
      value: String(text || ''),
    };
    if (this._draftTimer) clearTimeout(this._draftTimer);
    this._draftTimer = setTimeout(() => {
      this._draftTimer = null;
      const pending = this._draftPending;
      this._draftPending = null;
      if (pending) saveDraft(pending.threadId, pending.value);
    }, DRAFT_DEBOUNCE_MS);
  },

  flushDraft(threadId = this.threadId) {
    if (this._draftTimer) {
      clearTimeout(this._draftTimer);
      this._draftTimer = null;
    }
    const pending = this._draftPending;
    this._draftPending = null;
    if (pending) saveDraft(pending.threadId, pending.value);
    else if (this.prompt) saveDraft(threadId, this.prompt);
  },

  restoreDraft(threadId = this.threadId) {
    if (this.prompt) return;
    this.prompt = draftText(threadId);
  },

  clearDraftFor(threadId) {
    const key = String(threadId || '');
    if (this._draftPending && this._draftPending.threadId === key) {
      this._draftPending = null;
      if (this._draftTimer) clearTimeout(this._draftTimer);
      this._draftTimer = null;
    }
    clearDraft(key);
  },

  clearAllDrafts() {
    if (this._draftTimer) {
      clearTimeout(this._draftTimer);
      this._draftTimer = null;
    }
    this._draftPending = null;
    clearStoredDrafts();
  },
};
