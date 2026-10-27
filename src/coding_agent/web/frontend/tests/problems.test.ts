import { test } from 'node:test';
import assert from 'node:assert/strict';

import { groupProblems, problemGroupKey } from '../src/ui/problems';

function problem(id: number, kind: string, severity: string, title: string, path = '') {
  return {
    id,
    kind,
    severity,
    title,
    _path: path,
    _cls: `problem-${severity}${path ? ' problem-clickable' : ''}`,
  };
}

test('problems group by file path and fall back to source kind', () => {
  const rows = [
    problem(1, 'review', 'error', 'bad import', 'src/a.py'),
    problem(2, 'review', 'warning', 'slow loop', 'src/a.py'),
    problem(3, 'verification', 'warning', 'tests failed', ''),
    problem(4, 'run', 'error', 'crash', ''),
  ];
  assert.equal(problemGroupKey(rows[0]), 'file:src/a.py');
  assert.equal(problemGroupKey(rows[2]), 'kind:verification');

  const groups = groupProblems(rows);
  assert.equal(groups.length, 3);
  assert.equal(groups[0]._label, 'src/a.py');
  assert.equal(groups[0]._count, 2);
  assert.equal(groups[0]._errors, 1);
  assert.equal(groups[0]._warnings, 1);
  assert.equal(groups[0]._countText, '1 错误 / 1 警告');
  assert.equal(groups[0]._items[0]._itemCls, 'problem-error problem-clickable');
  assert.equal(groups[1]._label, '验证');
  assert.equal(groups[2]._label, '运行');
});

test('problem groups honour explicit collapse state', () => {
  const rows = [
    problem(1, 'review', 'error', 'a', 'src/a.py'),
    problem(2, 'review', 'error', 'b', 'src/b.py'),
  ];
  const groups = groupProblems(rows, { 'file:src/a.py': false });
  assert.equal(groups[0]._open, false);
  assert.equal(groups[0]._toggle, '▸');
  assert.equal(groups[1]._open, true);
  assert.equal(groups[1]._toggle, '▾');
  assert.equal(groups[0]._cls, 'problem-group has-error');
});

test('problem grouping tolerates empty and null rows', () => {
  assert.deepEqual(groupProblems([]), []);
  assert.deepEqual(groupProblems(null), []);
});
