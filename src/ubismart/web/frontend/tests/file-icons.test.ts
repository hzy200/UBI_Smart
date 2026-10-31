import { test } from 'node:test';
import assert from 'node:assert/strict';

import { fileIcon, treeGuides } from '../src/ui/file-icons';

test('file icons map common extensions', () => {
  assert.equal(fileIcon('main.py', false)._iconId, '#i-file-code');
  assert.match(fileIcon('main.py', false)._iconCls, /icon-py/);
  assert.equal(fileIcon('app.ts', false)._iconCls, 'tree-icon icon-ts');
  assert.equal(fileIcon('data.json', false)._iconId, '#i-file-data');
  assert.equal(fileIcon('README.md', false)._iconId, '#i-file-text');
  assert.equal(fileIcon('logo.png', false)._iconId, '#i-file-image');
  assert.equal(fileIcon('a.unknown', false)._iconId, '#i-file');
});

test('folders switch icon when expanded and guides follow depth', () => {
  assert.equal(fileIcon('src', true, false)._iconId, '#i-folder');
  assert.equal(fileIcon('src', true, true)._iconId, '#i-folder-open');
  assert.deepEqual(treeGuides(0), []);
  assert.deepEqual(
    treeGuides(2).map((row) => row._style),
    ['left:14px', 'left:28px'],
  );
});
