/* Alpine mixin: 国际化。
 *
 * CSP 构建不能解析 `t('key')` 这类方法调用，所以模板统一绑定
 * `i18n.<section>.<key>` 的属性路径；JS 内部用 `this.t(key, params)`。
 */

import {
  DEFAULT_LOCALE,
  MESSAGES,
  normalizeLocale,
  translate,
} from '../i18n';

export const i18nMixin = {
  locale: DEFAULT_LOCALE,
  i18n: MESSAGES[DEFAULT_LOCALE],

  t(key, params = {}) {
    return translate(this.locale, key, params);
  },

  setLocale(value) {
    const locale = normalizeLocale(value);
    this.locale = locale;
    this.i18n = MESSAGES[locale];
    if (typeof document !== 'undefined' && document.documentElement) {
      document.documentElement.lang = locale;
    }
  },
};
