/* 查询匹配的纯函数；视图与单元测试共用。 */

export function includesQuery(haystack: string, query: string): boolean {
  const needle = String(query || '').trim().toLowerCase();
  if (!needle) return true;
  return String(haystack || '').toLowerCase().includes(needle);
}

export function matchesAllTokens(haystack: string, query: string): boolean {
  const text = String(haystack || '').toLowerCase();
  const tokens = String(query || '')
    .trim()
    .toLowerCase()
    .split(/\s+/)
    .filter(Boolean);
  if (!tokens.length) return true;
  return tokens.every((token) => text.includes(token));
}
