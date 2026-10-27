import 'fake-indexeddb/auto';

import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  clearHistoryCache,
  deleteCachedTranscript,
  readCachedSessions,
  readCachedTranscript,
  writeCachedSessions,
  writeCachedTranscript,
} from '../src/state/history-cache';

test('session list cache round-trips', async () => {
  await clearHistoryCache();
  assert.equal(await readCachedSessions(), null);

  const payload = { sessions: [{ thread_id: 'a', title: 'A' }] };
  await writeCachedSessions(payload);
  assert.deepEqual(await readCachedSessions(), payload);
});

test('invalid session payloads are ignored', async () => {
  await clearHistoryCache();
  await writeCachedSessions({ sessions: 'nope' });
  assert.equal(await readCachedSessions(), null);
});

test('transcript cache is isolated by thread and can be deleted', async () => {
  await clearHistoryCache();
  await writeCachedTranscript('a', { transcript: [{ role: 'user', text: 'A' }] });
  await writeCachedTranscript('b', { transcript: [{ role: 'user', text: 'B' }] });

  assert.deepEqual(
    await readCachedTranscript('a'),
    { transcript: [{ role: 'user', text: 'A' }] },
  );
  assert.deepEqual(
    await readCachedTranscript('b'),
    { transcript: [{ role: 'user', text: 'B' }] },
  );
  assert.equal(await readCachedTranscript('missing'), null);

  await deleteCachedTranscript('a');
  assert.equal(await readCachedTranscript('a'), null);
  assert.deepEqual(
    await readCachedTranscript('b'),
    { transcript: [{ role: 'user', text: 'B' }] },
  );
});

test('clearHistoryCache removes both stores', async () => {
  await writeCachedSessions({ sessions: [{ thread_id: 'a' }] });
  await writeCachedTranscript('a', { messages: [{ role: 'user', text: 'A' }] });
  await clearHistoryCache();
  assert.equal(await readCachedSessions(), null);
  assert.equal(await readCachedTranscript('a'), null);
});
