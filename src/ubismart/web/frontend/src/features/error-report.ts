/* Alpine mixin: 前端错误采集与可选上报。
 *
 * 始终保留最近 20 条错误摘要供本地诊断；只有在设置里显式开启时才
 * POST 到 /api/client-errors，且同一消息一分钟内只发一次。
 * 不采集对话内容、请求体或 token。
 */

import { authFetch } from '../api/http';
import { normalizeClientError } from '../ui/error-report';
import { FRONTEND_VERSION } from '../version';

export const errorReportMixin = {
  initErrorReporting() {
    if (typeof window === 'undefined') return;
    window.addEventListener('error', (event) => {
      const detail = (event as ErrorEvent).error || event.message;
      this.captureClientError('error', detail);
    });
    window.addEventListener('unhandledrejection', (event) => {
      this.captureClientError(
        'unhandledrejection',
        (event as PromiseRejectionEvent).reason,
      );
    });
  },

  captureClientError(kind, error) {
    const path = typeof location !== 'undefined' ? location.pathname : '';
    const report = normalizeClientError(kind, error, path);
    if (!report) return;
    this.clientErrors = [report, ...this.clientErrors].slice(0, 20);
    if (!this.errorReportingEnabled) return;
    const now = Date.now();
    if (Object.keys(this._errorReportTimes).length > 100) {
      this._errorReportTimes = {};
    }
    const last = Number(this._errorReportTimes[report.message] || 0);
    if (now - last < 60_000) return;
    this._errorReportTimes[report.message] = now;
    void this.sendClientError(report);
  },

  async sendClientError(report) {
    try {
      await authFetch('/api/client-errors', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          ...report,
          version: FRONTEND_VERSION,
          locale: this.locale,
        }),
      });
    } catch (err) {
      // 上报失败不能递归上报。
    }
  },

  copyClientErrors() {
    this.copyText(JSON.stringify(this.clientErrors, null, 2));
  },
};
