/* 会话输入草稿的本地持久化。
 *
 * 只存 localStorage，不进入 URL、服务端或事件流；按 thread_id 隔离。
 * 新会话还没有 thread_id 时用 __new__ 占位，创建会话后迁移。
 */

export const DRAFT_STORAGE_KEY = 'codingagent.drafts';
export const NEW_DRAFT_KEY = '__new__';
const MAX_DRAFTS = 50;
const MAX_DRAFT_CHARS = 20_000;

export type DraftStore = Pick<Storage, 'getItem' | 'setItem' | 'removeItem'>;

export function draftKey(threadId: string): string {
  return String(threadId || '') || NEW_DRAFT_KEY;
}

function defaultStore(): DraftStore | null {
  try {
    return typeof localStorage !== 'undefined' ? localStorage : null;
  } catch (err) {
    return null;
  }
}

export function readDrafts(
  store: DraftStore | null = defaultStore(),
): Record<string, string> {
  if (!store) return {};
  try {
    const raw = store.getItem(DRAFT_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return {};
    }
    const drafts: Record<string, string> = {};
    for (const [key, value] of Object.entries(parsed)) {
      if (typeof value === 'string' && value) drafts[key] = value;
    }
    return drafts;
  } catch (err) {
    return {};
  }
}

export function draftText(
  threadId: string,
  store: DraftStore | null = defaultStore(),
): string {
  return readDrafts(store)[draftKey(threadId)] || '';
}

export function saveDraft(
  threadId: string,
  text: string,
  store: DraftStore | null = defaultStore(),
): void {
  if (!store) return;
  const drafts = readDrafts(store);
  const key = draftKey(threadId);
  const value = String(text || '');
  if (!value.trim()) delete drafts[key];
  else drafts[key] = value.slice(0, MAX_DRAFT_CHARS);

  const keys = Object.keys(drafts);
  for (const stale of keys.slice(0, Math.max(0, keys.length - MAX_DRAFTS))) {
    delete drafts[stale];
  }
  try {
    if (keys.length) store.setItem(DRAFT_STORAGE_KEY, JSON.stringify(drafts));
    else store.removeItem(DRAFT_STORAGE_KEY);
  } catch (err) {
    // 隐私模式或配额不足：草稿是增强能力，不能阻断输入。
  }
}

export function clearDraft(
  threadId: string,
  store: DraftStore | null = defaultStore(),
): void {
  saveDraft(threadId, '', store);
}

export function clearAllDrafts(
  store: DraftStore | null = defaultStore(),
): void {
  if (!store) return;
  try {
    store.removeItem(DRAFT_STORAGE_KEY);
  } catch (err) {
    // 同上。
  }
}
