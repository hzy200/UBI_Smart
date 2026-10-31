import { useEffect, useState } from 'react';
import { api } from '../api';
import type { AdminUser, AuditRecord } from '../types';
import { EmptyState } from '../components/EmptyState';
import { Icon } from '../components/Icons';
import type { Notify } from '../components/Toasts';
import { buildAuditCsv } from '../lib/audit';
import { downloadCsv } from '../lib/csv';
import { useAdminI18n } from '../i18n';

const PAGE_SIZE = 100;
const MAX_EXPORT = 5000;

function sinceDays(days: number): string {
  const date = new Date();
  date.setDate(date.getDate() - days);
  date.setHours(0, 0, 0, 0);
  return date.toISOString().slice(0, 19);
}

export function Audit({ notify }: { notify: Notify }) {
  const { t, locale } = useAdminI18n();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [rows, setRows] = useState<AuditRecord[]>([]);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [exporting, setExporting] = useState(false);
  const [filters, setFilters] = useState({
    user: '',
    thread_id: '',
    kind: '',
    tool: '',
    ok: '',
    since: sinceDays(6),
    until: '',
  });

  function queryParams(
    nextFilters: typeof filters,
    limit: number,
    nextOffset: number,
  ) {
    return {
      user: nextFilters.user,
      thread_id: nextFilters.thread_id.trim(),
      kind: nextFilters.kind.trim(),
      tool: nextFilters.tool.trim(),
      ok: nextFilters.ok === ''
        ? undefined
        : nextFilters.ok === 'true',
      since: nextFilters.since,
      until: nextFilters.until,
      limit,
      offset: nextOffset,
    };
  }

  async function load(nextOffset = offset, nextFilters = filters) {
    setLoading(true);
    try {
      const result = await api.audit(
        queryParams(nextFilters, PAGE_SIZE, nextOffset),
      );
      setRows(result.records || []);
      setTotal(Number(result.total || 0));
      setHasMore(Boolean(result.has_more));
      setOffset(nextOffset);
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setLoading(false);
    }
  }

  async function exportCsv() {
    if (exporting) return;
    setExporting(true);
    try {
      const collected: AuditRecord[] = [];
      let nextOffset = 0;
      while (collected.length < MAX_EXPORT) {
        const page = await api.audit(
          queryParams(filters, 500, nextOffset),
        );
        const items = page.records || [];
        collected.push(...items);
        if (!page.has_more || items.length === 0) break;
        nextOffset += items.length;
      }
      if (collected.length === 0) {
        notify(t('auditNoExport'));
        return;
      }
      const since = filters.since ? filters.since.slice(0, 10) : 'all';
      const until = filters.until ? filters.until.slice(0, 10) : 'now';
      downloadCsv(
        `audit-${since}-${until}.csv`,
        buildAuditCsv(collected, locale),
      );
      notify(
        collected.length >= MAX_EXPORT
          ? t('auditExportedTruncated', { count: MAX_EXPORT })
          : t('auditExported', { count: collected.length }),
      );
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setExporting(false);
    }
  }

  useEffect(() => {
    void (async () => {
      try {
        const data = await api.listUsers();
        setUsers(data.users || []);
      } catch (err) {
        // 用户下拉失败不影响审计查询本身。
      }
      await load(0, filters);
    })();
  }, []);

  function submit(event: React.FormEvent) {
    event.preventDefault();
    void load(0, filters);
  }

  function reset() {
    const next = {
      user: '',
      thread_id: '',
      kind: '',
      tool: '',
      ok: '',
      since: sinceDays(6),
      until: '',
    };
    setFilters(next);
    void load(0, next);
  }

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <p className="hint">{t('auditHint')}</p>
        </div>
        <div className="row-actions">
          <span className="pill">{t('auditTotal', { count: total })}</span>
          <button
            className="btn ghost small"
            onClick={() => void exportCsv()}
            disabled={exporting || loading}
          >
            <Icon name="download" />
            {exporting ? t('auditExporting') : t('auditExport')}
          </button>
        </div>
      </div>

      <form className="toolbar card" onSubmit={submit}>
        <label className="field-inline">
          {t('user')}
          <select
            value={filters.user}
            onChange={(event) => setFilters({ ...filters, user: event.target.value })}
          >
            <option value="">{t('all')}</option>
            {users.map((user) => (
              <option key={user.id} value={user.username}>{user.username}</option>
            ))}
          </select>
        </label>
        <label className="field-inline">
          {t('auditThread')}
          <input
            value={filters.thread_id}
            onChange={(event) => setFilters({ ...filters, thread_id: event.target.value })}
            placeholder="thread_id"
          />
        </label>
        <label className="field-inline">
          {t('auditKind')}
          <input
            value={filters.kind}
            onChange={(event) => setFilters({ ...filters, kind: event.target.value })}
            placeholder="tool_call"
          />
        </label>
        <label className="field-inline">
          {t('auditTool')}
          <input
            value={filters.tool}
            onChange={(event) => setFilters({ ...filters, tool: event.target.value })}
            placeholder="shell_exec"
          />
        </label>
        <label className="field-inline">
          {t('auditResult')}
          <select
            value={filters.ok}
            onChange={(event) => setFilters({ ...filters, ok: event.target.value })}
          >
            <option value="">{t('all')}</option>
            <option value="true">{t('resultSuccess')}</option>
            <option value="false">{t('resultFailure')}</option>
          </select>
        </label>
        <label className="field-inline">
          {t('from')}
          <input
            type="datetime-local"
            value={filters.since.slice(0, 16)}
            onChange={(event) => setFilters({ ...filters, since: event.target.value })}
          />
        </label>
        <label className="field-inline">
          {t('to')}
          <input
            type="datetime-local"
            value={filters.until.slice(0, 16)}
            onChange={(event) => setFilters({ ...filters, until: event.target.value })}
          />
        </label>
        <span className="spacer" />
        <button className="btn ghost small" type="button" onClick={reset}>
          {t('auditReset')}
        </button>
        <button className="btn primary" type="submit" disabled={loading}>
          <Icon name="search" /> {loading ? t('querying') : t('query')}
        </button>
      </form>

      {loading ? (
        <div className="table-skeleton" aria-label={t('loadingLabel')}>
          {[0, 1, 2, 3, 4].map((item) => <span key={item} className="skeleton-row" />)}
        </div>
      ) : rows.length === 0 ? (
        <EmptyState title={t('auditEmpty')} hint={t('auditEmptyHint')} />
      ) : (
        <>
          <div className="table-card">
            <table className="table">
              <thead>
                <tr>
                  <th>{t('colTime')}</th>
                  <th>{t('user')}</th>
                  <th>{t('colKind')}</th>
                  <th>{t('colTool')}</th>
                  <th>{t('colLevel')}</th>
                  <th>{t('colDecision')}</th>
                  <th>{t('colResult')}</th>
                  <th>{t('colThread')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => (
                  <tr key={`${row.record_hash || row.ts}:${index}`}>
                    <td className="mono">{row.ts}</td>
                    <td><span className="pill">{row.tenant}</span></td>
                    <td>{row.kind}</td>
                    <td className="mono">{row.tool || '—'}</td>
                    <td>{row.level || '—'}</td>
                    <td>{row.decision || '—'}</td>
                    <td>
                      {row.ok === null || row.ok === undefined ? (
                        <span className="muted">—</span>
                      ) : (
                        <span className={`pill ${row.ok ? 'ok' : 'bad'}`}>
                          {row.ok ? t('resultSuccess') : t('resultFailure')}
                        </span>
                      )}
                    </td>
                    <td className="mono" title={row.thread_id}>
                      {row.thread_id || '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="toolbar card">
            <span className="muted">
              {offset + 1}–{offset + rows.length} / {total}
            </span>
            <span className="spacer" />
            <button
              className="btn ghost small"
              disabled={loading || offset === 0}
              onClick={() => void load(Math.max(0, offset - PAGE_SIZE), filters)}
            >
              ‹
            </button>
            <button
              className="btn ghost small"
              disabled={loading || !hasMore}
              onClick={() => void load(offset + PAGE_SIZE, filters)}
            >
              ›
            </button>
          </div>
        </>
      )}
    </section>
  );
}
