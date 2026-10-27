/* Alpine mixin: recovery。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
  readJson,
} from '../api/http';

export const recoveryMixin = {
  async openRecovery() {
    this.showRecovery = true;
    await this.loadRecovery();
  },

  async loadRecovery() {
    try {
      const response = await authFetch('/api/recovery');
      const data = await readJson(response, '读取恢复中心失败');
      const labels = {
        approval: '待审批',
        paused: '已暂停',
        running: '运行中',
        interrupted: '异常中断',
      };
      this.recoveryItems = (data.items || []).map((item) => ({
        ...item,
        _key: item.thread_id,
        _text: `${item.title} · ${item.last_active || ''}`,
        _status: labels[item.status] || item.status,
        _approvals: Number(item.approvals || 0)
          ? `待审批 ${Number(item.approvals)}`
          : '',
        _cls: item.status === 'interrupted' ? 'note-bad' : '',
      }));
    } catch (err) {
      this.recoveryItems = [];
      this.toastMessage(String(err));
    }
  },

  closeRecovery() {
    this.showRecovery = false;
  },

  async recoveryResume(item) {
    this.showRecovery = false;
    await this.openSession(item.thread_id);
    if (item.status === 'paused' && !Number(item.approvals || 0)) {
      await this.resumeSteering();
    } else if (
      item.status === 'approval' || Number(item.approvals || 0)
    ) {
      await this.loadApprovals();
    }
  },

};
