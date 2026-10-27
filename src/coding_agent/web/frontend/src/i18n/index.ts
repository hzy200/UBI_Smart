import { zhCN } from './zh-CN';
import type { Messages } from './zh-CN';
import { enUS } from './en-US';

export type Locale = 'zh-CN' | 'en-US';
export const DEFAULT_LOCALE: Locale = 'zh-CN';

export const MESSAGES: Record<Locale, Messages> = {
  'zh-CN': zhCN,
  'en-US': enUS,
};

export function normalizeLocale(value: unknown): Locale {
  return String(value || '') === 'en-US' ? 'en-US' : DEFAULT_LOCALE;
}

export function lookup(messages: Messages, key: string): string {
  let node: any = messages;
  for (const part of String(key || '').split('.')) {
    if (!part || !node || typeof node !== 'object') return '';
    node = node[part];
  }
  return typeof node === 'string' ? node : '';
}

export function translate(
  locale: Locale,
  key: string,
  params: Record<string, unknown> = {},
): string {
  const value = lookup(MESSAGES[locale], key)
    || lookup(MESSAGES[DEFAULT_LOCALE], key)
    || key;
  return value.replace(/\{(\w+)\}/g, (match, name) => (
    Object.prototype.hasOwnProperty.call(params, name)
      ? String(params[name])
      : match
  ));
}

export function flattenKeys(
  value: unknown,
  prefix = '',
  keys: string[] = [],
): string[] {
  if (value && typeof value === 'object') {
    for (const [key, child] of Object.entries(value)) {
      flattenKeys(child, prefix ? `${prefix}.${key}` : key, keys);
    }
  } else if (typeof value === 'string') {
    keys.push(prefix);
  }
  return keys;
}
