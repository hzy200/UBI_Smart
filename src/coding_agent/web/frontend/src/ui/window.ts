/* 尾部窗口切片：长会话只渲染最近 N 条。 */

export function tailSlice<T>(items: T[], count: number): T[] {
  const size = Math.max(0, Math.floor(Number(count) || 0));
  if (!size) return [];
  return items.slice(Math.max(0, items.length - size));
}
