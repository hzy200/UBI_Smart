/* Alpine mixin: URL 深链。
 *
 * 只把“当前会话 + 分屏会话”同步到 query，tokens 永远不进 URL。
 * 使用 replaceState，避免每次切换会话都堆积历史；刷新/分享链接可恢复。
 */

import { buildSessionUrl, parseSessionUrl } from '../ui/url-state';

function currentUrl(): string {
  return `${window.location.pathname}${window.location.search}${window.location.hash || ''}`;
}

export const urlStateMixin = {
  syncUrlState() {
    if (typeof window === 'undefined' || !window.history?.replaceState) return;
    const split = this.splitOpen ? (this.splitSessionId || '') : '';
    const nextPath = buildSessionUrl(
      window.location.pathname,
      window.location.search,
      { session: this.threadId || '', split },
    );
    const next = `${nextPath}${window.location.hash || ''}`;
    if (next === currentUrl()) return;
    try {
      window.history.replaceState(null, '', next);
    } catch (err) {
      // 某些内嵌 WebView 禁用 history；深链是增强能力，失败不影响会话。
    }
  },

  dropUrlParam(name: string) {
    if (typeof window === 'undefined' || !window.history?.replaceState) return;
    const params = new URLSearchParams(window.location.search);
    params.delete(name);
    const query = params.toString();
    const next = `${window.location.pathname}${query ? `?${query}` : ''}` +
      `${window.location.hash || ''}`;
    try {
      window.history.replaceState(null, '', next);
    } catch (err) {
      // 同上。
    }
  },

  async applyUrlState() {
    if (typeof window === 'undefined') return;
    const state = parseSessionUrl(window.location.search);
    const known = (id: string) => (
      this.sessions.some((row) => row.thread_id === id)
    );

    if (state.session) {
      if (known(state.session)) {
        await this.openSession(state.session);
      } else {
        this.dropUrlParam('session');
        this.toastMessage('链接中的会话不存在或已删除');
      }
    }

    if (state.split && state.split !== this.threadId) {
      if (known(state.split)) {
        await this.openSplitSession(state.split);
      } else {
        this.dropUrlParam('split');
      }
    } else if (state.split) {
      this.dropUrlParam('split');
    }

    if (!this.threadId) this.restoreDraft('');
    this.syncUrlState();
  },
};
