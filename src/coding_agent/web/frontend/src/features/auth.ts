/* Alpine mixin: auth。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  applyAuthHeaders,
  authUrl,
  clearGetCache,
  clearStoredToken,
  getToken,
  restoreDesktopToken,
  setToken,
} from '../api/http';
import {
  ConfigError,
  currentRuntimeConfig,
} from '../runtime-config';
import { clearHistoryCache } from '../state/history-cache';
import {
  VersionError,
} from '../version';

export const authMixin = {
  applyAuthConfig(config) {
    this.authMode = config.authMode;
    this.authLoginUrl = config.auth.loginUrl || '';
    this.authRefreshUrl = config.auth.refreshUrl || '';
    this.authLogoutUrl = config.auth.logoutUrl || '';
    this.authCsrfHeader = config.auth.csrfHeader || 'X-CSRF-Token';
    if (config.authMode === 'desktop-token') {
      restoreDesktopToken();
    } else {
      clearStoredToken();
      if (config.authMode === 'bff-cookie') setToken('');
    }
    this.tokenInput = getToken();
  },

  showLogin(message) {
    this.ready = false;
    this.configError = '';
    this.configErrorCode = '';
    this.versionError = '';
    this.versionErrorCode = '';
    this.loginError = message || '';
    if (this.authMode === 'bff-cookie' && this.authLoginUrl) {
      this.startLogin();
    }
  },

  handleAuthFailure() {
    const now = Date.now();
    if (now - this._authFailureAt < 1500) return;
    this._authFailureAt = now;
    if (this.authMode === 'bff-cookie' && this.authLoginUrl) {
      this.startLogin();
      return;
    }
    this.showLogin('认证已失效，请重新输入 token');
  },

  startLogin() {
    if (!this.authLoginUrl) {
      this.toastMessage('未配置统一登录地址');
      return;
    }
    const target = authUrl(this.authLoginUrl);
    if (target === location.href) return;
    location.assign(target);
  },

  get authModeLabel() {
    if (this.authMode === 'desktop-token') return '桌面本地令牌';
    if (this.authMode === 'bff-cookie') return '统一登录（BFF Cookie）';
    return 'Bearer Token（仅内存）';
  },

  async logout() {
    const config = currentRuntimeConfig();
    try {
      if (config?.auth.logoutUrl) {
        const headers = new Headers({ Accept: 'application/json' });
        applyAuthHeaders(headers, 'POST');
        await fetch(authUrl(config.auth.logoutUrl), {
          method: 'POST',
          credentials: 'include',
          headers,
        });
      }
    } catch (err) {
      // 服务端注销失败也要清掉本地凭证，避免界面继续显示已登录。
    }
    setToken('');
    clearGetCache();
    void clearHistoryCache();
    this.tokenInput = '';
    this.clearAllDrafts();
    this.resetSessionViews();
    this._boundProjectId = '';
    this.resetConversation();
    this.ready = false;
    this.versionChecked = true;
    this.showLogin('已退出登录');
  },

  showConfigError(err) {
    this.ready = false;
    this.configLoaded = false;
    this.versionChecked = false;
    this.versionError = '';
    this.versionErrorCode = '';
    this.loginError = '';
    this.configErrorCode =
      err instanceof ConfigError ? err.code : 'config.invalid';
    this.configError =
      err instanceof ConfigError
        ? err.message
        : `运行时配置加载失败：${err}`;
  },

  showVersionError(err) {
    this.ready = false;
    this.versionChecked = false;
    this.configError = '';
    this.configErrorCode = '';
    this.loginError = '';
    this.versionErrorCode =
      err instanceof VersionError ? err.code : 'version.unavailable';
    this.versionError =
      err instanceof VersionError
        ? err.message
        : `无法校验前后端版本：${err}`;
  },

  reloadPage() {
    location.reload();
  },

  async saveToken() {
    setToken(this.tokenInput.trim());
    await this.bootstrap();
  },

};
