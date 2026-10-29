import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { UsageReport } from '../types';
import { Icon } from '../components/Icons';
import { EmptyState } from '../components/EmptyState';
import { UsageChart } from '../components/UsageChart';
import type { Notify } from '../components/Toasts';
import { dayString, daysAgo, formatCost, formatNumber } from '../lib/format';
import { downloadCsv } from '../lib/csv';
import { aggregateUsageByDay, buildUsageCsv } from '../lib/usage';
import { useAdminI18n } from '../i18n';

export function Usage({ notify }: { notify: Notify }) {
  const { t, locale } = useAdminI18n();
  const [from, setFrom] = useState(dayString(daysAgo(6)));
  const [to, setTo] = useState(dayString(new Date()));
  const [user, setUser] = useState('');
  const [report, setReport] = useState<UsageReport | null>(null);
  const [loading, setLoading] = useState(true);

  async function load(nextFrom = from, nextTo = to, nextUser = user) {
    setLoading(true);
    try {
      setReport(await api.usage(nextFrom, nextTo, nextUser.trim()));
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  function preset(days: number) {
    const nextFrom = dayString(daysAgo(days - 1));
    const nextTo = dayString(new Date());
    setFrom(nextFrom);
    setTo(nextTo);
    void load(nextFrom, nextTo);
  }

  const daily = useMemo(
    () => aggregateUsageByDay(report?.rows || []),
    [report],
  );

  function exportCsv() {
    if (!report || report.rows.length === 0) return;
    downloadCsv(
      `usage-${from}-${to}.csv`,
      buildUsageCsv(report.rows, locale),
    );
  }

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <p className="hint">{t('usageHint')}</p>
        </div>
        <button
          className="btn ghost small"
          onClick={exportCsv}
          disabled={!report || report.rows.length === 0}
        >
          <Icon name="download" /> {t('exportCsv')}
        </button>
      </div>

      <form
        className="toolbar card"
        onSubmit={(event) => {
          event.preventDefault();
          void load();
        }}
      >
        <div className="date-presets" role="group" aria-label={t('colDate')}>
          <button type="button" className="btn small ghost" onClick={() => preset(7)}>{t('preset7')}</button>
          <button type="button" className="btn small ghost" onClick={() => preset(30)}>{t('preset30')}</button>
          <button type="button" className="btn small ghost" onClick={() => preset(90)}>{t('preset90')}</button>
        </div>
        <label className="field-inline">
          {t('from')}
          <input type="date" value={from} onChange={(event) => setFrom(event.target.value)} />
        </label>
        <label className="field-inline">
          {t('to')}
          <input type="date" value={to} onChange={(event) => setTo(event.target.value)} />
        </label>
        <label className="field-inline">
          {t('user')}
          <input
            value={user}
            onChange={(event) => setUser(event.target.value)}
            placeholder={t('usageUserPlaceholder')}
          />
        </label>
        <span className="spacer" />
        <button className="btn primary" type="submit" disabled={loading}>
          <Icon name="search" /> {loading ? t('querying') : t('query')}
        </button>
      </form>

      {report && (
        <div className="stat-grid">
          <Stat label={t('totalTokens')} value={formatNumber(report.totals.tokens, locale)} tone="accent" />
          <Stat label={t('cost')} value={formatCost(report.totals.cost_cny)} />
          <Stat label={t('runs')} value={formatNumber(report.totals.runs, locale)} />
          <Stat label={t('reservedTokens')} value={formatNumber(report.totals.reserved_tokens, locale)} />
        </div>
      )}

      {report && report.rows.length > 0 && (
        <div className="chart-grid">
          <UsageChart points={daily} metric="cost" title={t('chartCost')} locale={locale} />
          <UsageChart points={daily} metric="tokens" title={t('chartTokens')} locale={locale} />
        </div>
      )}

      {loading ? (
        <div className="table-skeleton" aria-label={t('loadingLabel')}>
          {[0, 1, 2, 3, 4].map((item) => <span key={item} className="skeleton-row" />)}
        </div>
      ) : !report || report.rows.length === 0 ? (
        <EmptyState
          title={t('noUsage')}
          hint={t('noUsageHint')}
        />
      ) : (
        <div className="table-card">
          <table className="table">
            <thead>
              <tr>
                <th>{t('colDate')}</th>
                <th>{t('user')}</th>
                <th className="right">{t('colInput')}</th>
                <th className="right">{t('colOutput')}</th>
                <th className="right">{t('colWorker')}</th>
                <th className="right">{t('colCost')}</th>
                <th className="right">{t('colRuns')}</th>
              </tr>
            </thead>
            <tbody>
              {report.rows.map((row) => (
                <tr key={`${row.day}:${row.user}`}>
                  <td className="mono">{row.day}</td>
                  <td><span className="pill">{row.user}</span></td>
                  <td className="right">{formatNumber(row.input_tokens, locale)}</td>
                  <td className="right">{formatNumber(row.output_tokens, locale)}</td>
                  <td className="right">{formatNumber(row.worker_input_tokens + row.worker_output_tokens, locale)}</td>
                  <td className="right">{formatCost(row.cost_cny)}</td>
                  <td className="right">{formatNumber(row.runs, locale)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Stat({ label, value, tone = '' }: { label: string; value: string; tone?: string }) {
  return (
    <div className={`stat ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
