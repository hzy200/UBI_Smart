/* GET 响应缓存：in-flight 去重 + 可选短 TTL。
 *
 * 只缓存解析后的 JSON，不缓存 Response；SSE、POST/DELETE 一律不经过这里。
 * invalidate/clear 会让此前发起的请求不再写回缓存，避免登出或变更后的
 * 旧响应重新污染缓存。
 */

export interface CacheOptions {
  ttlMs?: number;
  force?: boolean;
}

interface CacheEntry {
  at: number;
  value: unknown;
}

export class RequestCache {
  private entries = new Map<string, CacheEntry>();
  private inflight = new Map<string, Promise<unknown>>();
  private generation = 0;

  constructor(private now: () => number = () => Date.now()) {}

  async run<T>(
    key: string,
    loader: () => Promise<T>,
    options: CacheOptions = {},
  ): Promise<T> {
    const ttlMs = Math.max(0, Number(options.ttlMs || 0));
    if (!options.force && ttlMs > 0) {
      const hit = this.entries.get(key);
      if (hit && this.now() - hit.at <= ttlMs) return hit.value as T;
    }

    // force 不能复用变更前发起的请求，否则会把旧数据带回来。
    const pending = options.force ? null : this.inflight.get(key);
    if (pending) return pending as Promise<T>;

    const generation = this.generation;
    let task: Promise<T>;
    task = (async () => {
      const value = await loader();
      if (
        ttlMs > 0
        && generation === this.generation
        && this.inflight.get(key) === task
      ) {
        this.entries.set(key, { at: this.now(), value });
      }
      return value;
    })();
    this.inflight.set(key, task);
    try {
      return await task;
    } finally {
      if (this.inflight.get(key) === task) this.inflight.delete(key);
    }
  }

  invalidate(prefix = ''): void {
    this.generation += 1;
    for (const key of Array.from(this.entries.keys())) {
      if (!prefix || key.startsWith(prefix)) this.entries.delete(key);
    }
  }

  clear(): void {
    this.generation += 1;
    this.entries.clear();
    this.inflight.clear();
  }
}

export const responseCache = new RequestCache();
