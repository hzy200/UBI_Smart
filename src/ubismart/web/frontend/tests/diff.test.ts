import { test } from 'node:test';
import assert from 'node:assert/strict';

import { diffRowsFromText, splitDiffRows, wordDiffMarkers } from '../src/ui/diff';

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

test('diff rows expose kind, sign and code', () => {
  const rows = diffRowsFromText([
    '@@ -1,1 +1,1 @@',
    '-old',
    '+new',
  ].join('\n'));
  assert.equal(rows[1]._kind, 'del');
  assert.equal(rows[1]._sign, '-');
  assert.equal(rows[1]._code, 'old');
  assert.equal(rows[2]._kind, 'add');
  assert.equal(rows[2]._sign, '+');
  assert.equal(rows[2]._code, 'new');
});

test('split diff pairs deletions and additions', () => {
  const decorated = diffRowsFromText('-a\n+b\n-c').map((row) => ({
    ...row,
    _html: row._code,
  }));
  const rows = splitDiffRows(decorated);
  assert.equal(rows[0]._leftHtml, 'a');
  assert.equal(rows[0]._rightHtml, 'b');
  assert.equal(rows[1]._leftHtml, 'c');
  assert.equal(rows[1]._hasRight, false);
});

test('word diff marks changed tokens', () => {
  const marks = wordDiffMarkers('hello old world', 'hello new world');
  assert.ok(marks.old.includes('\u0001old\u0002'));
  assert.ok(marks.new.includes('\u0001new\u0002'));
  assert.ok(!marks.old.includes('\u0001hello'));
});
