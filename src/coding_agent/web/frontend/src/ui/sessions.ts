/* 会话列表展示：最后消息摘要与元信息。 */

export function sessionPreviewText(message: unknown, limit = 90): string {
  const text = String(message ?? '').replace(/\s+/g, ' ').trim();
  if (!text) return '';
  const max = Math.max(1, Number(limit) || 90);
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
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
