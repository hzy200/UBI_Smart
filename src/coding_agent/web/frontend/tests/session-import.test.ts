import { test } from 'node:test';
import assert from 'node:assert/strict';

import { normalizeSessionImport } from '../src/ui/session-import';

test('normalizeSessionImport keeps messages and transcript', () => {
  const payload = normalizeSessionImport({
    title: '导入的会话',
    messages: [{ role: 'user', text: 'hi' }, 'bad'],
    transcript: [{ role: 'assistant', text: 'hello' }],
  });
  assert.equal(payload.title, '导入的会话');
  assert.deepEqual(payload.messages, [{ role: 'user', text: 'hi' }]);
  assert.deepEqual(payload.transcript, [{ role: 'assistant', text: 'hello' }]);
});

test('normalizeSessionImport rejects invalid payloads', () => {
  assert.throws(() => normalizeSessionImport(null), /不是对象/);
  assert.throws(() => normalizeSessionImport([]), /不是对象/);
  assert.throws(() => normalizeSessionImport({ messages: [] }), /没有消息/);
});

test('normalizeSessionImport tolerates a transcript-only export', () => {
  const payload = normalizeSessionImport({
    transcript: [{ role: 'user', text: 'only transcript' }],
  });
  assert.deepEqual(payload.messages, []);
  assert.equal(payload.transcript.length, 1);
  assert.equal(payload.title, '');
});
