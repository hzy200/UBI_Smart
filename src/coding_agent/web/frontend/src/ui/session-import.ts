export interface SessionImportPayload {
  title: string;
  messages: Record<string, unknown>[];
  transcript: Record<string, unknown>[];
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

export function normalizeSessionImport(raw: unknown): SessionImportPayload {
  if (!isRecord(raw)) throw new Error('导入文件不是对象');
  const messages = Array.isArray(raw.messages)
    ? raw.messages.filter(isRecord)
    : [];
  const transcript = Array.isArray(raw.transcript)
    ? raw.transcript.filter(isRecord)
    : [];
  if (!messages.length && !transcript.length) {
    throw new Error('导入文件没有消息');
  }
  return {
    title: String(raw.title || '').slice(0, 120),
    messages,
    transcript,
  };
}
