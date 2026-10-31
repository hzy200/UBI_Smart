/* Alpine mixin: 个人访问令牌与出站 Webhook（CI / 外部集成）。 */

import { authFetch, readJson } from '../api/http';

export const integrationsMixin = {
  async loadIntegrations() {
    try {
      const response = await authFetch('/api/tokens');
      const data = await readJson(response, '读取访问令牌失败');
      this.tokensEnabled = data.enabled !== false;
      this.apiTokens = (data.tokens || []).map((item) => ({
        ...item,
        _scopes: (item.scopes || [])
          .map((scope) => this.tokenScopeLabel(scope))
          .join(' · '),
        _expires: item.expires_at
          ? String(item.expires_at).slice(0, 10)
          : this.t('settings.tokenNeverExpires'),
        _lastUsed: item.last_used_at
          ? String(item.last_used_at).slice(0, 16).replace('T', ' ')
          : '—',
      }));
    } catch (err) {
      this.tokensEnabled = false;
    }
    try {
      const response = await authFetch('/api/webhooks');
      const data = await readJson(response, '读取 Webhook 失败');
      this.webhooksEnabled = data.enabled !== false;
      this.webhooks = (data.webhooks || []).map((item) => ({
        ...item,
        _events: (item.events || [])
          .map((event) => this.webhookEventLabel(event))
          .join(' · '),
        _status: item.last_status
          ? `HTTP ${item.last_status}`
          : this.t('settings.webhookNeverSent'),
        _lastSent: item.last_sent_at
          ? String(item.last_sent_at).slice(0, 16).replace('T', ' ')
          : '—',
      }));
    } catch (err) {
      this.webhooksEnabled = false;
    }
  },

  tokenScopeLabel(scope) {
    const keys = {
      read: 'scopeRead',
      run: 'scopeRun',
      audit: 'scopeAudit',
    };
    const key = keys[scope];
    return key ? this.t(`settings.${key}`) : scope;
  },

  webhookEventLabel(event) {
    const keys = {
      run_finished: 'eventRunFinished',
      run_failed: 'eventRunFailed',
      approval_requested: 'eventApprovalRequested',
    };
    const key = keys[event];
    return key ? this.t(`settings.${key}`) : event;
  },

  toggleTokenScope(scope) {
    if (!scope) return;
    this.tokenScopes = {
      ...this.tokenScopes,
      [scope]: !this.tokenScopes[scope],
    };
  },

  toggleWebhookEvent(event) {
    if (!event) return;
    this.webhookEvents = {
      ...this.webhookEvents,
      [event]: !this.webhookEvents[event],
    };
  },

  onTokenAction(op, id) {
    if (op === 'revoke') void this.revokeToken(id);
  },

  onWebhookAction(op, id) {
    if (op === 'delete') void this.deleteWebhook(id);
  },

  async createToken() {
    if (this.tokenBusy) return;
    const scopes = ['read', 'run', 'audit'].filter(
      (scope) => this.tokenScopes[scope],
    );
    if (!scopes.length) {
      this.toastMessage(this.t('settings.tokenScopeRequired'));
      return;
    }
    this.tokenBusy = true;
    try {
      const response = await authFetch('/api/tokens', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: String(this.tokenName || '').trim() || 'CI token',
          scopes,
          expires_in_days: Math.max(0, Number(this.tokenDays) || 0),
        }),
      });
      const data = await readJson(response, '创建访问令牌失败');
      this.newToken = String(data.token || '');
      this.tokenName = '';
      this.toastMessage(this.t('settings.tokenCreatedToast'));
      await this.loadIntegrations();
    } catch (err) {
      this.toastMessage(String(err));
    } finally {
      this.tokenBusy = false;
    }
  },

  async revokeToken(id) {
    if (!id) return;
    if (!window.confirm(this.t('settings.tokenRevokeConfirm'))) return;
    try {
      const response = await authFetch(
        `/api/tokens/${encodeURIComponent(id)}`,
        { method: 'DELETE' },
      );
      await readJson(response, '撤销访问令牌失败');
      if (this.newToken) this.newToken = '';
      this.toastMessage(this.t('settings.tokenRevoked'));
      await this.loadIntegrations();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },

  async createWebhook() {
    if (this.webhookBusy) return;
    const events = ['run_finished', 'run_failed', 'approval_requested'].filter(
      (event) => this.webhookEvents[event],
    );
    if (!events.length) {
      this.toastMessage(this.t('settings.webhookEventRequired'));
      return;
    }
    if (!String(this.webhookUrl || '').trim()) {
      this.toastMessage(this.t('settings.webhookUrlRequired'));
      return;
    }
    this.webhookBusy = true;
    try {
      const response = await authFetch('/api/webhooks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: String(this.webhookName || '').trim() || 'Webhook',
          url: String(this.webhookUrl || '').trim(),
          events,
        }),
      });
      const data = await readJson(response, '创建 Webhook 失败');
      this.newWebhookSecret = String(data.secret || '');
      this.webhookUrl = '';
      this.webhookName = '';
      this.toastMessage(this.t('settings.webhookCreatedToast'));
      await this.loadIntegrations();
    } catch (err) {
      this.toastMessage(String(err));
    } finally {
      this.webhookBusy = false;
    }
  },

  async deleteWebhook(id) {
    if (!id) return;
    if (!window.confirm(this.t('settings.webhookDeleteConfirm'))) return;
    try {
      const response = await authFetch(
        `/api/webhooks/${encodeURIComponent(id)}`,
        { method: 'DELETE' },
      );
      await readJson(response, '删除 Webhook 失败');
      if (this.newWebhookSecret) this.newWebhookSecret = '';
      this.toastMessage(this.t('settings.webhookDeleted'));
      await this.loadIntegrations();
    } catch (err) {
      this.toastMessage(String(err));
    }
  },
};
