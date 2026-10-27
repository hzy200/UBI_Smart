/* 会话深链参数。只处理 session / split，其余 query 原样保留。 */

export interface SessionUrlState {
  session: string;
  split: string;
}

const SESSION_PARAM = 'session';
const SPLIT_PARAM = 'split';

export function parseSessionUrl(search: string): SessionUrlState {
  const params = new URLSearchParams(String(search || ''));
  return {
    session: String(params.get(SESSION_PARAM) || '').trim(),
    split: String(params.get(SPLIT_PARAM) || '').trim(),
  };
}

export function buildSessionUrl(
  pathname: string,
  search: string,
  state: SessionUrlState,
): string {
  const params = new URLSearchParams(String(search || ''));
  const session = String(state.session || '').trim();
  const split = String(state.split || '').trim();
  setParam(params, SESSION_PARAM, session);
  setParam(params, SPLIT_PARAM, split && split !== session ? split : '');
  const query = params.toString();
  return `${pathname || '/'}${query ? `?${query}` : ''}`;
}

function setParam(params: URLSearchParams, key: string, value: string): void {
  if (value) params.set(key, value);
  else params.delete(key);
}
