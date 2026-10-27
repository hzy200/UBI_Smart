/* Alpine mixin: desktop。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import { shouldSystemNotify } from '../ui/notifications';

export const desktopMixin = {
  async collectDiagnostics() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge || this.diagnosticsBusy) return;
    this.diagnosticsBusy = true;
    try {
      const result = await bridge.collect_diagnostics();
      if (result.ok) {
        this.diagnosticsPath = result.path;
        this.toastMessage('诊断包已生成');
      } else {
        this.toastMessage(result.error);
      }
    } catch (err) {
      this.toastMessage(String(err));
    } finally {
      this.diagnosticsBusy = false;
    }
  },

  async revealDiagnostics() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge || !this.diagnosticsPath) return;
    const result = await bridge.reveal_diagnostics(this.diagnosticsPath);
    if (!result.ok) this.toastMessage(result.error);
  },

  async checkUpdate() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge) return;
    try {
      const data = await bridge.check_update();
      this.updateInfo = data;
      if (!data.configured) {
        this.updateText = data.message || '未配置更新清单';
      } else if (!data.ok) {
        this.updateText = `检查失败：${data.error}`;
      } else if (data.available) {
        this.updateText = `可更新：${data.current} → ${data.latest}`;
      } else {
        this.updateText = `已是最新版本 ${data.current}`;
      }
    } catch (err) {
      this.updateText = String(err);
    }
  },

  async openUpdate() {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge || !this.updateInfo || !this.updateInfo.download_url) return;
    const result = await bridge.open_url(this.updateInfo.download_url);
    if (!result.ok) this.toastMessage(result.error);
  },

  async toggleNotify() {
    this.notifyEnabled = !this.notifyEnabled;
    if (this.notifyEnabled && 'Notification' in window) {
      try {
        const permission = await Notification.requestPermission();
        if (permission !== 'granted') {
          this.notifyEnabled = false;
          this.toastMessage('浏览器未授予通知权限');
        }
      } catch (err) {
        this.notifyEnabled = false;
      }
    }
    this.saveSettings();
  },

  notify(title, body, meta: any = {}) {
    const kind = title.includes('失败')
      ? 'error'
      : title.includes('审批')
        ? 'warning'
        : title.includes('完成')
          ? 'success'
          : 'info';
    this.pushNotification(kind, title, body, meta);
    const supported = 'Notification' in window;
    if (!shouldSystemNotify({
      enabled: Boolean(this.notifyEnabled),
      hidden: this.sessionHidden(),
      supported,
      permission: supported ? Notification.permission : 'default',
    })) return;
    try {
      new Notification(title, { body: String(body || '').slice(0, 120) });
    } catch (err) { /* ignore */ }
  },

};
