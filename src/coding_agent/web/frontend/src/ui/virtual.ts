/* 变量高度虚拟列表的区间计算；纯函数，便于单元测试。 */

export interface VirtualRange {
  start: number;
  end: number;
  top: number;
  bottom: number;
}

export function computeVirtualRange(
  heights: number[],
  scrollTop: number,
  viewportHeight: number,
  overscan = 6,
): VirtualRange {
  const total = heights.length;
  if (!total) return { start: 0, end: 0, top: 0, bottom: 0 };
  const prefix = new Array<number>(total + 1).fill(0);
  for (let index = 0; index < total; index += 1) {
    prefix[index + 1] = prefix[index] + Math.max(0, Number(heights[index]) || 0);
  }
  const top = Math.max(0, Number(scrollTop) || 0);
  const viewport = Math.max(0, Number(viewportHeight) || 0);
  let start = 0;
  while (start < total && prefix[start + 1] <= top) start += 1;
  let end = start;
  const limit = top + viewport;
  while (end < total && prefix[end] < limit) end += 1;
  start = Math.max(0, start - Math.max(0, overscan));
  end = Math.min(total, end + Math.max(0, overscan));
  return {
    start,
    end,
    top: prefix[start],
    bottom: prefix[total] - prefix[end],
  };
}
