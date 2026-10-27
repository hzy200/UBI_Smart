import { test } from 'node:test';
import assert from 'node:assert/strict';

import { uploadPercent } from '../src/ui/upload';

test('uploadPercent clamps and handles unknown totals', () => {
  assert.equal(uploadPercent(0, 100), 0);
  assert.equal(uploadPercent(50, 100), 50);
  assert.equal(uploadPercent(100, 100), 100);
  assert.equal(uploadPercent(150, 100), 100);
  assert.equal(uploadPercent(1, 0), 0);
  assert.equal(uploadPercent(1, Number.NaN), 0);
});
