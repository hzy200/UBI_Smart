export function prependOlder<T>(older: T[], current: T[]): T[] {
  return [...(older || []), ...(current || [])];
}
