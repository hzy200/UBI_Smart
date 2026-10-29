/* Alpine mixin: 同一会话两次运行的 diff / 用量并排对比。 */

import { authFetch, readJson } from '../api/http';

export const compareMixin = {
  async openCompare(id) {
    const threadId = String(id || this.threadId || '');
    if (!threadId) {
      this.toastMessage(this.t('compare.needSession'));
      return;
    }
    this.compareOpen = true;
    this.compareThreadId = threadId;
    this.compareRuns = [];
    this.compareResult = null;
    this.compareError = '';
    await this.loadCompareRuns();
  },

  closeCompare() {
    this.compareOpen = false;
  },

  async loadCompareRuns() {
    if (!this.compareThreadId) return;
    this.compareLoading = true;
    this.compareError = '';
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(this.compareThreadId)}/runs?limit=20`,
      );
      const data = await readJson(response, '读取运行记录失败');
      this.compareRuns = (data.runs || []).map((run) => ({
        ...run,
        _label: this.compareRunLabel(run),
      }));
      if (this.compareRuns.length >= 2) {
        this.compareLeft = this.compareRuns[this.compareRuns.length - 2].index;
        this.compareRight = this.compareRuns[this.compareRuns.length - 1].index;
      } else if (this.compareRuns.length === 1) {
        this.compareLeft = this.compareRuns[0].index;
        this.compareRight = this.compareRuns[0].index;
      } else {
        this.compareLeft = 0;
        this.compareRight = 0;
      }
    } catch (err) {
      this.compareError = String(err);
    } finally {
      this.compareLoading = false;
    }
  },

  compareRunLabel(run) {
    if (!run) return '';
    const when = String(run.started_at || '')
      .replace('T', ' ')
      .slice(0, 16);
    const status = run.status === 'ok'
      ? this.t('compare.ok')
      : run.status === 'failed'
        ? this.t('compare.failed')
        : this.t('compare.interrupted');
    return `#${run.index} · ${when} · ${status} · ${Number(run.total_tokens || 0)} tokens`;
  },

  async runCompare() {
    const left = Number(this.compareLeft || 0);
    const right = Number(this.compareRight || 0);
    if (!this.compareThreadId || !left || !right) return;
    this.compareLoading = true;
    this.compareError = '';
    try {
      const response = await authFetch(
        `/api/sessions/${encodeURIComponent(this.compareThreadId)}`
        + `/runs/compare?left=${left}&right=${right}&max_files=30`,
      );
      const data = await readJson(response, '运行对比失败');
      const noDiff = this.t('compare.noDiff');
      const untouched = this.t('compare.notTouched');
      data._leftLabel = this.compareRunLabel(data.left);
      data._rightLabel = this.compareRunLabel(data.right);
      data.files = (data.files || []).map((file) => ({
        ...file,
        _left: file.left
          ? (file.left.diff || noDiff)
          : untouched,
        _right: file.right
          ? (file.right.diff || noDiff)
          : untouched,
      }));
      this.compareResult = data;
    } catch (err) {
      this.compareResult = null;
      this.compareError = String(err);
    } finally {
      this.compareLoading = false;
    }
  },

  formatCompareCost(value) {
    return `$${Number(value || 0).toFixed(4)}`;
  },
};
