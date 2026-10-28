export function formatNumber(value: number): string {
  return new Intl.NumberFormat('zh-CN').format(Number(value || 0));
}

export function formatCost(value: number): string {
  return `¥${Number(value || 0).toFixed(4)}`;
}

export function formatDate(value: string): string {
  if (!value) return '—';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

export function initials(name: string): string {
  return String(name || '?').slice(0, 2).toUpperCase();
}

export function dayString(date: Date): string {
  return date.toISOString().slice(0, 10);
}

export function daysAgo(days: number): Date {
  return new Date(Date.now() - days * 24 * 3600 * 1000);
}
