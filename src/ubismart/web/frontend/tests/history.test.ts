import { test } from 'node:test';
import assert from 'node:assert/strict';

import { prependOlder } from '../src/ui/history';

test('prependOlder keeps chronological order and handles empty sides', () => {
  assert.deepEqual(prependOlder([1, 2], [3, 4]), [1, 2, 3, 4]);
  assert.deepEqual(prependOlder([], [3]), [3]);
  assert.deepEqual(prependOlder([1], []), [1]);
  assert.deepEqual(prependOlder([], []), []);
});
