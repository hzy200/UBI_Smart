import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  MESSAGES,
  flattenKeys,
  normalizeLocale,
  translate,
} from '../src/i18n';

test('zh-CN and en-US expose the same key set', () => {
  const zh = flattenKeys(MESSAGES['zh-CN']).sort();
  const en = flattenKeys(MESSAGES['en-US']).sort();
  assert.deepEqual(en, zh);
});

test('translate interpolates parameters', () => {
  assert.equal(
    translate('zh-CN', 'sidebar.selected', { count: 3 }),
    '3 条已选',
  );
  assert.equal(
    translate('en-US', 'sidebar.selected', { count: 3 }),
    '3 selected',
  );
  assert.equal(
    translate('en-US', 'notifications.unread', { count: 2 }),
    '2 unread',
  );
});

test('translate falls back to the key for unknown paths', () => {
  assert.equal(translate('en-US', 'missing.key'), 'missing.key');
  assert.equal(translate('zh-CN', ''), '');
});

test('locale aliases normalize to zh-CN', () => {
  assert.equal(normalizeLocale('en-US'), 'en-US');
  assert.equal(normalizeLocale('zh-CN'), 'zh-CN');
  assert.equal(normalizeLocale('fr-FR'), 'zh-CN');
  assert.equal(normalizeLocale(''), 'zh-CN');
});
