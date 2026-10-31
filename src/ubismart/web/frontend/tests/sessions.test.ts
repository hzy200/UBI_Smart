import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  mergeSessionPayloads,
  sessionMetaText,
  sessionPayloadHasMore,
  sessionPreviewText,
} from '../src/ui/sessions';

test('session preview collapses whitespace and truncates', () => {
  assert.equal(sessionPreviewText('  hello\n\nworld  '), 'hello world');
  assert.equal(sessionPreviewText(''), '');
  assert.equal(sessionPreviewText('abcdefghij', 6), 'abcde…');
});

test('session meta text shows time, counts and flags', () => {
  assert.equal(
    sessionMetaText({
      last_active: '2026-10-27T12:34:56+00:00',
      prompts: 3,
      tool_calls: 5,
      archived: true,
      interrupted: true,
    }),
    '2026-10-27 12:34 · 3 轮 · 5 次工具 · 已归档 · 异常中断',
  );
  assert.equal(sessionMetaText({}), '');
});

test('mergeSessionPayloads combines active and archived without duplicates', () => {
  const merged = mergeSessionPayloads(
    { sessions: [{ thread_id: 'a' }, { thread_id: 'b' }], total: 2 },
    { sessions: [{ thread_id: 'b' }, { thread_id: 'c' }], total: 2 },
  );
  assert.deepEqual(
    merged.sessions.map((row) => row.thread_id),
    ['a', 'b', 'c'],
  );
  assert.deepEqual(mergeSessionPayloads(null, {}).sessions, []);
});

test('sessionPayloadHasMore compares total with loaded rows', () => {
  assert.equal(
    sessionPayloadHasMore(
      { sessions: [{ thread_id: 'a' }], total: 3 },
      { sessions: [], total: 0 },
    ),
    true,
  );
  assert.equal(
    sessionPayloadHasMore(
      { sessions: [{ thread_id: 'a' }], total: 1 },
      { sessions: [{ thread_id: 'b' }], total: 1 },
    ),
    false,
  );
  // 旧后端没有 total 时不谎报还有更多。
  assert.equal(sessionPayloadHasMore({ sessions: [] }, {}), false);
});
