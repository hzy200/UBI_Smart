import { useEffect, useState } from 'react';
import { api } from '../api';
import type { ProviderKeyRow } from '../types';
import { Icon } from '../components/Icons';
import { EmptyState } from '../components/EmptyState';
import type { Notify } from '../components/Toasts';
import { useAdminI18n } from '../i18n';

export function Providers({ notify }: { notify: Notify }) {
  const { t } = useAdminI18n();
  const [rows, setRows] = useState<ProviderKeyRow[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    void (async () => {
      try {
        const data = await api.providers();
        setRows(data.providers);
      } catch (err) {
        notify(String(err), 'error');
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <p className="hint">{t('providersHint')}</p>
        </div>
      </div>

      {loading ? (
        <div className="card-grid">
          {[0, 1, 2].map((item) => <span key={item} className="skeleton-card" />)}
        </div>
      ) : rows.length === 0 ? (
        <EmptyState title={t('noProvidersTitle')} hint={t('noProvidersHint')} />
      ) : (
        <div className="card-grid">
          {rows.map((row) => (
            <div className="provider-card" key={row.provider}>
              <div className="provider-head">
                <span className="provider-icon"><Icon name="provider" /></span>
                <div>
                  <strong>{row.provider}</strong>
                  <small>{row.model || t('modelUnset')}</small>
                </div>
              </div>
              <dl>
                <div>
                  <dt>{t('baseUrl')}</dt>
                  <dd className="mono">{row.base_url || t('envDefault')}</dd>
                </div>
                <div>
                  <dt>{t('envKey')}</dt>
                  <dd>{row.has_key ? t('configured') : t('unconfigured')}</dd>
                </div>
              </dl>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
