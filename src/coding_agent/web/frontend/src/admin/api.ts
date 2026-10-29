/* 管理端 API 客户端。
 *
 * 前端是独立静态产物：API 基址只从站点根的 config.json 读取，不依赖
 * FastAPI 注入；跨源部署由反向代理/边缘提供 CORS 与凭证策略。
 */

import type {
  AdminUser,
  ApprovalRow,
  AuthEvent,
  LoginResponse,
  Me,
  ProviderKeyRow,
  Quota,
  UsageReport,
  UsageToday,
} from './types';

interface RuntimeConfig {
  apiBaseUrl?: string;
  authMode?: string;
  auth?: { csrfHeader?: string };
}

let runtime: RuntimeConfig =
  ((window as any).__APP_CONFIG__ as RuntimeConfig) || {};

export class AuthError extends Error {}

export class ApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown>;

  constructor(status: number, code: string, message: string, details: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.details = details;
  }
}

function joinUrl(base: string, path: string): string {
  if (!base) return path;
  return `${base.replace(/\/$/, '')}${path}`;
}

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  if ((window as any).__APP_CONFIG__) {
    runtime = (window as any).__APP_CONFIG__ as RuntimeConfig;
    return runtime;
  }
  try {
    const response = await fetch('/config.json', { cache: 'no-store' });
    if (response.ok) runtime = await response.json();
  } catch (err) {
    // 独立静态站可以没有 config.json；此时按同源 API 处理。
  }
  return runtime;
}

function readCookie(name: string): string {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(';')) {
    const item = part.trim();
    if (item.startsWith(prefix)) return decodeURIComponent(item.slice(prefix.length));
  }
  return '';
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const method = String(init.method || 'GET').toUpperCase();
  const headers = new Headers(init.headers || {});
  headers.set('Accept', 'application/json');
  if (init.body !== undefined && !(init.body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
  }
  if (method !== 'GET' && method !== 'HEAD') {
    const csrf = readCookie('ca_csrf');
    if (csrf) headers.set(runtime.auth?.csrfHeader || 'X-CSRF-Token', csrf);
  }
  const response = await fetch(joinUrl(runtime.apiBaseUrl || '', path), {
    ...init,
    method,
    headers,
    credentials: 'include',
    cache: 'no-store',
  });
  if (response.status === 401) throw new AuthError('需要登录');
  if (!response.ok) {
    let payload: any = {};
    try {
      payload = await response.json();
    } catch (err) { /* 非 JSON 错误体 */ }
    const code = String(payload.code || payload.detail || `http.${response.status}`);
    const message = String(payload.detail || payload.message || `请求失败（HTTP ${response.status}）`);
    throw new ApiError(response.status, code, message, payload.details || {});
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  me: () => request<Me>('/api/me'),
  login: (username: string, password: string) =>
    request<LoginResponse>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  mfaVerify: (mfaToken: string, code: string) =>
    request<LoginResponse>('/api/auth/mfa/verify', {
      method: 'POST',
      body: JSON.stringify({ mfa_token: mfaToken, code }),
    }),
  oidcStatus: () => request<{ enabled: boolean }>('/api/auth/oidc/status'),
  oidcStart: (returnTo = '/') =>
    request<{ authorization_url: string }>(
      `/api/auth/oidc/start?return_to=${encodeURIComponent(returnTo)}`,
    ),
  logout: () => request<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  pendingApprovals: () =>
    request<{ enabled: boolean; approvals: ApprovalRow[] }>('/api/admin/approvals'),
  decideApproval: (id: string, approved: boolean) =>
    request<{ ok: boolean; approval: ApprovalRow }>(
      `/api/admin/approvals/${encodeURIComponent(id)}/decide`,
      { method: 'POST', body: JSON.stringify({ approved }) },
    ),
  resetUserMfa: (id: string) =>
    request<{ ok: boolean }>(
      `/api/admin/users/${encodeURIComponent(id)}/mfa-reset`,
      { method: 'POST' },
    ),

  listUsers: () => request<{ users: AdminUser[] }>('/api/admin/users'),
  createUser: (payload: {
    username: string;
    email: string;
    role: 'admin' | 'approver' | 'auditor' | 'member';
    password: string;
  }) =>
    request<{ user: AdminUser }>('/api/admin/users', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  updateUser: (id: string, payload: { status?: string; role?: string; email?: string }) =>
    request<{ user: AdminUser }>(`/api/admin/users/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
  resetPassword: (id: string, password: string) =>
    request<{ ok: boolean }>(`/api/admin/users/${encodeURIComponent(id)}/reset-password`, {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  userQuota: (id: string) =>
    request<{ quota: Quota; usage: UsageToday }>(
      `/api/admin/users/${encodeURIComponent(id)}/quotas`,
    ),
  saveQuota: (id: string, quota: Quota) =>
    request<{ quota: Quota }>(`/api/admin/users/${encodeURIComponent(id)}/quotas`, {
      method: 'PUT',
      body: JSON.stringify(quota),
    }),
  userProviders: (id: string) =>
    request<{ providers: ProviderKeyRow[] }>(
      `/api/admin/users/${encodeURIComponent(id)}/providers`,
    ),
  saveProviderKey: (id: string, provider: string, apiKey: string) =>
    request<{ ok: boolean; last4: string }>(
      `/api/admin/users/${encodeURIComponent(id)}/providers/${encodeURIComponent(provider)}`,
      { method: 'PUT', body: JSON.stringify({ api_key: apiKey }) },
    ),
  deleteProviderKey: (id: string, provider: string) =>
    request<{ ok: boolean }>(
      `/api/admin/users/${encodeURIComponent(id)}/providers/${encodeURIComponent(provider)}`,
      { method: 'DELETE' },
    ),
  providers: () =>
    request<{ providers: ProviderKeyRow[] }>('/api/admin/providers'),
  authEvents: (username: string, limit = 20) => {
    const params = new URLSearchParams({ limit: String(limit) });
    if (username) params.set('user', username);
    return request<{ events: AuthEvent[] }>(
      `/api/admin/auth-events?${params.toString()}`,
    );
  },
  usage: (from: string, to: string, user = '') => {
    const params = new URLSearchParams({ from, to });
    if (user) params.set('user', user);
    return request<UsageReport>(`/api/admin/usage?${params.toString()}`);
  },
};
