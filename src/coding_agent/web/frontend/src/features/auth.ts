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
  apiUrl,
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
      if (
        config.authMode === 'bff-cookie' ||
        config.authMode === 'local-session'
      ) {
        setToken('');
      }
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
    this.showLogin(
      this.authMode === 'local-session'
        ? '登录已失效，请重新登录'
        : '认证已失效，请重新输入 token',
    );
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
    if (this.authMode === 'desktop-token') {
      return this.t('auth.desktopToken');
    }
    if (this.authMode === 'bff-cookie') return this.t('auth.bffCookie');
    if (this.authMode === 'local-session') return this.t('auth.localSession');
    return this.t('auth.bearer');
  },

  async loginWithPassword() {
    const username = String(this.loginUsername || '').trim();
    const password = String(this.loginPassword || '');
    if (!username || !password) {
      this.loginError = '请输入用户名和密码';
      return;
    }
    try {
      const response = await fetch(apiUrl('/api/auth/login'), {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username, password }),
      });
      if (!response.ok) {
        let message = '登录失败';
        try {
          const data = await response.json();
          message = String(data.detail?.message || data.detail || message);
        } catch (err) { /* 非 JSON 错误体 */ }
        this.loginError = message;
        return;
      }
      this.loginPassword = '';
      this.loginError = '';
      await this.bootstrap();
    } catch (err) {
      this.loginError = `登录失败：${String(err)}`;
    }
  },

  async logout() {
    const config = currentRuntimeConfig();
    try {
      if (this.authMode === 'local-session') {
        const headers = new Headers({ Accept: 'application/json' });
        applyAuthHeaders(headers, 'POST');
        await fetch(apiUrl('/api/auth/logout'), {
          method: 'POST',
          credentials: 'include',
          headers,
        });
      } else if (config?.auth.logoutUrl) {
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
