import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  DRAFT_STORAGE_KEY,
  clearAllDrafts,
  clearDraft,
  draftKey,
  draftText,
  readDrafts,
  saveDraft,
} from '../src/state/drafts';

function fakeStore() {
  const map = new Map<string, string>();
  return {
    getItem(key: string) {
      return map.has(key) ? String(map.get(key)) : null;
    },
    setItem(key: string, value: string) {
      map.set(key, String(value));
    },
    removeItem(key: string) {
      map.delete(key);
    },
  };
}

test('draft key falls back to __new__ for an unsaved session', () => {
  assert.equal(draftKey('thread-1'), 'thread-1');
  assert.equal(draftKey(''), '__new__');
});

test('drafts are stored per thread and empty text removes them', () => {
  const store = fakeStore();
  saveDraft('a', 'A 的草稿', store);
  saveDraft('b', 'B 的草稿', store);
  assert.equal(draftText('a', store), 'A 的草稿');
  assert.equal(draftText('b', store), 'B 的草稿');
  assert.equal(draftText('missing', store), '');

  saveDraft('a', '   ', store);
  assert.equal(draftText('a', store), '');
  assert.equal(draftText('b', store), 'B 的草稿');
});

test('invalid or malformed storage is ignored', () => {
  const store = fakeStore();
  store.setItem(DRAFT_STORAGE_KEY, '{not json');
  assert.deepEqual(readDrafts(store), {});
  store.setItem(DRAFT_STORAGE_KEY, JSON.stringify(['x']));
  assert.deepEqual(readDrafts(store), {});
  store.setItem(DRAFT_STORAGE_KEY, JSON.stringify({ a: 1, b: 'ok' }));
  assert.deepEqual(readDrafts(store), { b: 'ok' });
});

test('drafts are capped by count and per-draft length', () => {
  const store = fakeStore();
  for (let index = 0; index < 55; index += 1) {
    saveDraft(`t${index}`, `draft-${index}`, store);
  }
  const drafts = readDrafts(store);
  assert.equal(Object.keys(drafts).length, 50);
  assert.equal(drafts.t0, undefined);
  assert.equal(drafts.t54, 'draft-54');

  saveDraft('long', 'x'.repeat(25_000), store);
  assert.equal(draftText('long', store).length, 20_000);
});

test('clearDraft and clearAllDrafts remove persisted text', () => {
  const store = fakeStore();
  saveDraft('a', 'A', store);
  clearDraft('a', store);
  assert.equal(draftText('a', store), '');
  saveDraft('b', 'B', store);
  clearAllDrafts(store);
  assert.deepEqual(readDrafts(store), {});
});
