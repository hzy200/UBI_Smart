import { test } from 'node:test';
import assert from 'node:assert/strict';

import { buildSessionUrl, parseSessionUrl } from '../src/ui/url-state';

test('parseSessionUrl reads session and split', () => {
  assert.deepEqual(parseSessionUrl(''), { session: '', split: '' });
  assert.deepEqual(
    parseSessionUrl('?session=thread-1&split=thread-2'),
    { session: 'thread-1', split: 'thread-2' },
  );
  assert.deepEqual(
    parseSessionUrl('?session=%20thread-1%20'),
    { session: 'thread-1', split: '' },
  );
});

test('buildSessionUrl keeps unrelated params and drops empty values', () => {
  assert.equal(
    buildSessionUrl('/', '?foo=1&session=old', { session: 'new', split: '' }),
    '/?foo=1&session=new',
  );
  assert.equal(
    buildSessionUrl('/', '?session=old', { session: '', split: '' }),
    '/',
  );
});

test('buildSessionUrl never duplicates the split into the main session', () => {
  assert.equal(
    buildSessionUrl('/', '', { session: 'same', split: 'same' }),
    '/?session=same',
  );
  assert.equal(
    buildSessionUrl('/app', '?split=old', { session: '', split: '' }),
    '/app',
  );
});
