/* 管理端 i18n：复用用户端的 zh-CN/en-US 词表与 locale 检测。 */

import {
  createContext,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from 'react';

import {
  detectLocale,
  normalizeLocale,
  translate,
  type Locale,
} from '../i18n';

interface AdminI18n {
  locale: Locale;
  t: (key: string, params?: Record<string, unknown>) => string;
  setLocale: (locale: Locale) => void;
}

const AdminI18nContext = createContext<AdminI18n | null>(null);

const SETTINGS_KEY = 'codingagent.settings';

export function AdminI18nProvider({ children }: { children: ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>(() => detectLocale());
  const value = useMemo<AdminI18n>(() => {
    const t = (
      key: string,
      params: Record<string, unknown> = {},
    ) => translate(locale, `admin.${key}`, params);
    const setLocale = (next: Locale) => {
      const normalized = normalizeLocale(next);
      setLocaleState(normalized);
      try {
        const saved = JSON.parse(
          localStorage.getItem(SETTINGS_KEY) || '{}',
        );
        localStorage.setItem(
          SETTINGS_KEY,
          JSON.stringify({ ...saved, locale: normalized }),
        );
      } catch (err) {
        // 偏好持久化失败不阻断切换。
      }
    };
    return { locale, t, setLocale };
  }, [locale]);
  return (
    <AdminI18nContext.Provider value={value}>
      {children}
    </AdminI18nContext.Provider>
  );
}

export function useAdminI18n(): AdminI18n {
  const value = useContext(AdminI18nContext);
  if (!value) {
    throw new Error('AdminI18nProvider is missing');
  }
  return value;
}
