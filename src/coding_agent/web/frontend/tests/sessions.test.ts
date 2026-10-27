import { test } from 'node:test';
import assert from 'node:assert/strict';

import { sessionMetaText, sessionPreviewText } from '../src/ui/sessions';

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
