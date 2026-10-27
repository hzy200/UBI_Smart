export interface SyncMessage {
  type: string;
  [key: string]: unknown;
}

export function parseSyncMessage(raw: unknown): SyncMessage | null {
  let value: any = raw;
  if (typeof raw === 'string') {
    try {
      value = JSON.parse(raw);
    } catch (err) {
      return null;
    }
  }
  if (!value || typeof value !== 'object') return null;
  if (typeof value.type !== 'string' || !value.type) return null;
  return value as SyncMessage;
}
