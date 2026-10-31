import { test } from 'node:test';
import assert from 'node:assert/strict';

import { isWindowsPath, isWslPath } from '../src/ui/paths';

test('path helpers distinguish WSL and Windows roots', () => {
  assert.equal(isWslPath('/mnt/d/proj'), true);
  assert.equal(isWslPath('D:\\proj'), false);
  assert.equal(isWindowsPath('D:\\proj'), true);
  assert.equal(isWindowsPath('D:/proj'), true);
  assert.equal(isWindowsPath('\\\\server\\share'), true);
  assert.equal(isWindowsPath('/mnt/d/proj'), false);
  assert.equal(isWindowsPath(''), false);
});
