import { useEffect, useState } from 'react';
import { api } from '../api';
import { EmptyState } from '../components/EmptyState';
import { Icon } from '../components/Icons';
import type { Notify } from '../components/Toasts';
import type { ApprovalRow } from '../types';
import { buildApprovalCsv } from '../lib/approvals';
import { downloadCsv } from '../lib/csv';
import { formatDate } from '../lib/format';
import { useAdminI18n } from '../i18n';

const PAGE_SIZE = 100;
const MAX_EXPORT = 5000;

export function Approvals({ notify }: { notify: Notify }) {
  const { t, locale } = useAdminI18n();
  const [tab, setTab] = useState<'pending' | 'history'>('pending');
  const [rows, setRows] = useState<ApprovalRow[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');
  const [selectedIds, setSelectedIds] = useState<string[]>([]);
  const [streamState, setStreamState] = useState<
    'connecting' | 'live' | 'polling'
  >('connecting');
  const [history, setHistory] = useState<ApprovalRow[]>([]);
  const [historyTotal, setHistoryTotal] = useState(0);
  const [historyOffset, setHistoryOffset] = useState(0);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [historyLoaded, setHistoryLoaded] = useState(false);
  const [historyFilters, setHistoryFilters] = useState({
    status: '',
    user: '',
    from: '',
    to: '',
  });

  async function load() {
    try {
      const data = await api.pendingApprovals();
      setRows(data.approvals || []);
      setEnabled(data.enabled !== false);
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    const controller = new AbortController();
    let pollTimer = 0;
    let stopped = false;

    async function connect() {
      try {
        const response = await fetch(api.approvalsStreamUrl(), {
          credentials: 'include',
          cache: 'no-store',
          headers: { Accept: 'text/event-stream' },
          signal: controller.signal,
        });
        if (!response.ok || !response.body) {
          throw new Error(`HTTP ${response.status}`);
        }
        setStreamState('live');
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (!stopped) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let boundary = buffer.indexOf('\n\n');
          while (boundary >= 0) {
            const frame = buffer.slice(0, boundary);
            buffer = buffer.slice(boundary + 2);
            boundary = buffer.indexOf('\n\n');
            for (const line of frame.split('\n')) {
              if (!line.startsWith('data:')) continue;
              const raw = line.slice(5).trim();
              if (!raw) continue;
              try {
                const payload = JSON.parse(raw);
                if (Array.isArray(payload.approvals)) {
                  setRows(payload.approvals);
                  setEnabled(payload.enabled !== false);
                  setLoading(false);
                }
              } catch (err) {
                // 单帧损坏不终止流；下一次变化会重新推送完整快照。
              }
            }
          }
        }
        throw new Error('stream ended');
      } catch (err) {
        if (stopped || controller.signal.aborted) return;
        setStreamState('polling');
        pollTimer = window.setInterval(() => void load(), 5000);
      }
    }

    void connect();
    return () => {
      stopped = true;
      controller.abort();
      window.clearInterval(pollTimer);
    };
  }, []);

  useEffect(() => {
    setSelectedIds((current) =>
      current.filter((id) => rows.some((row) => row.id === id)),
    );
  }, [rows]);

  async function historyParams(limit: number, offset: number) {
    return api.approvalHistory({
      status: historyFilters.status,
      user: historyFilters.user.trim(),
      from: historyFilters.from,
      to: historyFilters.to,
      limit,
      offset,
    });
  }

  async function loadHistory(offset = 0) {
    setHistoryLoading(true);
    try {
      const result = await historyParams(PAGE_SIZE, offset);
      setHistory(result.records || []);
      setHistoryTotal(Number(result.total || 0));
      setHistoryOffset(offset);
      setEnabled(result.enabled !== false);
      setHistoryLoaded(true);
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setHistoryLoading(false);
    }
  }

  function switchTab(next: 'pending' | 'history') {
    setTab(next);
    if (next === 'history' && !historyLoaded) void loadHistory(0);
  }

  function toggleSelect(id: string) {
    setSelectedIds((current) =>
      current.includes(id)
        ? current.filter((item) => item !== id)
        : [...current, id],
    );
  }

  function toggleAll() {
    setSelectedIds(
      selectedIds.length === rows.length
        ? []
        : rows.map((row) => row.id),
    );
  }

  async function decide(id: string, approved: boolean) {
    setBusy(id);
    try {
      await api.decideApproval(id, approved);
      notify(approved ? t('statusApproved') : t('statusDenied'));
      await load();
      if (historyLoaded) void loadHistory(historyOffset);
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setBusy('');
    }
  }

  async function decideSelected(approved: boolean) {
    if (!selectedIds.length || busy) return;
    setBusy('batch');
    try {
      const result = await api.batchApprovals(
        selectedIds,
        approved,
      );
      const failed = (result.results || []).filter(
        (item) => !item.ok,
      ).length;
      notify(
        failed
          ? t('batchResultFailed', { updated: result.updated, failed })
          : t('batchResult', { updated: result.updated }),
      );
      setSelectedIds([]);
      await load();
      if (historyLoaded) await loadHistory(historyOffset);
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setBusy('');
    }
  }

  async function exportHistory() {
    if (busy === 'export') return;
    setBusy('export');
    try {
      const collected: ApprovalRow[] = [];
      let offset = 0;
      while (collected.length < MAX_EXPORT) {
        const page = await historyParams(500, offset);
        const items = page.records || [];
        collected.push(...items);
        if (!page.has_more || items.length === 0) break;
        offset += items.length;
      }
      if (collected.length === 0) {
        notify(t('noApprovalExport'));
        return;
      }
      const suffix = historyFilters.from || 'all';
      const until = historyFilters.to || 'now';
      downloadCsv(
        `approvals-${suffix}-${until}.csv`,
        buildApprovalCsv(collected, locale),
      );
      notify(
        collected.length >= MAX_EXPORT
          ? t('exportedApprovalsTruncated', { count: MAX_EXPORT })
          : t('exportedApprovals', { count: collected.length }),
      );
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setBusy('');
    }
  }

  if (!enabled) {
    return (
      <section className="page">
        <EmptyState
          title={t('approvalDisabled')}
          hint={t('approvalDisabledHint')}
        />
      </section>
    );
  }

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <p className="hint">{t('approvalsHint')}</p>
        </div>
        <div className="row-actions">
          <span className={`pill ${streamState === 'live' ? 'ok' : 'muted'}`}>
            {streamState === 'live'
              ? t('live')
              : streamState === 'polling'
                ? t('polling')
                : t('connecting')}
          </span>
          <button
            className="btn ghost"
            onClick={() => {
              void load();
              if (historyLoaded) void loadHistory(historyOffset);
            }}
            disabled={loading}
          >
            <Icon name="refresh" /> {t('refresh')}
          </button>
        </div>
      </div>

      <div className="segmented" role="group" aria-label={t('navApprovals')}>
        <button
          className={tab === 'pending' ? 'active' : ''}
          onClick={() => switchTab('pending')}
          aria-pressed={tab === 'pending'}
        >
          {t('pending')}
        </button>
        <button
          className={tab === 'history' ? 'active' : ''}
          onClick={() => switchTab('history')}
          aria-pressed={tab === 'history'}
        >
          {t('history')}
        </button>
      </div>

      {tab === 'pending' ? (
        <>
          {selectedIds.length > 0 && (
            <div className="toolbar card">
              <span>{t('selected', { count: selectedIds.length })}</span>
              <span className="spacer" />
              <button
                className="btn small primary"
                disabled={busy === 'batch'}
                onClick={() => void decideSelected(true)}
              >
                {t('approveSelected')}
              </button>
              <button
                className="btn small danger"
                disabled={busy === 'batch'}
                onClick={() => void decideSelected(false)}
              >
                {t('rejectSelected')}
              </button>
              <button
                className="btn small ghost"
                onClick={() => setSelectedIds([])}
              >
                {t('clearSelection')}
              </button>
            </div>
          )}
          {rows.length === 0 ? (
            <EmptyState
              title={t('noPending')}
              hint={t('noPendingHint')}
            />
          ) : (
            <div className="card table-wrap">
              <table>
                <thead>
                  <tr>
                    <th aria-label={t('selectAll')}>
                      <input
                        type="checkbox"
                        aria-label={t('selectAll')}
                        checked={
                          rows.length > 0
                          && selectedIds.length === rows.length
                        }
                        onChange={toggleAll}
                      />
                    </th>
                    <th>{t('historyRequester')}</th>
                    <th>{t('colLevel')}</th>
                    <th>{t('colTool')}</th>
                    <th>{t('colDecision')}</th>
                    <th>{t('colTime')}</th>
                    <th aria-label={t('colActions')} />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <tr key={row.id}>
                      <td>
                        <input
                          type="checkbox"
                          aria-label={t('selectOne', { user: row.requested_by })}
                          checked={selectedIds.includes(row.id)}
                          onChange={() => toggleSelect(row.id)}
                        />
                      </td>
                      <td>{row.requested_by}</td>
                      <td><span className="pill">{row.level || `L${row.level_int}`}</span></td>
                      <td>{row.tool}</td>
                      <td><code className="mono">{row.command}</code></td>
                      <td className="muted">
                        {formatDate(row.created_at_iso || row.created_at, locale)}
                      </td>
                      <td className="row-actions">
                        <button
                          className="btn small primary"
                          disabled={busy === row.id}
                          onClick={() => void decide(row.id, true)}
                        >
                          {t('approve')}
                        </button>
                        <button
                          className="btn small danger"
                          disabled={busy === row.id}
                          onClick={() => void decide(row.id, false)}
                        >
                          {t('reject')}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      ) : (
        <>
          <div className="toolbar card">
            <label className="field-inline">
              {t('historyStatus')}
              <select
                value={historyFilters.status}
                onChange={(event) =>
                  setHistoryFilters({
                    ...historyFilters,
                    status: event.target.value,
                  })}
              >
                <option value="">{t('all')}</option>
                <option value="pending">{t('statusPending')}</option>
                <option value="approved">{t('statusApproved')}</option>
                <option value="denied">{t('statusDenied')}</option>
                <option value="expired">{t('statusExpired')}</option>
              </select>
            </label>
            <label className="field-inline">
              {t('historyRequester')}
              <input
                value={historyFilters.user}
                onChange={(event) =>
                  setHistoryFilters({
                    ...historyFilters,
                    user: event.target.value,
                  })}
                placeholder={t('usageUserPlaceholder')}
              />
            </label>
            <label className="field-inline">
              {t('from')}
              <input
                type="date"
                value={historyFilters.from}
                onChange={(event) =>
                  setHistoryFilters({
                    ...historyFilters,
                    from: event.target.value,
                  })}
              />
            </label>
            <label className="field-inline">
              {t('to')}
              <input
                type="date"
                value={historyFilters.to}
                onChange={(event) =>
                  setHistoryFilters({
                    ...historyFilters,
                    to: event.target.value,
                  })}
              />
            </label>
            <span className="spacer" />
            <button
              className="btn ghost small"
              onClick={() => void exportHistory()}
              disabled={busy === 'export'}
            >
              <Icon name="download" />
              {busy === 'export' ? t('auditExporting') : t('exportCsv')}
            </button>
            <button
              className="btn primary"
              onClick={() => void loadHistory(0)}
              disabled={historyLoading}
            >
              <Icon name="search" /> {historyLoading ? t('querying') : t('query')}
            </button>
          </div>

          {historyLoading && history.length === 0 ? (
            <div className="table-skeleton" aria-label={t('loadingLabel')}>
              {[0, 1, 2, 3].map((item) => (
                <span key={item} className="skeleton-row" />
              ))}
            </div>
          ) : history.length === 0 ? (
            <EmptyState
              title={t('historyEmpty')}
              hint={t('historyEmptyHint')}
            />
          ) : (
            <>
              <div className="card table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>{t('colCreated')}</th>
                      <th>{t('historyRequester')}</th>
                      <th>{t('colLevel')}</th>
                      <th>{t('colTool')}</th>
                      <th>{t('colDecision')}</th>
                      <th>{t('historyStatus')}</th>
                      <th>{t('colApprover')}</th>
                      <th>{t('colDecidedAt')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {history.map((row) => (
                      <tr key={row.id}>
                        <td className="muted">
                          {formatDate(row.created_at_iso || row.created_at, locale)}
                        </td>
                        <td>{row.requested_by}</td>
                        <td><span className="pill">{row.level || `L${row.level_int}`}</span></td>
                        <td>{row.tool}</td>
                        <td><code className="mono">{row.command}</code></td>
                        <td>
                          <span className={`pill ${row.status === 'approved' ? 'ok' : row.status === 'denied' ? 'bad' : ''}`}>
                            {t(
                              row.status === 'approved'
                                ? 'statusApproved'
                                : row.status === 'denied'
                                  ? 'statusDenied'
                                  : row.status === 'expired'
                                    ? 'statusExpired'
                                    : 'statusPending',
                            )}
                          </span>
                        </td>
                        <td>{row.decided_by || t('none')}</td>
                        <td className="muted">
                          {row.decided_at
                            ? formatDate(row.decided_at_iso || row.decided_at, locale)
                            : t('none')}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="toolbar card">
                <span className="muted">
                  {historyOffset + 1}–{historyOffset + history.length} / {historyTotal}
                </span>
                <span className="spacer" />
                <button
                  className="btn ghost small"
                  disabled={historyLoading || historyOffset === 0}
                  onClick={() =>
                    void loadHistory(Math.max(0, historyOffset - PAGE_SIZE))}
                >
                  ‹
                </button>
                <button
                  className="btn ghost small"
                  disabled={
                    historyLoading
                    || historyOffset + history.length >= historyTotal
                  }
                  onClick={() =>
                    void loadHistory(historyOffset + PAGE_SIZE)}
                >
                  ›
                </button>
              </div>
            </>
          )}
        </>
      )}
    </section>
  );
}
