/* Alpine mixin: system。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  authFetch,
  cachedJson,
  readJson,
} from '../api/http';

export const systemMixin = {
  async refreshHealth() {
    const response = await authFetch('/api/health');
    const data = await readJson(response, '无法读取服务状态');
    this.healthText = data.reachable
      ? `${data.model} · ${data.user || ''}`
      : `API 不可用：${data.error || '未配置 Key'}`;
    if (!this.model) this.model = data.model || '';
    if (!this.workspace && data.workspace) {
      this.workspace = data.workspace;
      this.workspaceInput = data.workspace;
    }
  },

  async loadWorkspace() {
    const response = await authFetch('/api/workspace');
    const data = await readJson(response, '无法读取工作区');
    if (data.workspace) {
      this.workspace = data.workspace;
      this.workspaceInput = this.workspaceInput || data.workspace;
    }
  },

  async loadCapabilities() {
    const query = this.threadId
      ? `?thread_id=${encodeURIComponent(this.threadId)}`
      : '';
    // 只做 in-flight 去重：能力随会话/工作区变化，不做跨调用 TTL 缓存。
    this.capabilities = await cachedJson(
      `/api/capabilities${query}`,
      '无法读取能力',
      { ttlMs: 0 },
    );
    const capabilitiesVersion = Number(this.capabilities.capabilities_version);
    if (Number.isFinite(capabilitiesVersion) && capabilitiesVersion > 0) {
      this.capabilitiesVersion = capabilitiesVersion;
    }
    this.oidcAvailable = Boolean(this.capabilities.oidc_enabled);
    this.fourEyes = Boolean(this.capabilities.four_eyes);
    this.fourEyesMinLevel = Number(
      this.capabilities.four_eyes_min_level || 3,
    );
    const budget = Number(this.capabilities.run_cost_budget_usd || 0);
    const maxUpload = Number(this.capabilities.max_upload_bytes || 0);
    const projectUpload = Number(
      this.capabilities.workspace_upload_max_bytes || 0,
    );
    this.limitsText =
      `单次运行成本预算：${budget > 0 ? '$' + budget.toFixed(2) : '未设置'} · ` +
      `单文件上传上限：${this.formatBytes(maxUpload)}` +
      (projectUpload
        ? ` · 项目归档上限：${this.formatBytes(projectUpload)}`
        : '');
    this.refreshBadgeClass();
    if (this.capabilities.workspace_registry) {
      await this.loadProjects();
      await this.loadRevisions();
    }
  },

  async loadProviders(force = false) {
    try {
      const data = await cachedJson(
        '/api/providers',
        '读取 provider 失败',
        { ttlMs: 30_000, force },
      );
      this.providers = (data.providers || []).map((item) => ({
        ...item,
        _key: `${item.name}:${item.model}`,
        _label: `${item.name} / ${item.model}`,
      }));
      if (this.providers.length && !this.providers.some((p) => p.name === this.providerName)) {
        this.providerName = this.providers[0].name;
      }
    } catch (e) { /* provider 端点是 P1，可缺省 */ }
  },

  async saveProviderKey() {
    const key = this.providerKeyInput.trim();
    if (!key) return;
    try {
      const response = await authFetch(
        `/api/providers/${encodeURIComponent(this.providerName)}/key`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ api_key: key }),
        },
      );
      await readJson(response, '保存密钥失败');
      this.providerKeyInput = '';
      this.toastMessage('密钥已保存到系统密钥环');
      await this.loadProviders(true);
      await this.refreshHealth();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async testProvider() {
    try {
      const response = await authFetch('/api/providers/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: this.providerName }),
      });
      const data = await readJson(response, '测试连接失败');
      if (data.ok) {
        this.toastMessage(`连接成功：${data.model || data.status_code}`);
      } else {
        this.toastMessage(`连接失败：${data.error || data.status_code}`);
      }
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async deleteProviderKey() {
    try {
      const response = await authFetch(
        `/api/providers/${encodeURIComponent(this.providerName)}/key`,
        { method: 'DELETE' },
      );
      await readJson(response, '删除密钥失败');
      this.toastMessage('密钥已删除');
      await this.loadProviders(true);
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

};
