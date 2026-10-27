import { test } from 'node:test';
import assert from 'node:assert/strict';

import { outputPage } from '../src/ui/output';

test('short output is returned unchanged', () => {
  const page = outputPage('a\nb', 40);
  assert.equal(page.text, 'a\nb');
  assert.equal(page.truncated, false);
  assert.equal(page.hint, '');
});

test('long output is cut to the line limit with a hint', () => {
  const text = Array.from({ length: 120 }, (_, index) => `line ${index}`).join('\n');
  const page = outputPage(text, 40);
  assert.equal(page.total, 120);
  assert.equal(page.shown, 40);
  assert.equal(page.hidden, 80);
  assert.equal(page.truncated, true);
  assert.equal(page.text.split('\n').length, 40);
  assert.equal(page.hint, '显示 40/120 行');
});

test('output page handles empty input and invalid limits', () => {
  assert.equal(outputPage('').total, 1);
  const page = outputPage('a\nb', 0);
  assert.equal(page.shown, 2);
  assert.equal(outputPage(null, 1).text, '');
});
