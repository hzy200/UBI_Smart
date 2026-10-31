/* 历史数据缓存：会话列表 + 每个会话的 transcript。
 *
 * 目的只是首屏加速与离线只读：先渲染缓存，再用服务端结果覆盖。
 * 浏览器禁用 IndexedDB 时全部退化为 no-op，不影响主流程。
 * 登出时由 auth 清空，避免跨用户复用。
 */

const DB_NAME = 'codingagent-cache';
const DB_VERSION = 1;
const SESSIONS_STORE = 'sessions';
const TRANSCRIPT_STORE = 'transcripts';
const SESSIONS_KEY = 'list';
const MAX_TRANSCRIPTS = 40;
const MAX_AGE_MS = 7 * 24 * 60 * 60 * 1000;

interface Entry<T> {
  at: number;
  payload: T;
}

let dbPromise: Promise<IDBDatabase | null> | null = null;

function openDb(): Promise<IDBDatabase | null> {
  if (typeof indexedDB === 'undefined') return Promise.resolve(null);
  if (dbPromise) return dbPromise;
  dbPromise = new Promise((resolve) => {
    let request: IDBOpenDBRequest;
    try {
      request = indexedDB.open(DB_NAME, DB_VERSION);
    } catch (err) {
      resolve(null);
      return;
    }
    request.onupgradeneeded = () => {
      const db = request.result;
      if (!db.objectStoreNames.contains(SESSIONS_STORE)) {
        db.createObjectStore(SESSIONS_STORE);
      }
      if (!db.objectStoreNames.contains(TRANSCRIPT_STORE)) {
        db.createObjectStore(TRANSCRIPT_STORE);
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => resolve(null);
    request.onblocked = () => resolve(null);
  });
  return dbPromise;
}

function txDone(tx: IDBTransaction): Promise<void> {
  return new Promise((resolve) => {
    tx.oncomplete = () => resolve();
    tx.onerror = () => resolve();
    tx.onabort = () => resolve();
  });
}

async function put(store: string, key: IDBValidKey, payload: unknown): Promise<void> {
  const db = await openDb();
  if (!db) return;
  try {
    const tx = db.transaction(store, 'readwrite');
    tx.objectStore(store).put({ at: Date.now(), payload }, key);
    await txDone(tx);
  } catch (err) {
    // 缓存是增强能力，写入失败不能影响请求结果。
  }
}

async function read<T>(store: string, key: IDBValidKey): Promise<T | null> {
  const db = await openDb();
  if (!db) return null;
  try {
    const tx = db.transaction(store, 'readonly');
    const request = tx.objectStore(store).get(key);
    const entry = await new Promise<Entry<T> | null>((resolve) => {
      request.onsuccess = () => resolve((request.result as Entry<T>) || null);
      request.onerror = () => resolve(null);
    });
    if (!entry || !entry.payload) return null;
    if (!Number.isFinite(entry.at) || Date.now() - entry.at > MAX_AGE_MS) {
      return null;
    }
    return entry.payload;
  } catch (err) {
    return null;
  }
}

async function remove(store: string, key: IDBValidKey): Promise<void> {
  const db = await openDb();
  if (!db) return;
  try {
    const tx = db.transaction(store, 'readwrite');
    tx.objectStore(store).delete(key);
    await txDone(tx);
  } catch (err) {
    // 同上。
  }
}

async function pruneTranscripts(): Promise<void> {
  const db = await openDb();
  if (!db) return;
  try {
    const readTx = db.transaction(TRANSCRIPT_STORE, 'readonly');
    const store = readTx.objectStore(TRANSCRIPT_STORE);
    const keysRequest = store.getAllKeys();
    const valuesRequest = store.getAll();
    const keys = await new Promise<IDBValidKey[]>((resolve) => {
      keysRequest.onsuccess = () => resolve(keysRequest.result || []);
      keysRequest.onerror = () => resolve([]);
    });
    const values = await new Promise<Entry<unknown>[]>((resolve) => {
      valuesRequest.onsuccess = () => resolve(valuesRequest.result || []);
      valuesRequest.onerror = () => resolve([]);
    });
    if (keys.length <= MAX_TRANSCRIPTS) return;
    const rows = keys.map((key, index) => ({
      key,
      at: Number(values[index] && values[index].at) || 0,
    })).sort((a, b) => a.at - b.at);
    const tx = db.transaction(TRANSCRIPT_STORE, 'readwrite');
    for (const row of rows.slice(0, rows.length - MAX_TRANSCRIPTS)) {
      tx.objectStore(TRANSCRIPT_STORE).delete(row.key);
    }
    await txDone(tx);
  } catch (err) {
    // 同上。
  }
}

export async function readCachedSessions(): Promise<any | null> {
  return read<any>(SESSIONS_STORE, SESSIONS_KEY);
}

export async function writeCachedSessions(payload: any): Promise<void> {
  if (!payload || !Array.isArray(payload.sessions)) return;
  await put(SESSIONS_STORE, SESSIONS_KEY, payload);
}

export async function readCachedTranscript(threadId: string): Promise<any | null> {
  if (!threadId) return null;
  return read<any>(TRANSCRIPT_STORE, threadId);
}

export async function writeCachedTranscript(
  threadId: string,
  payload: any,
): Promise<void> {
  if (!threadId || !payload) return;
  await put(TRANSCRIPT_STORE, threadId, payload);
  void pruneTranscripts();
}

export async function deleteCachedTranscript(threadId: string): Promise<void> {
  if (!threadId) return;
  await remove(TRANSCRIPT_STORE, threadId);
}

export async function clearHistoryCache(): Promise<void> {
  const db = await openDb();
  if (!db) return;
  try {
    const tx = db.transaction(
      [SESSIONS_STORE, TRANSCRIPT_STORE],
      'readwrite',
    );
    tx.objectStore(SESSIONS_STORE).clear();
    tx.objectStore(TRANSCRIPT_STORE).clear();
    await txDone(tx);
  } catch (err) {
    // 同上。
  }
}
