/* 主题设置：强调色预设与 body class 组合。 */

export interface AccentPreset {
  key: string;
  label: string;
  color: string;
}

export const ACCENT_PRESETS: AccentPreset[] = [
  { key: 'blue', label: '蓝', color: '#5b9dff' },
  { key: 'violet', label: '紫', color: '#a78bfa' },
  { key: 'green', label: '绿', color: '#4cc38a' },
  { key: 'amber', label: '琥珀', color: '#e5b567' },
  { key: 'rose', label: '玫红', color: '#f472b6' },
];

export function accentColor(key: unknown): string {
  const preset = ACCENT_PRESETS.find((item) => item.key === key);
  return (preset || ACCENT_PRESETS[0]).color;
}

export function normalizeChoice(
  value: unknown,
  allowed: string[],
  fallback: string,
): string {
  const text = String(value || '');
  return allowed.includes(text) ? text : fallback;
}

export interface ThemeSettings {
  theme: string;
  density: string;
  fontSize: string;
  fontFamily: string;
  contrast: string;
  codeFontSize: string;
  codeLineHeight: string;
}

export function themeBodyClasses(settings: ThemeSettings): string {
  return [
    `theme-${normalizeChoice(settings.theme, ['dark', 'light', 'auto'], 'dark')}`,
    `density-${normalizeChoice(settings.density, ['compact', 'normal'], 'normal')}`,
    `font-${normalizeChoice(settings.fontSize, ['small', 'normal', 'large'], 'normal')}`,
    `family-${normalizeChoice(settings.fontFamily, ['system', 'serif', 'mono'], 'system')}`,
    `contrast-${normalizeChoice(settings.contrast, ['normal', 'high'], 'normal')}`,
    `code-${normalizeChoice(settings.codeFontSize, ['small', 'normal', 'large'], 'normal')}`,
    `code-lh-${normalizeChoice(settings.codeLineHeight, ['compact', 'normal', 'relaxed'], 'normal')}`,
  ].join(' ');
}
