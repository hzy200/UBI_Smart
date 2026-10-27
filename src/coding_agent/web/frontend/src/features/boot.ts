/* Alpine mixin: boot。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  AuthError,
  authFetch,
  getToken,
  parseHashToken,
  readJson,
  setToken,
} from '../api/http';
import {
  apiUrl,
  currentRuntimeConfig,
  loadRuntimeConfig,
} from '../runtime-config';
import {
  FRONTEND_VERSION,
  VersionError,
  compareSemver,
} from '../version';

function installDesktopBridgeGuard() {
  const install = () => {
    const bridge = window.pywebview && window.pywebview.api;
    if (!bridge || (bridge as any).__codingagentGuarded) return;
    for (const name of Object.keys(bridge)) {
      const original = bridge[name];
      if (typeof original !== 'function') continue;
      try {
        bridge[name] = (...args: any[]) => {
          if (window.top !== window) {
            return Promise.reject(
              new Error('桌面桥只允许顶层窗口调用'),
            );
          }
          return original.apply(bridge, args);
        };
      } catch (err) {
        // 桥对象不可改写时，Python 侧的 origin 校验仍然生效。
      }
    }
    try {
      (bridge as any).__codingagentGuarded = true;
    } catch (err) { /* ignore */ }
  };
  install();
  window.addEventListener('pywebviewready', install);
}

export const bootMixin = {
  init() {
    // Alpine CSP 构建在 init 返回 Promise 时不会把数据挂到元素上；
    // 所以 init 保持同步，异步启动放到 bootstrap() 里。
    const hashToken = parseHashToken();
    if (hashToken) setToken(hashToken);
    this.tokenInput = getToken();
    installDesktopBridgeGuard();
    this.desktopAvailable = Boolean(window.pywebview && window.pywebview.api);
    // pywebview 可能在 Alpine 初始化之后才注入 API；以事件为准再更新一次。
    window.addEventListener('pywebviewready', () => {
      this.desktopAvailable = true;
      this.probeProject();
    });
    this.loadSettings();
    this.initLayout();
    this.loadRecentCommands();
    this.loadNotifications();
    this.applyBodyClasses();
    this.refreshBadgeClass();
    window.addEventListener('keydown', (event) => this.onGlobalKeydown(event));
    window.addEventListener('codingagent:auth-required', () => {
      this.handleAuthFailure();
    });
    void this.bootstrap();
  },

  async bootstrap() {
    if (!this.configLoaded) {
      try {
        await loadRuntimeConfig();
        this.configLoaded = true;
        this.configError = '';
        this.configErrorCode = '';
      } catch (err) {
        this.showConfigError(err);
        return;
      }
    }
    const config = currentRuntimeConfig();
    if (config) this.applyAuthConfig(config);
    if (!this.versionChecked) {
      try {
        await this.checkVersion();
        this.versionChecked = true;
        this.versionError = '';
        this.versionErrorCode = '';
      } catch (err) {
        this.showVersionError(err);
        return;
      }
    }
    try {
      await this.boot();
      await this.probeProject();
    } catch (err) {
      if (err instanceof AuthError) {
        this.handleAuthFailure();
        return;
      }
      this.showLogin(String(err));
    }
  },

  async checkVersion() {
    const config = currentRuntimeConfig();
    const response = await fetch(apiUrl('/healthz'), {
      cache: 'no-store',
      headers: {
        Accept: 'application/json',
        'X-Frontend-Version': FRONTEND_VERSION,
      },
    });
    if (!response.ok) {
      throw new VersionError(
        'version.unavailable',
        `无法读取前后端版本信息：HTTP ${response.status}`,
      );
    }
    let data: any;
    try {
      data = await response.json();
    } catch (err) {
      throw new VersionError('version.invalid_metadata', '版本信息不是合法 JSON');
    }
    const apiVersion = typeof data.api_version === 'string' ? data.api_version : '';
    const minFrontend = typeof data.min_frontend_version === 'string'
      ? data.min_frontend_version
      : '';
    if (!apiVersion || !minFrontend) {
      throw new VersionError(
        'version.invalid_metadata',
        '版本信息缺少 api_version 或 min_frontend_version',
      );
    }
    if (config && apiVersion !== config.apiVersion) {
      throw new VersionError(
        'version.api_mismatch',
        `运行时配置要求 API ${config.apiVersion}，服务端为 ${apiVersion}`,
      );
    }
    if (compareSemver(FRONTEND_VERSION, minFrontend) < 0) {
      throw new VersionError(
        'version.frontend_too_old',
        `前端版本 ${FRONTEND_VERSION} 低于服务端要求的最低版本 ${minFrontend}`,
      );
    }
    this.apiVersion = apiVersion;
    this.minFrontendVersion = minFrontend;
    this.serverVersion = String(data.version || '');
    const capabilityVersion = Number(data.capabilities_version);
    this.capabilitiesVersion = Number.isFinite(capabilityVersion)
      ? capabilityVersion
      : 0;
  },

  async boot() {
    // 健康检查可能探测外部模型服务，不能阻塞整个界面；
    // 本地能力/会话加载完成就先放行。
    await Promise.all([
      this.loadCapabilities(),
      this.loadProviders(),
      this.loadSessions(),
      this.loadWorkspace(),
      this.loadProfiles(),
    ]);
    this.ready = true;
    this.startConsentPolling();
    void this.refreshHealth().catch((err) => {
      this.healthText = `状态读取失败：${err}`;
    });
  },

  startConsentPolling() {
    if (this._consentTimer) return;
    this._consentTimer = setInterval(() => {
      void this.loadConsents();
    }, 1200);
    void this.loadConsents();
  },

  async loadConsents() {
    if (!this.consentSupported || !this.ready) return;
    try {
      const response = await authFetch('/api/consent/pending');
      if (response.status === 404) {
        this.consentSupported = false;
        return;
      }
      const data = await readJson(response, '读取授权请求失败');
      const pending = (data.pending || []).map((item) => ({
        ...item,
        _verbsText: this.consentVerbs(item.verbs),
      }));
      const hasNew = pending.length > this.consents.length;
      this.consents = pending;
      if (hasNew) this.scrollBottom();
    } catch (err) {
      // 认证失败由运行链路统一处理；这里保持静默，避免轮询刷 toast。
    }
  },

  consentVerbs(verbs) {
    const labels = {
      read: '读取文件',
      list: '列出目录',
      find: '查找文件',
      write: '写入文件',
      exec: '执行命令',
    };
    const items = (verbs || []).map(
      (verb) => labels[verb] || String(verb),
    );
    return items.join('、') || '读取文件';
  },

  async decideConsent(id, allow) {
    try {
      const response = await authFetch(
        `/api/consent/${encodeURIComponent(id)}`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ allow: Boolean(allow) }),
        },
      );
      await readJson(response, '提交授权失败');
      this.consents = this.consents.filter((item) => item.id !== id);
      this.toastMessage(
        allow ? '已允许本机文件访问' : '已拒绝本机文件访问',
      );
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

};
