import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  fuzzyScore,
  includesQuery,
  matchesAllTokens,
  parseCommandQuery,
} from '../src/ui/search';

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

test('parseCommandQuery handles prefixes', () => {
  assert.deepEqual(parseCommandQuery('>theme'), { kind: 'command', query: 'theme' });
  assert.deepEqual(parseCommandQuery('@login'), { kind: 'session', query: 'login' });
  assert.deepEqual(parseCommandQuery('#main.py'), { kind: 'file', query: 'main.py' });
  assert.deepEqual(parseCommandQuery('plain'), { kind: 'all', query: 'plain' });
});

test('fuzzyScore prefers direct matches and rejects absent queries', () => {
  assert.ok(fuzzyScore('open settings', 'settings') > 0);
  assert.ok(fuzzyScore('open settings', 'opst') > 0);
  assert.equal(fuzzyScore('open settings', 'zzz'), -1);
  assert.equal(fuzzyScore('open settings', ''), 0);
});
