/* 认证、请求与错误协议。
 *
 * 所有 fetch 都必须经过 authFetch；它负责 Bearer/BFF-Cookie、CSRF、
 * 401 单飞刷新、前端版本头与统一错误解析。
 */

import { FRONTEND_VERSION } from '../version';
import { apiUrl, currentRuntimeConfig } from '../runtime-config';
import { responseCache } from './cache';
import type { CacheOptions } from './cache';

const TOKEN_KEY = 'codingagent.token';

let accessToken = '';

export function getToken() {
  return accessToken;
}

export function setToken(value) {
  const next = String(value || '');
  // 切换身份时不能让上一个用户/登录态的 GET 缓存继续命中。
  if (next !== accessToken) responseCache.clear();
  accessToken = next;
  const mode = currentRuntimeConfig()?.authMode;
  if (mode === 'desktop-token') {
    if (accessToken) sessionStorage.setItem(TOKEN_KEY, accessToken);
    else sessionStorage.removeItem(TOKEN_KEY);
  } else if (mode) {
    // Bearer 只驻内存；BFF 完全不用本地 token。
    sessionStorage.removeItem(TOKEN_KEY);
  }
}

function isSafeMethod(method?: string) {
  const upper = String(method || 'GET').toUpperCase();
  return upper === 'GET' || upper === 'HEAD' || upper === 'OPTIONS';
}

function readCookie(name: string) {
  const prefix = `${name}=`;
  for (const part of document.cookie.split(';')) {
    const item = part.trim();
    if (!item.startsWith(prefix)) continue;
    try {
      return decodeURIComponent(item.slice(prefix.length));
    } catch (err) {
      return '';
    }
  }
  return '';
}

function csrfCookie() {
  return readCookie('csrf_token') || readCookie('XSRF-TOKEN');
}

export function applyAuthHeaders(headers: Headers, method?: string) {
  const config = currentRuntimeConfig();
  if (config?.authMode !== 'bff-cookie') {
    const token = getToken();
    if (token) headers.set('Authorization', `Bearer ${token}`);
  }
  headers.set('X-Frontend-Version', FRONTEND_VERSION);
  if (config?.authMode === 'bff-cookie' && !isSafeMethod(method)) {
    const token = csrfCookie();
    if (token) {
      headers.set(config.auth.csrfHeader || 'X-CSRF-Token', token);
    }
  }
}

function authCredentials(): RequestCredentials {
  return currentRuntimeConfig()?.authMode === 'bff-cookie'
    ? 'include'
    : 'same-origin';
}

export function authUrl(path: string) {
  if (/^https?:/i.test(path)) return path;
  return new URL(path, document.baseURI).toString();
}

function notifyAuthFailure() {
  responseCache.clear();
  window.dispatchEvent(new CustomEvent('codingagent:auth-required'));
}

export function restoreDesktopToken() {
  if (!accessToken) {
    accessToken = sessionStorage.getItem(TOKEN_KEY) || '';
  }
  setToken(accessToken);
}

export function clearStoredToken() {
  responseCache.clear();
  sessionStorage.removeItem(TOKEN_KEY);
}

export function parseHashToken() {
  const match = location.hash.match(/token=([^&]+)/);
  if (!match) return '';
  const token = decodeURIComponent(match[1]);
  history.replaceState(null, '', location.pathname + location.search);
  return token;
}

export class AuthError extends Error {}

export class ProblemError extends Error {
  status: number;
  code: string;
  requestId: string;
  retryable: boolean;
  details: any;

  constructor(message: string, info: {
    status: number;
    code: string;
    requestId: string;
    retryable: boolean;
    details: any;
  }) {
    super(message);
    this.name = 'ProblemError';
    this.status = info.status;
    this.code = info.code;
    this.requestId = info.requestId;
    this.retryable = info.retryable;
    this.details = info.details;
  }
}

export function newIdempotencyKey() {
  const cryptoApi = window.crypto as Crypto & { randomUUID?: () => string };
  if (cryptoApi && typeof cryptoApi.randomUUID === 'function') {
    return cryptoApi.randomUUID();
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

let refreshPromise: Promise<boolean> | null = null;

async function refreshAuth(): Promise<boolean> {
  const config = currentRuntimeConfig();
  if (
    !config
    || config.authMode === 'desktop-token'
    || !config.auth.refreshUrl
  ) {
    return false;
  }
  if (refreshPromise) return refreshPromise;
  refreshPromise = (async () => {
    const headers = new Headers({
      Accept: 'application/json',
      'X-Frontend-Version': FRONTEND_VERSION,
    });
    if (config.authMode === 'bff-cookie') {
      const token = csrfCookie();
      if (token) {
        headers.set(config.auth.csrfHeader || 'X-CSRF-Token', token);
      }
    }
    let response: Response;
    try {
      response = await fetch(authUrl(config.auth.refreshUrl), {
        method: 'POST',
        credentials: 'include',
        headers,
      });
    } catch (err) {
      return false;
    }
    if (!response.ok) return false;
    if (config.authMode === 'bearer') {
      let data: any = null;
      try {
        data = await response.json();
      } catch (err) {
        return false;
      }
      const token = data && typeof data.access_token === 'string'
        ? data.access_token
        : '';
      if (!token) return false;
      setToken(token);
    }
    return true;
  })().finally(() => {
    refreshPromise = null;
  });
  return refreshPromise;
}

export async function authFetch(
  url: string,
  options: RequestInit = {},
  allowRefresh = true,
) {
  const headers = new Headers(options.headers || {});
  applyAuthHeaders(headers, options.method);
  const response = await fetch(apiUrl(url), {
    ...options,
    headers,
    credentials: authCredentials(),
  });
  if (response.status === 401) {
    if (allowRefresh) {
      const refreshed = await refreshAuth();
      if (refreshed) return authFetch(url, options, false);
    }
    notifyAuthFailure();
    throw new AuthError('需要认证');
  }
  return response;
}

export async function readJson(response, fallbackMessage) {
  if (!response.ok) {
    let text = '';
    try { text = await response.text(); } catch (e) { /* ignore */ }
    let payload: any = null;
    try { payload = text ? JSON.parse(text) : null; } catch (e) { /* 非 JSON */ }
    const code = payload && typeof payload.code === 'string'
      ? payload.code
      : `http.${response.status}`;
    const requestId = (
      payload && typeof payload.request_id === 'string' && payload.request_id
    ) || response.headers.get('X-Request-ID') || '';
    const rawDetail = payload && payload.detail !== undefined
      ? payload.detail
      : text;
    const detail = typeof rawDetail === 'string'
      ? rawDetail
      : JSON.stringify(rawDetail);
    const clipped = String(
      detail || fallbackMessage || `HTTP ${response.status}`,
    ).slice(0, 500);
    const suffix = requestId ? ` · request_id=${requestId}` : '';
    throw new ProblemError(`${clipped}（${code}${suffix}）`, {
      status: response.status,
      code,
      requestId,
      retryable: Boolean(payload && payload.retryable),
      details: payload ? payload.details : undefined,
    });
  }
  return response.json();
}

export async function cachedJson<T = any>(
  url: string,
  fallbackMessage: string,
  options: CacheOptions = {},
): Promise<T> {
  return responseCache.run(
    String(url),
    async () => {
      const response = await authFetch(url, {
        headers: { Accept: 'application/json' },
      });
      return readJson(response, fallbackMessage) as Promise<T>;
    },
    options,
  );
}

export function invalidateGet(prefix = ''): void {
  responseCache.invalidate(prefix);
}

export function clearGetCache(): void {
  responseCache.clear();
}
