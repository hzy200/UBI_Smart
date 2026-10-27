/* Alpine mixin: keyboard。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */


const SHORTCUTS = [
  { keys: 'Ctrl / Cmd + Shift + P', action: '打开命令面板' },
  { keys: 'Ctrl / Cmd + K', action: '聚焦会话搜索' },
  { keys: 'Ctrl / Cmd + Shift + F', action: '搜索当前对话' },
  { keys: 'Ctrl / Cmd + B', action: '切换侧栏' },
  { keys: 'Ctrl / Cmd + J', action: '切换右侧检查器' },
  { keys: 'Ctrl / Cmd + Enter', action: '发送任务' },
  { keys: 'Shift + Enter', action: '输入框内换行' },
  { keys: 'Esc', action: '关闭命令面板 / 设置 / 恢复中心 / 快捷键' },
  { keys: '?', action: '打开快捷键帮助' },
];

export const keyboardMixin = {
  toastMessage(text) {
    this.toast = text;
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => { this.toast = ''; }, 3200);
  },

  toggleReasoning() {
    this.showReasoning = !this.showReasoning;
    this.saveSettings();
  },

  shortcuts() {
    return SHORTCUTS;
  },

  onGlobalKeydown(event) {
    if (event.defaultPrevented) return;
    const target = event.target as HTMLElement | null;
    const typing = Boolean(target && (
      target.tagName === 'INPUT'
      || target.tagName === 'TEXTAREA'
      || target.isContentEditable
    ));
    const mod = event.ctrlKey || event.metaKey;
    if (this.commandOpen) {
      if (event.key === 'Escape') {
        event.preventDefault();
        this.closeCommandPalette();
      }
      return;
    }
    if (mod && event.shiftKey && event.key.toLowerCase() === 'p') {
      event.preventDefault();
      this.openCommandPalette();
      return;
    }
    if (mod && event.shiftKey && event.key.toLowerCase() === 'f') {
      event.preventDefault();
      this.showChat();
      this.openMessageSearch();
      return;
    }
    if (mod && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      this.setView('sessions', false, true);
      this.$nextTick(() => {
        const search = document.querySelector('.session-search') as HTMLElement | null;
        if (search) search.focus();
      });
      return;
    }
    if (mod && event.key.toLowerCase() === 'b') {
      event.preventDefault();
      this.toggleSidebar();
      return;
    }
    if (mod && event.key.toLowerCase() === 'j') {
      event.preventDefault();
      this.toggleInspector();
      return;
    }
    if (mod && event.key === 'Enter') {
      event.preventDefault();
      this.send();
      return;
    }
    if (event.key === 'Escape') {
      if (this.messageSearchOpen) { this.closeMessageSearch(); return; }
      if (this.notificationsOpen) { this.closeNotifications(); return; }
      if (this.shortcutsOpen) { this.shortcutsOpen = false; return; }
      if (this.showSettings) { this.showSettings = false; return; }
      if (this.showRecovery) { this.showRecovery = false; return; }
    }
    if (!typing && event.key === '?') {
      event.preventDefault();
      this.shortcutsOpen = !this.shortcutsOpen;
    }
  },

  onCompositionStart() {
    this._composing = true;
  },

  onCompositionEnd() {
    this._composing = false;
    this._compositionEndAt = Date.now();
  },

  onEnter(event) {
    if (event.shiftKey) return;
    // 中文输入法选词时的 Enter 不能提交；CompositionEvent 结束后
    // 浏览器仍可能补发一次 keydown，用 keyCode 229 和极短时间窗
    // 一并拦截，避免把没打完的提示词发出去。
    if (
      this._composing ||
      event.isComposing ||
      event.keyCode === 229 ||
      Date.now() - this._compositionEndAt < 80
    ) {
      return;
    }
    event.preventDefault();
    this.send();
  },

  openNewSettings() {
    this.addTask();
  },
};
