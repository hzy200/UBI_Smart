import { useEffect, useState } from 'react';
import { api } from '../api';
import type { HealthReport } from '../types';
import { EmptyState } from '../components/EmptyState';
import { Icon } from '../components/Icons';
import type { Notify } from '../components/Toasts';
import { formatNumber } from '../lib/format';
import { useAdminI18n } from '../i18n';

const PRIMARY: Array<[string, string]> = [
  ['agent_active_runs', 'activeRuns'],
  ['agent_sse_connections', 'sseConnections'],
  ['agent_approvals_pending', 'pendingApprovals'],
  ['agent_auth_events_total', 'authEvents'],
  ['agent_audit_write_failures_total', 'auditFailures'],
  ['agent_llm_errors_total', 'llmErrors'],
  ['agent_http_requests_total', 'httpRequests'],
  ['agent_runs_total', 'completedRuns'],
];

export function Health({
  notify,
  readOnly = false,
}: {
  notify: Notify;
  readOnly?: boolean;
}) {
  const { t, locale } = useAdminI18n();
  const [report, setReport] = useState<HealthReport | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [reindexing, setReindexing] = useState(false);

  async function load(silent = false) {
    if (!silent) setLoading(true);
    try {
      setReport(await api.health());
      setError('');
    } catch (err) {
      setError(String(err));
      if (!silent) notify(String(err), 'error');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(true), 10000);
    return () => window.clearInterval(timer);
  }, []);

  async function reindex() {
    if (!window.confirm(t('reindexConfirm'))) {
      return;
    }
    setReindexing(true);
    try {
      const data = await api.reindexSearch();
      notify(t('reindexed', { count: data.reindexed }));
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setReindexing(false);
    }
  }

  if (loading && !report) {
    return (
      <section className="page">
        <div className="table-skeleton" aria-label={t('loadingLabel')}>
          {[0, 1, 2, 3].map((item) => <span key={item} className="skeleton-row" />)}
        </div>
      </section>
    );
  }

  if (!report) {
    return (
      <section className="page">
        <EmptyState title={t('unavailable')} hint={error || t('healthHint')} />
      </section>
    );
  }

  const metrics = report.metrics || {};
  const auditFailures = Number(metrics.agent_audit_write_failures_total || 0);
  const extras = Object.entries(metrics)
    .filter(([name]) => !PRIMARY.some(([key]) => key === name))
    .sort(([a], [b]) => a.localeCompare(b));

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <p className="hint">{t('healthHint')}</p>
        </div>
        <div className="row-actions">
          {!readOnly && (
            <button
              className="btn ghost small"
              onClick={() => void reindex()}
              disabled={reindexing}
            >
              <Icon name="database" /> {reindexing ? t('rebuilding') : t('rebuildSearch')}
            </button>
          )}
          <button className="btn ghost small" onClick={() => void load()}>
            <Icon name="refresh" /> {t('refresh')}
          </button>
        </div>
      </div>

      {error && <p className="error" role="alert">{error}</p>}

      <div className="stat-grid">
        <Stat
          label={t('database')}
          value={report.database.ok ? t('normal') : t('unavailable')}
          tone={report.database.ok ? 'ok' : 'bad'}
          hint={report.database.error || `API ${report.api_version}`}
        />
        {PRIMARY.map(([name, labelKey]) => (
          <Stat
            key={name}
            label={t(labelKey)}
            value={formatNumber(metrics[name] || 0, locale)}
            tone={name === 'agent_audit_write_failures_total' && auditFailures > 0 ? 'bad' : ''}
          />
        ))}
      </div>

      <p className="hint">{t('snapshotAt', { time: report.time })}</p>

      {extras.length > 0 && (
        <div className="table-card">
          <table className="table">
            <thead>
              <tr><th>{t('metricName')}</th><th className="right">{t('metricValue')}</th></tr>
            </thead>
            <tbody>
              {extras.map(([name, value]) => (
                <tr key={name}>
                  <td className="mono">{name}</td>
                  <td className="right">{formatNumber(value, locale)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Stat({
  label,
  value,
  hint,
  tone = '',
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: '' | 'ok' | 'bad';
}) {
  return (
    <div className={`stat ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
      {hint && <small>{hint}</small>}
    </div>
  );
}
