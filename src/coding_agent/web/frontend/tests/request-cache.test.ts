import { test } from 'node:test';
import assert from 'node:assert/strict';

import { RequestCache } from '../src/api/cache';

test('concurrent GETs share one in-flight request', async () => {
  let calls = 0;
  let release: (value: string) => void = () => {};
  const cache = new RequestCache();
  const loader = () => {
    calls += 1;
    return new Promise<string>((resolve) => { release = resolve; });
  };

  const first = cache.run('same', loader, { ttlMs: 100 });
  const second = cache.run('same', loader, { ttlMs: 100 });
  release('value');

  assert.equal(await first, 'value');
  assert.equal(await second, 'value');
  assert.equal(calls, 1);
});

test('TTL hits reuse the cached value and expire afterwards', async () => {
  let now = 1000;
  let calls = 0;
  const cache = new RequestCache(() => now);
  const loader = async () => {
    calls += 1;
    return calls;
  };

  assert.equal(await cache.run('k', loader, { ttlMs: 100 }), 1);
  now += 50;
  assert.equal(await cache.run('k', loader, { ttlMs: 100 }), 1);
  now += 100;
  assert.equal(await cache.run('k', loader, { ttlMs: 100 }), 2);
  assert.equal(calls, 2);
});

test('force bypasses a fresh entry', async () => {
  let calls = 0;
  const cache = new RequestCache();
  const loader = async () => {
    calls += 1;
    return calls;
  };

  assert.equal(await cache.run('k', loader, { ttlMs: 60_000 }), 1);
  assert.equal(await cache.run('k', loader, { ttlMs: 60_000, force: true }), 2);
});

test('force starts a fresh request instead of joining an older one', async () => {
  let calls = 0;
  const releases: Array<(value: string) => void> = [];
  const cache = new RequestCache();
  const loader = () => new Promise<string>((resolve) => {
    calls += 1;
    releases.push(resolve);
  });

  const first = cache.run('k', loader, { ttlMs: 0 });
  const second = cache.run('k', loader, { ttlMs: 0, force: true });
  assert.equal(calls, 2);
  releases[0]('old');
  releases[1]('new');
  assert.equal(await first, 'old');
  assert.equal(await second, 'new');
});

test('invalidate matches prefixes and clear removes everything', async () => {
  const cache = new RequestCache();
  let calls = 0;
  const loader = async (key: string) => {
    calls += 1;
    return `${key}-${calls}`;
  };

  await cache.run('/api/sessions?a', () => loader('a'), { ttlMs: 60_000 });
  await cache.run('/api/providers', () => loader('p'), { ttlMs: 60_000 });
  cache.invalidate('/api/sessions');
  assert.equal(
    await cache.run('/api/sessions?a', () => loader('a'), { ttlMs: 60_000 }),
    'a-3',
  );

  cache.clear();
  assert.equal(
    await cache.run('/api/providers', () => loader('p'), { ttlMs: 60_000 }),
    'p-4',
  );
});

test('invalidate prevents an in-flight response from repopulating', async () => {
  let calls = 0;
  let release: (value: string) => void = () => {};
  const cache = new RequestCache();
  const loader = () => {
    calls += 1;
    return new Promise<string>((resolve) => { release = resolve; });
  };

  const pending = cache.run('k', loader, { ttlMs: 60_000 });
  cache.invalidate('k');
  release('stale');
  assert.equal(await pending, 'stale');

  const next = cache.run('k', loader, { ttlMs: 60_000 });
  release('fresh');
  assert.equal(await next, 'fresh');
  assert.equal(calls, 2);
});

test('a rejected loader is not cached', async () => {
  let calls = 0;
  const cache = new RequestCache();
  const loader = async () => {
    calls += 1;
    if (calls === 1) throw new Error('boom');
    return 'ok';
  };

  await assert.rejects(cache.run('k', loader, { ttlMs: 60_000 }), /boom/);
  assert.equal(await cache.run('k', loader, { ttlMs: 60_000 }), 'ok');
  assert.equal(calls, 2);
});
