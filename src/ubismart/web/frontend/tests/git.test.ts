import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  buildGitStatusMap,
  gitTreeFields,
  normalizeGitStatus,
} from '../src/ui/git';

test('normalizeGitStatus collapses porcelain codes', () => {
  assert.equal(normalizeGitStatus('M'), 'M');
  assert.equal(normalizeGitStatus('MM'), 'M');
  assert.equal(normalizeGitStatus('??'), 'U');
  assert.equal(normalizeGitStatus('A '), 'A');
  assert.equal(normalizeGitStatus(' D'), 'D');
  assert.equal(normalizeGitStatus('R '), 'R');
  assert.equal(normalizeGitStatus(''), '');
});

test('buildGitStatusMap keeps the last status per path', () => {
  const map = buildGitStatusMap([
    { status: 'M', path: 'src/a.py' },
    { status: '??', path: 'new.txt' },
    { status: 'A', path: 'src/b.ts' },
  ]);
  assert.deepEqual(map, {
    'src/a.py': 'M',
    'new.txt': 'U',
    'src/b.ts': 'A',
  });
});

test('gitTreeFields marks files and aggregates directories', () => {
  const map = { 'src/a.py': 'M', 'src/nested/b.ts': 'A', 'root.md': 'D' };
  assert.equal(gitTreeFields(map, 'src/a.py', false)._git, 'M');
  assert.equal(gitTreeFields(map, 'src/a.py', false)._gitCls, 'tree-git git-m');
  assert.equal(gitTreeFields(map, 'src/nested/b.ts', false)._git, 'A');
  assert.equal(gitTreeFields(map, 'root.md', false)._git, 'D');
  assert.equal(gitTreeFields(map, 'unknown.txt', false)._git, '');

  const dir = gitTreeFields(map, 'src', true);
  assert.equal(dir._git, '●');
  assert.equal(dir._gitCls, 'tree-git git-dir');
  assert.equal(dir._gitTitle, '2 个未提交改动');
  assert.equal(gitTreeFields(map, '', true)._gitTitle, '3 个未提交改动');
  assert.equal(gitTreeFields(map, 'empty', true)._git, '');
});
