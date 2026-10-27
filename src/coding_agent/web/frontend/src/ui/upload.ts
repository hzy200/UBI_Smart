export function uploadPercent(loaded: number, total: number): number {
  const size = Number(total);
  if (!Number.isFinite(size) || size <= 0) return 0;
  const percent = Math.round((Number(loaded) / size) * 100);
  return Math.max(0, Math.min(100, percent));
}
