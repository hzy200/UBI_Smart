import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  matchesSettingsQuery,
  settingsVisibility,
} from '../src/ui/settings';

test('settings query matches Chinese and English keywords', () => {
  assert.equal(matchesSettingsQuery('主题', ['外观', '主题']), true);
  assert.equal(matchesSettingsQuery('PROVIDER', ['provider']), true);
  assert.equal(matchesSettingsQuery('日志', ['诊断', '日志']), true);
  assert.equal(matchesSettingsQuery('不存在', ['外观', '主题']), false);
  assert.equal(matchesSettingsQuery('  ', ['anything']), true);
});

test('settings visibility marks every section when the query is empty', () => {
  const visible = settingsVisibility('');
  assert.equal(Object.values(visible).every(Boolean), true);
});

test('settings visibility narrows to matching sections', () => {
  const visible = settingsVisibility('主题');
  assert.equal(visible.appearance, true);
  assert.equal(visible.models, false);
  assert.equal(visible.workspace, false);
  const model = settingsVisibility('模型');
  assert.equal(model.appearance, false);
  assert.equal(model.models, true);
});
