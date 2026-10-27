import { test } from 'node:test';
import assert from 'node:assert/strict';

import { computeTabTarget } from '../src/ui/focus';

test('tab target wraps forward and backward', () => {
  assert.equal(computeTabTarget(3, 0, false), 1);
  assert.equal(computeTabTarget(3, 2, false), 0);
  assert.equal(computeTabTarget(3, 0, true), 2);
  assert.equal(computeTabTarget(3, 1, true), 0);
});

test('tab target enters the first or last item when focus is outside', () => {
  assert.equal(computeTabTarget(4, -1, false), 0);
  assert.equal(computeTabTarget(4, -1, true), 3);
  assert.equal(computeTabTarget(4, 9, false), 0);
});

test('tab target handles an empty focus list', () => {
  assert.equal(computeTabTarget(0, 0, false), -1);
  assert.equal(computeTabTarget(0, 0, true), -1);
});
