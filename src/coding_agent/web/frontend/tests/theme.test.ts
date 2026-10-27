import { test } from 'node:test';
import assert from 'node:assert/strict';

import {
  ACCENT_PRESETS,
  accentColor,
  themeBodyClasses,
} from '../src/ui/theme';

const BASE = {
  theme: 'dark',
  density: 'normal',
  fontSize: 'normal',
  fontFamily: 'system',
  contrast: 'high',
  codeFontSize: 'large',
  codeLineHeight: 'relaxed',
};

test('accent presets resolve colors and fall back to blue', () => {
  assert.equal(accentColor('violet'), '#a78bfa');
  assert.equal(accentColor('missing'), ACCENT_PRESETS[0].color);
});

test('theme body classes include contrast and code settings', () => {
  assert.equal(
    themeBodyClasses(BASE),
    'theme-dark density-normal font-normal family-system contrast-high code-large code-lh-relaxed',
  );
});

test('invalid theme values fall back to defaults', () => {
  assert.equal(
    themeBodyClasses({
      ...BASE,
      theme: 'neon',
      density: 'huge',
      contrast: 'maximum',
      codeFontSize: 'xxl',
      codeLineHeight: 'wide',
    }),
    'theme-dark density-normal font-normal family-system contrast-normal code-normal code-lh-normal',
  );
});
