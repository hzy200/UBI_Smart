/* 运行时配置：基址、认证模式、SSE 参数。
 *
 * 设计契约见 docs/rfc/0003-frontend-backend-separation.md。
 * 配置非法时必须失败关闭：阻断页优先于登录页，不能退回同源猜测。
 */

export type AuthMode = 'desktop-token' | 'bearer' | 'bff-cookie' | 'local-session';

export interface RuntimeAuthConfig {
  loginUrl: string;
  refreshUrl: string;
  logoutUrl: string;
  csrfHeader: string;
}

export interface RuntimeCostConfig {
  currency: 'CNY' | 'USD';
  usdToCny: number;
}

export interface RuntimeSseConfig {
  heartbeatMs: number;
  idleTimeoutMs: number;
  reconnectBaseMs: number;
  reconnectMaxMs: number;
  reconnectMaxAttempts: number;
}

export interface RuntimeConfig {
  schemaVersion: 1;
  apiBaseUrl: string;
  apiVersion: string;
  authMode: AuthMode;
  auth: RuntimeAuthConfig;
  cost: RuntimeCostConfig;
  sse: RuntimeSseConfig;
  requestIdHeader: string;
}

export class ConfigError extends Error {
  readonly code = 'config.invalid';

  constructor(message: string) {
    super(message);
    this.name = 'ConfigError';
  }
}

const AUTH_MODES: AuthMode[] = [
  'desktop-token',
  'bearer',
  'bff-cookie',
  'local-session',
];
const DEFAULT_SSE: RuntimeSseConfig = {
  heartbeatMs: 15000,
  idleTimeoutMs: 45000,
  reconnectBaseMs: 500,
  reconnectMaxMs: 30000,
  reconnectMaxAttempts: 3,
};

let activeConfig: RuntimeConfig | null = null;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function readNumber(
  value: unknown,
  fallback: number,
  min: number,
  max: number,
  label: string,
): number {
  if (value === undefined || value === null) return fallback;
  const number = Number(value);
  if (!Number.isFinite(number) || number < min || number > max) {
    throw new ConfigError(`${label} 必须是 ${min}–${max} 之间的数字`);
  }
  return Math.round(number);
}

function readBaseUrl(value: unknown): string {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value !== 'string') {
    throw new ConfigError('apiBaseUrl 必须是字符串');
  }
  const text = value.trim();
  if (!text) return '';
  if (/\s/.test(text)) {
    throw new ConfigError('apiBaseUrl 不能包含空白字符');
  }
  if (text.startsWith('//')) {
    throw new ConfigError('apiBaseUrl 不允许协议相对 URL');
  }
  if (text.startsWith('/')) {
    if (text.includes('?') || text.includes('#')) {
      throw new ConfigError('apiBaseUrl 不能包含 query 或 fragment');
    }
    return assertNotApiBase(text.replace(/\/+$/, ''));
  }
  let parsed: URL;
  try {
    parsed = new URL(text);
  } catch (err) {
    throw new ConfigError('apiBaseUrl 必须是同源路径或 http(s) 绝对 URL');
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new ConfigError('apiBaseUrl 只允许 http/https');
  }
  if (parsed.username || parsed.password) {
    throw new ConfigError('apiBaseUrl 不能包含用户名或密码');
  }
  if (parsed.search || parsed.hash) {
    throw new ConfigError('apiBaseUrl 不能包含 query 或 fragment');
  }
  return assertNotApiBase(text.replace(/\/+$/, ''));
}

// 基址只描述 origin/部署前缀；版本段由 apiUrl 按运行时配置插入。
function assertNotApiBase(value: string): string {
  if (value === '/api' || value.endsWith('/api')) {
    throw new ConfigError(
      'apiBaseUrl 不能以 /api 结尾；只填 origin 或部署前缀',
    );
  }
  return value;
}

function readAuthUrl(value: unknown, label: string): string {
  if (value === undefined || value === null || value === '') return '';
  if (typeof value !== 'string') {
    throw new ConfigError(`${label} 必须是字符串`);
  }
  const text = value.trim();
  if (!text) return '';
  if (text.startsWith('//')) {
    throw new ConfigError(`${label} 不允许协议相对 URL`);
  }
  if (text.startsWith('/')) return text;
  let parsed: URL;
  try {
    parsed = new URL(text);
  } catch (err) {
    throw new ConfigError(`${label} 必须是同源路径或 http(s) 绝对 URL`);
  }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
    throw new ConfigError(`${label} 只允许 http/https`);
  }
  if (parsed.username || parsed.password) {
    throw new ConfigError(`${label} 不能包含用户名或密码`);
  }
  return text;
}

export function validateRuntimeConfig(raw: unknown): RuntimeConfig {
  if (!isRecord(raw)) {
    throw new ConfigError('运行时配置必须是 JSON 对象');
  }
  if (raw.schemaVersion !== 1) {
    throw new ConfigError(
      `不支持的运行时配置版本：${String(raw.schemaVersion)}`,
    );
  }
  const apiBaseUrl = readBaseUrl(raw.apiBaseUrl);
  const apiVersion =
    typeof raw.apiVersion === 'string' ? raw.apiVersion.trim() : '';
  if (!/^v\d+$/.test(apiVersion)) {
    throw new ConfigError('apiVersion 必须是 v0、v1 这样的版本号');
  }
  const authMode = raw.authMode;
  if (typeof authMode !== 'string' || !AUTH_MODES.includes(authMode as AuthMode)) {
    throw new ConfigError(
      'authMode 必须是 desktop-token、bearer、bff-cookie 或 local-session',
    );
  }
  const authRaw = isRecord(raw.auth) ? raw.auth : {};
  const auth: RuntimeAuthConfig = {
    loginUrl: readAuthUrl(authRaw.loginUrl, 'auth.loginUrl'),
    refreshUrl: readAuthUrl(authRaw.refreshUrl, 'auth.refreshUrl'),
    logoutUrl: readAuthUrl(authRaw.logoutUrl, 'auth.logoutUrl'),
    csrfHeader:
      typeof authRaw.csrfHeader === 'string' && authRaw.csrfHeader.trim()
        ? authRaw.csrfHeader.trim()
        : 'X-CSRF-Token',
  };
  if (authMode === 'bff-cookie' && (!auth.loginUrl || !auth.refreshUrl)) {
    throw new ConfigError('bff-cookie 模式必须配置 auth.loginUrl 与 auth.refreshUrl');
  }
  const costRaw = isRecord(raw.cost) ? raw.cost : {};
  const cost: RuntimeCostConfig = {
    currency: costRaw.currency === 'USD' ? 'USD' : 'CNY',
    usdToCny:
      costRaw.currency === 'USD'
        ? 1
        : readNumber(costRaw.usdToCny, 7.2, 0.0001, 100, 'cost.usdToCny'),
  };
  const sseRaw = isRecord(raw.sse) ? raw.sse : {};
  const sse: RuntimeSseConfig = {
    heartbeatMs: readNumber(
      sseRaw.heartbeatMs,
      DEFAULT_SSE.heartbeatMs,
      5000,
      60000,
      'sse.heartbeatMs',
    ),
    idleTimeoutMs: readNumber(
      sseRaw.idleTimeoutMs,
      DEFAULT_SSE.idleTimeoutMs,
      5000,
      300000,
      'sse.idleTimeoutMs',
    ),
    reconnectBaseMs: readNumber(
      sseRaw.reconnectBaseMs,
      DEFAULT_SSE.reconnectBaseMs,
      100,
      10000,
      'sse.reconnectBaseMs',
    ),
    reconnectMaxMs: readNumber(
      sseRaw.reconnectMaxMs,
      DEFAULT_SSE.reconnectMaxMs,
      1000,
      120000,
      'sse.reconnectMaxMs',
    ),
    reconnectMaxAttempts: readNumber(
      sseRaw.reconnectMaxAttempts,
      DEFAULT_SSE.reconnectMaxAttempts,
      1,
      20,
      'sse.reconnectMaxAttempts',
    ),
  };
  if (sse.reconnectMaxMs < sse.reconnectBaseMs) {
    throw new ConfigError('sse.reconnectMaxMs 不能小于 sse.reconnectBaseMs');
  }
  if (sse.idleTimeoutMs < sse.heartbeatMs * 2) {
    throw new ConfigError('sse.idleTimeoutMs 不能小于 heartbeatMs 的两倍');
  }
  const requestIdHeader =
    typeof raw.requestIdHeader === 'string' && raw.requestIdHeader.trim()
      ? raw.requestIdHeader.trim()
      : 'X-Request-ID';
  if (!/^[A-Za-z0-9-]{1,64}$/.test(requestIdHeader)) {
    throw new ConfigError('requestIdHeader 不是合法的 HTTP 头名');
  }
  const config: RuntimeConfig = {
    schemaVersion: 1,
    apiBaseUrl,
    apiVersion,
    authMode: authMode as AuthMode,
    auth,
    cost,
    sse,
    requestIdHeader,
  };
  activeConfig = config;
  return config;
}

export async function loadRuntimeConfig(): Promise<RuntimeConfig> {
  if (activeConfig) return activeConfig;
  const injected = window.__APP_CONFIG__;
  let raw: unknown;
  if (injected) {
    raw = injected;
  } else {
    const url = new URL('config.json', document.baseURI).toString();
    let response: Response;
    try {
      response = await fetch(url, {
        cache: 'no-store',
        headers: { Accept: 'application/json' },
      });
    } catch (err) {
      throw new ConfigError(`无法读取运行时配置：${err}`);
    }
    if (!response.ok) {
      throw new ConfigError(`读取运行时配置失败：HTTP ${response.status}`);
    }
    try {
      raw = await response.json();
    } catch (err) {
      throw new ConfigError('运行时配置不是合法 JSON');
    }
  }
  return validateRuntimeConfig(raw);
}

export function apiUrl(path: string): string {
  if (!activeConfig) {
    throw new ConfigError('运行时配置尚未加载');
  }
  if (/^[a-z][a-z0-9+.-]*:/i.test(path)) {
    if (!/^https?:/i.test(path)) {
      throw new ConfigError('API 请求只允许 http/https 绝对 URL');
    }
    return path;
  }
  const normalized = path.startsWith('/') ? path : `/${path}`;
  // 调用方仍写 `/api/...`；版本段集中在这里插入，避免 80 处调用各写一遍。
  // 已带版本（/api/v1/...）的路径原样保留。
  const versioned = normalized.replace(
    /^\/api\/(?!v\d+\/)/,
    `/api/${activeConfig.apiVersion}/`,
  );
  if (!activeConfig.apiBaseUrl) return versioned;
  return `${activeConfig.apiBaseUrl}${versioned}`;
}

export function currentRuntimeConfig(): RuntimeConfig | null {
  return activeConfig;
}
