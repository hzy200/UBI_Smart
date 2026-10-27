export interface ClientErrorReport {
  kind: string;
  message: string;
  stack: string;
  url: string;
  at: string;
}

export function normalizeClientError(
  kind: string,
  error: unknown,
  path: string,
): ClientErrorReport | null {
  const isObject = Boolean(error) && typeof error === 'object';
  const name = isObject
    ? String((error as { name?: unknown }).name || '')
    : '';
  const raw = typeof error === 'string'
    ? error
    : (isObject ? String((error as { message?: unknown }).message || '') : String(error || ''));
  const message = raw.replace(/\s+/g, ' ').trim();
  if (!message) return null;
  if (name === 'AbortError' || /abort/i.test(name)) return null;
  const stack = String((error as { stack?: unknown })?.stack || '');
  return {
    kind: String(kind || 'error').slice(0, 32),
    message: message.slice(0, 500),
    stack: stack.slice(0, 4000),
    url: String(path || '').slice(0, 500),
    at: new Date().toISOString(),
  };
}
