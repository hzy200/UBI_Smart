/* Alpine mixin: errors。全局错误横幅与可重试动作。 */

export const errorsMixin = {
  reportError(message, retryKind = '', payload = '') {
    const text = String(message || '').replace(/\s+/g, ' ').trim();
    if (!text) return;
    this.uiError = text;
    this._errorRetryKind = retryKind || '';
    this._errorRetryPayload = payload || '';
  },

  dismissError() {
    this.uiError = '';
    this._errorRetryKind = '';
    this._errorRetryPayload = '';
  },

  retryLastError() {
    const kind = this._errorRetryKind;
    const payload = this._errorRetryPayload;
    this.dismissError();
    if (kind === 'sessions') {
      void this.loadSessions(true);
      return;
    }
    if (kind === 'tree') {
      void this.refreshTree();
      return;
    }
    if (kind === 'file' && payload) {
      void this.openFile(payload, 0, true);
    }
  },
};
