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

export type CommandKind = 'all' | 'command' | 'session' | 'file';

export interface ParsedCommandQuery {
  kind: CommandKind;
  query: string;
}

export function parseCommandQuery(input: string): ParsedCommandQuery {
  const text = String(input || '').trimStart();
  if (text.startsWith('>')) {
    return { kind: 'command', query: text.slice(1).trim() };
  }
  if (text.startsWith('@')) {
    return { kind: 'session', query: text.slice(1).trim() };
  }
  if (text.startsWith('#')) {
    return { kind: 'file', query: text.slice(1).trim() };
  }
  return { kind: 'all', query: text.trim() };
}

export function fuzzyScore(haystack: string, query: string): number {
  const text = String(haystack || '').toLowerCase();
  const needle = String(query || '').trim().toLowerCase();
  if (!needle) return 0;
  const direct = text.indexOf(needle);
  if (direct >= 0) {
    return 1000 - direct * 2 - Math.max(0, text.length - needle.length);
  }
  let position = -1;
  let score = 0;
  for (const char of needle) {
    const next = text.indexOf(char, position + 1);
    if (next < 0) return -1;
    score += 1 + (next === position + 1 ? 2 : 0);
    position = next;
  }
  return score;
}
