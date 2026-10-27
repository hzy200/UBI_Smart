import { test } from 'node:test';
import assert from 'node:assert/strict';

import { diffRowsFromText } from '../src/ui/diff';

test('diff rows classify file headers, hunks and changes', () => {
  const rows = diffRowsFromText([
    '--- a/a.py',
    '+++ b/a.py',
    '@@ -10,2 +20,2 @@',
    ' context',
    '-before',
    '+after',
    ' tail',
  ].join('\n'));

  assert.equal(rows[0]._cls, 'diff-line diff-file');
  assert.equal(rows[1]._cls, 'diff-line diff-file');
  assert.equal(rows[2]._cls, 'diff-line diff-hunk');
  assert.equal(rows[3]._no, '20');
  assert.equal(rows[4]._cls, 'diff-line diff-del');
  assert.equal(rows[4]._no, '-11');
  assert.equal(rows[5]._cls, 'diff-line diff-add');
  assert.equal(rows[5]._no, '+21');
  assert.equal(rows[6]._no, '22');
});

test('diff parser handles empty and metadata lines', () => {
  assert.deepEqual(diffRowsFromText(''), []);
  const rows = diffRowsFromText('\\ No newline at end of file');
  assert.equal(rows[0]._cls, 'diff-line diff-meta');
  assert.equal(rows[0]._no, '');
});
