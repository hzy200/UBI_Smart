import { test } from 'node:test';
import assert from 'node:assert/strict';

import { memoize } from '../src/ui/memo';

test('memoize caches until the key changes', () => {
  const slot = { key: '', value: [] as number[] };
  let calls = 0;
  const compute = () => {
    calls += 1;
    return [calls];
  };
  const first = memoize(slot, 'a', compute);
  const second = memoize(slot, 'a', compute);
  assert.equal(first, second);
  assert.equal(calls, 1);
  const third = memoize(slot, 'b', compute);
  assert.notEqual(third, first);
  assert.equal(calls, 2);
});
