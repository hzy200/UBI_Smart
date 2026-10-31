/* 单槽缓存：派生列表只在输入键变化时重算。 */

export interface MemoSlot<T> {
  key: string;
  value: T;
}

export function memoize<T>(
  slot: MemoSlot<T>,
  key: string,
  compute: () => T,
): T {
  if (slot.key !== key) {
    slot.value = compute();
    slot.key = key;
  }
  return slot.value;
}
