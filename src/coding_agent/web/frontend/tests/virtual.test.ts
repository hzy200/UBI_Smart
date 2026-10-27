import { test } from 'node:test';
import assert from 'node:assert/strict';

import { computeVirtualRange } from '../src/ui/virtual';

test('virtual range covers the viewport with overscan', () => {
  const heights = [100, 100, 100, 100, 100];
  const range = computeVirtualRange(heights, 150, 100, 1);
  assert.equal(range.start, 0);
  assert.equal(range.end, 4);
  assert.equal(range.top, 0);
  assert.equal(range.bottom, 100);
});

test('virtual range reports spacers for long lists', () => {
  const heights = Array.from({ length: 10 }, () => 100);
  const range = computeVirtualRange(heights, 500, 200, 2);
  assert.equal(range.start, 3);
  assert.equal(range.end, 9);
  assert.equal(range.top, 300);
  assert.equal(range.bottom, 100);
});

test('virtual range handles empty and zero viewport', () => {
  assert.deepEqual(computeVirtualRange([], 0, 500), {
    start: 0, end: 0, top: 0, bottom: 0,
  });
  const range = computeVirtualRange([50, 50, 50], 0, 0, 0);
  assert.equal(range.start, 0);
  assert.equal(range.end, 0);
  assert.equal(range.bottom, 150);
});
