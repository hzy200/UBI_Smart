import { test } from 'node:test';
import assert from 'node:assert/strict';

import { normalizeClientError } from '../src/ui/error-report';

test('normalizeClientError keeps a bounded summary', () => {
  const error = new Error('x'.repeat(600));
  error.stack = 's'.repeat(5000);
  const report = normalizeClientError('error', error, '/app');
  assert.ok(report);
  assert.equal(report?.message.length, 500);
  assert.equal(report?.stack.length, 4000);
  assert.equal(report?.url, '/app');
  assert.equal(report?.kind, 'error');
  assert.match(report?.at || '', /^\d{4}-\d{2}-\d{2}T/);
});

test('normalizeClientError ignores empty and aborted errors', () => {
  assert.equal(normalizeClientError('error', '', '/'), null);
  assert.equal(normalizeClientError('error', new Error(''), '/'), null);
  const aborted = new Error('cancelled');
  aborted.name = 'AbortError';
  assert.equal(normalizeClientError('error', aborted, '/'), null);
});

test('normalizeClientError accepts string rejections', () => {
  const report = normalizeClientError('unhandledrejection', 'boom', '/x');
  assert.equal(report?.message, 'boom');
  assert.equal(report?.stack, '');
});
