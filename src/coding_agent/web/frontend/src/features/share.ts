/* Alpine mixin: 只读会话分享链接。 */

import { authFetch, readJson } from '../api/http';

export const shareMixin = {
  async openShare(id) {
    const threadId = String(id || this.threadId || '');
    if (!threadId) {
      this.toastMessage(this.t('share.needSession'));
      return;
    }
    this.showShare = true;
    this.shareThreadId = threadId;
    this.shareUrl = '';
    this.shareError = '';
    this.shareDays = 7;
    await this.loadShares();
  },

  closeShare() {
    this.showShare = false;
  },

  async loadShares() {
    if (!this.shareThreadId) return;
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(this.shareThreadId)}/shares`,
      );
      const data = await readJson(response, '读取分享链接失败');
      this.shares = (data.shares || []).map((item) => ({
        ...item,
        _created: String(item.created_at || '').slice(0, 16).replace('T', ' '),
        _expires: String(item.expires_at || '').slice(0, 10),
        _views: this.t('share.views', { count: Number(item.views || 0) }),
      }));
    } catch (err) {
      this.shareError = String(err);
    }
  },

  async createShare() {
    if (this.shareBusy) return;
    this.shareBusy = true;
    this.shareError = '';
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(this.shareThreadId)}/shares`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            expires_in_days: Math.max(1, Number(this.shareDays) || 7),
          }),
        },
      );
      const data = await readJson(response, '创建分享链接失败');
      const path = String(data.path || '');
      this.shareUrl = path
        ? `${window.location.origin}${path}`
        : '';
      this.toastMessage(this.t('share.created'));
      await this.loadShares();
    } catch (err) {
      this.shareError = String(err);
    } finally {
      this.shareBusy = false;
    }
  },

  onShareAction(op, id) {
    if (op === 'revoke') void this.revokeShare(id);
  },

  async revokeShare(id) {
    if (!id) return;
    if (!window.confirm(this.t('share.revokeConfirm'))) return;
    try {
      const response = await authFetch(
        `/api/shares/${encodeURIComponent(id)}`,
        { method: 'DELETE' },
      );
      await readJson(response, '撤销分享链接失败');
      this.toastMessage(this.t('share.revoked'));
      await this.loadShares();
    } catch (err) {
      this.shareError = String(err);
    }
  },
};
