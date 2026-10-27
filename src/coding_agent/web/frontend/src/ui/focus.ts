/* 模态焦点陷阱的纯计算部分。 */

export const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

/** 返回 Tab（shift=false）或 Shift+Tab（shift=true）应该聚焦的索引。 */
export function computeTabTarget(
  count: number,
  activeIndex: number,
  shift: boolean,
): number {
  if (count <= 0) return -1;
  if (activeIndex < 0 || activeIndex >= count) {
    return shift ? count - 1 : 0;
  }
  return shift
    ? (activeIndex - 1 + count) % count
    : (activeIndex + 1) % count;
}
