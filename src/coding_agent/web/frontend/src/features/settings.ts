/* Alpine mixin: settings。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import { accentColor, themeBodyClasses } from '../ui/theme';

const SETTINGS_KEY = 'codingagent.settings';

export const settingsMixin = {
  loadSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
      if (saved.workspace) this.workspaceInput = saved.workspace;
      this.applyRemoteSettings(saved);
    } catch (e) {
      this.setLocale('zh-CN');
    }
  },

  applyRemoteSettings(saved) {
    if (!saved || typeof saved !== 'object') return;
    if (saved.theme) this.theme = saved.theme;
    if (saved.density) this.density = saved.density;
    if (saved.fontSize) this.fontSize = saved.fontSize;
    if (saved.fontFamily) this.fontFamily = saved.fontFamily;
    if (saved.contrast) this.contrast = saved.contrast;
    if (saved.accent) this.accent = saved.accent;
    if (saved.codeFontSize) this.codeFontSize = saved.codeFontSize;
    if (saved.codeLineHeight) this.codeLineHeight = saved.codeLineHeight;
    if (typeof saved.showReasoning === 'boolean') {
      this.showReasoning = saved.showReasoning;
    }
    if (typeof saved.notify === 'boolean') {
      this.notifyEnabled = saved.notify;
    }
    if (typeof saved.errorReporting === 'boolean') {
      this.errorReportingEnabled = saved.errorReporting;
    }
    this.setLocale(saved.locale || 'zh-CN');
    this.applyBodyClasses();
  },

  changeLocale(event) {
    const value = event && event.target ? event.target.value : 'zh-CN';
    this.setLocale(value);
    this.saveSettings();
  },

  toggleErrorReporting() {
    this.errorReportingEnabled = !this.errorReportingEnabled;
    this.saveSettings();
    this.toastMessage(
      this.t(
        this.errorReportingEnabled
          ? 'settings.errorReportingOn'
          : 'settings.errorReportingOff',
      ),
    );
  },

  saveSettings() {
    const payload = {
      theme: this.theme,
      density: this.density,
      fontSize: this.fontSize,
      fontFamily: this.fontFamily,
      contrast: this.contrast,
      accent: this.accent,
      codeFontSize: this.codeFontSize,
      codeLineHeight: this.codeLineHeight,
      showReasoning: this.showReasoning,
      workspace: this.workspaceInput,
      notify: this.notifyEnabled,
      errorReporting: this.errorReportingEnabled,
      locale: this.locale,
    };
    localStorage.setItem(SETTINGS_KEY, JSON.stringify(payload));
    this.broadcastSync('settings', { settings: payload });
    this.applyBodyClasses();
  },

  rememberWorkspace() {
    try {
      const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
      saved.workspace = this.workspaceInput;
      localStorage.setItem(SETTINGS_KEY, JSON.stringify(saved));
    } catch (e) { /* ignore */ }
  },

  applyBodyClasses() {
    this.bodyClasses = themeBodyClasses(this);
    if (typeof document !== 'undefined' && document.body) {
      document.body.style.setProperty('--accent', accentColor(this.accent));
    }
  },

  refreshBadgeClass() {
    this.badgeClasses = this.capabilities.read_only
      ? 'badge-read'
      : 'badge-write';
  },

  bodyClass() {
    return themeBodyClasses(this);
  },

};
