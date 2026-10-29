/* 会话列表展示：最后消息摘要与元信息。 */

export function sessionPreviewText(message: unknown, limit = 90): string {
  const text = String(message ?? '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  const max = Math.max(1, Number(limit) || 90);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

function sessionRows(payload: unknown): any[] {
  if (!payload || typeof payload !== 'object') return [];
  const items = (payload as { sessions?: unknown }).sessions;
  return Array.isArray(items) ? items : [];
}

/** 合并“活跃 + 归档”两次列表响应；按 thread_id 去重，活跃优先。 */
export function mergeSessionPayloads(
  active: unknown,
  archived: unknown,
): { sessions: any[] } {
  const rows: any[] = [];
  const seen = new Set<string>();
  for (const payload of [active, archived]) {
    for (const item of sessionRows(payload)) {
      const id = item && String(item.thread_id || '');
      if (!id || seen.has(id)) continue;
      seen.add(id);
      rows.push(item);
    }
  }
  return { sessions: rows };
}

/** 任一来源的 total 大于已加载条数时，说明还能继续加载。 */
export function sessionPayloadHasMore(
  active: unknown,
  archived: unknown,
): boolean {
  return [active, archived].some((payload) => {
    if (!payload || typeof payload !== 'object') return false;
    const loaded = sessionRows(payload).length;
    const total = Number((payload as { total?: unknown }).total);
    return Number.isFinite(total) && total > loaded;
  });
}

export function sessionMetaText(row: any): string {
  const parts: string[] = [];
  const when = String((row && row.last_active) || '')
    .replace('T', ' ')
    .slice(0, 16);
  if (when) parts.push(when);
  const prompts = Number((row && row.prompts) || 0);
  const calls = Number((row && row.tool_calls) || 0);
  if (prompts) parts.push(`${prompts} 轮`);
  if (calls) parts.push(`${calls} 次工具`);
  if (row && row.archived) parts.push('已归档');
  if (row && row.interrupted) parts.push('异常中断');
  return parts.join(' · ');
}
