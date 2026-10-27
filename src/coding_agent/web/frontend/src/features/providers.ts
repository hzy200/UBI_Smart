/* Alpine mixin: providers。由 app.ts 拆分，方法名保持稳定以兼容模板与构建守卫。 */

import {
  AuthError,
  ProblemError,
  applyAuthHeaders,
  authFetch,
  authUrl,
  clearStoredToken,
  getToken,
  newIdempotencyKey,
  parseHashToken,
  readJson,
  restoreDesktopToken,
  setToken,
} from '../api/http';
import { escapeHtml, formatDuration, formatTokens, renderMarkdown } from '../ui/format';
import { renderTimers, tokenBuffers, toolMap } from '../state/runtime';
import { ConfigError, apiUrl, currentRuntimeConfig, loadRuntimeConfig } from '../runtime-config';
import { consumeSse } from '../sse';
import { FRONTEND_VERSION, VersionError, compareSemver } from '../version';

export const providersMixin = {
  async loadProfiles() {
    try {
      const response = await authFetch('/api/provider-profiles');
      const data = await readJson(response, '读取 provider 档案失败');
      this.profiles = data.profiles || [];
      this.profilesEnabled = Boolean(data.profiles_enabled);
    } catch (err) {
      this.profiles = [];
    }
  },

  async saveProfile() {
    const payload = {
      name: this.profileName.trim(),
      base_url: this.profileBaseUrl.trim(),
      model: this.profileModel.trim(),
    };
    if (!payload.name || !payload.base_url || !payload.model) {
      this.toastMessage('provider 名称、base_url、model 都不能为空');
      return;
    }
    try {
      const response = await authFetch('/api/provider-profiles', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await readJson(response, '保存 provider 失败');
      this.profiles = data.profiles || [];
      await this.loadProviders();
      this.toastMessage(`provider ${payload.name} 已保存；请设置密钥`);
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async deleteProfile(name) {
    if (!window.confirm(`删除 provider 档案 ${name}？密钥不会自动删除。`)) return;
    try {
      const response = await authFetch(
        `/api/provider-profiles/${encodeURIComponent(name)}`,
        { method: 'DELETE' },
      );
      const data = await readJson(response, '删除 provider 失败');
      this.profiles = data.profiles || [];
      await this.loadProviders();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  editProfile(profile) {
    this.profileName = profile.name;
    this.profileBaseUrl = profile.base_url;
    this.profileModel = profile.model;
  },

  async discoverModels() {
    try {
      const response = await authFetch('/api/providers/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: this.providerName }),
      });
      const data = await readJson(response, '模型发现失败');
      this.modelOptions = data.models || [];
      this.modelOptionsText = this.modelOptions.slice(0, 20).join(', ');
      this.toastMessage(
        data.ok
          ? `发现 ${this.modelOptions.length} 个模型`
          : (data.error || '模型发现失败'),
      );
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  useLocalPreset() {
    this.profileName = this.profileName || 'local';
    this.profileBaseUrl = 'http://127.0.0.1:11434/v1';
    if (!this.profileModel) this.profileModel = 'qwen2.5-coder';
    this.toastMessage('已填入 Ollama / LM Studio 兼容端点，按需修改');
  },

};
