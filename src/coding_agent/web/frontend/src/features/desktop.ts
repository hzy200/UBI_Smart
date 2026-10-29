/* Alpine mixin: desktop。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import { shouldSystemNotify } from '../ui/notifications';

function desktopBridge() {
  return window.pywebview && window.pywebview.api;
}

export const desktopMixin = {
  get desktopRestored() {
    return !this.desktopMaximized;
  },

  applyDesktopShell() {
    if (typeof document === 'undefined' || !document.body) return;
    document.body.classList.toggle('desktop-shell', Boolean(this.desktopAvailable));
  },

  async syncDesktopWindowState() {
    const bridge = desktopBridge();
    if (!bridge) return;
    try {
      const state = await bridge.window_state();
      if (state && state.ok) this.desktopMaximized = Boolean(state.maximized);
    } catch (err) { /* 桥接不可用时保持默认状态 */ }
  },

  async minimizeDesktop() {
    const bridge = desktopBridge();
    if (!bridge) return;
    try {
      await bridge.window_minimize();
    } catch (err) { /* ignore */ }
  },

  async toggleDesktopMaximize() {
    const bridge = desktopBridge();
    if (!bridge) return;
    try {
      const state = await bridge.window_toggle_maximize();
      if (state && state.ok) this.desktopMaximized = Boolean(state.maximized);
    } catch (err) { /* ignore */ }
  },

  async closeDesktop() {
    const bridge = desktopBridge();
    if (!bridge) return;
    try {
      await bridge.window_close();
    } catch (err) { /* ignore */ }
  },

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
        } else {
          // 用户刚授权：立即建立 Web Push 订阅。
          void this.setupPush();
        }
      } catch (err) {
        this.notifyEnabled = false;
      }
    }
    this.saveSettings();
  },

  notify(title, body = '', meta: any = {}, kind = '') {
    // 标题可能已被本地化，不能再用中文字符串推断类型。
    const resolvedKind = kind || (
      title.includes('失败')
        ? 'error'
        : title.includes('审批')
          ? 'warning'
          : title.includes('完成')
            ? 'success'
            : 'info'
    );
    this.pushNotification(resolvedKind, title, body, meta);
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
