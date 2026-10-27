import { test } from 'node:test';
import assert from 'node:assert/strict';

import { tailSlice } from '../src/ui/window';

test('tailSlice returns the newest items', () => {
  assert.deepEqual(tailSlice([1, 2, 3, 4, 5], 3), [3, 4, 5]);
  assert.deepEqual(tailSlice([1, 2], 10), [1, 2]);
  assert.deepEqual(tailSlice([1, 2, 3], 0), []);
  assert.deepEqual(tailSlice([1, 2, 3], -2), []);
});
