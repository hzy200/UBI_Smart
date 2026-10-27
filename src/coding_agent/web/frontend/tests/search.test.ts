import { test } from 'node:test';
import assert from 'node:assert/strict';

import { includesQuery, matchesAllTokens } from '../src/ui/search';

test('includesQuery is case-insensitive and empty-safe', () => {
  assert.equal(includesQuery('SQLite is great', 'sql'), true);
  assert.equal(includesQuery('SQLite is great', '  GREAT '), true);
  assert.equal(includesQuery('SQLite is great', 'missing'), false);
  assert.equal(includesQuery('SQLite is great', ''), true);
});

test('matchesAllTokens requires every token', () => {
  assert.equal(matchesAllTokens('SQLite is great', 'sql great'), true);
  assert.equal(matchesAllTokens('SQLite is great', 'sql missing'), false);
  assert.equal(matchesAllTokens('anything', '   '), true);
});
