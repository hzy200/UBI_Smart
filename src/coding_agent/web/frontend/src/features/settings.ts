/* Alpine mixin: settings。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */


const SETTINGS_KEY = 'codingagent.settings';

export const settingsMixin = {
  loadSettings() {
    try {
      const saved = JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}');
      if (saved.theme) this.theme = saved.theme;
      if (saved.density) this.density = saved.density;
      if (saved.fontSize) this.fontSize = saved.fontSize;
      if (saved.fontFamily) this.fontFamily = saved.fontFamily;
      if (typeof saved.showReasoning === 'boolean') {
        this.showReasoning = saved.showReasoning;
      }
      if (saved.workspace) this.workspaceInput = saved.workspace;
      if (saved.notify) this.notifyEnabled = true;
    } catch (e) { /* ignore */ }
  },

  saveSettings() {
    localStorage.setItem(SETTINGS_KEY, JSON.stringify({
      theme: this.theme,
      density: this.density,
      fontSize: this.fontSize,
      fontFamily: this.fontFamily,
      showReasoning: this.showReasoning,
      workspace: this.workspaceInput,
      notify: this.notifyEnabled,
    }));
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
    this.bodyClasses = `theme-${this.theme} density-${this.density} font-${this.fontSize} family-${this.fontFamily}`;
  },

  refreshBadgeClass() {
    this.badgeClasses = this.capabilities.read_only
      ? 'badge badge-read'
      : 'badge badge-write';
  },

  bodyClass() {
    return `theme-${this.theme} density-${this.density} font-${this.fontSize} family-${this.fontFamily}`;
  },

};
