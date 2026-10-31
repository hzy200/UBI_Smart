import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { AdminUser, WorkspaceAcl, WorkspaceRow } from '../types';
import { EmptyState } from '../components/EmptyState';
import { Icon } from '../components/Icons';
import type { Notify } from '../components/Toasts';
import { formatBytes, formatDate } from '../lib/format';
import { useAdminI18n } from '../i18n';

export function Workspaces({
  notify,
  readOnly = false,
}: {
  notify: Notify;
  readOnly?: boolean;
}) {
  const { t, locale } = useAdminI18n();
  const [rows, setRows] = useState<WorkspaceRow[]>([]);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [query, setQuery] = useState('');
  const [kind, setKind] = useState('');
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [selected, setSelected] = useState<WorkspaceRow | null>(null);
  const [grants, setGrants] = useState<WorkspaceAcl[]>([]);
  const [grantsLoading, setGrantsLoading] = useState(false);
  const [grantee, setGrantee] = useState('');
  const [busy, setBusy] = useState(false);

  async function load() {
    setLoading(true);
    try {
      const data = await api.adminWorkspaces();
      setRows(data.workspaces || []);
      setError('');
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
    void (async () => {
      try {
        const data = await api.listUsers();
        setUsers(data.users || []);
      } catch (err) {
        // 成员下拉失败不影响工作区列表。
      }
    })();
  }, []);

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return rows.filter((row) => {
      if (kind && String(row.kind || '') !== kind) return false;
      if (!keyword) return true;
      return (
        String(row.owner || '').toLowerCase().includes(keyword)
        || String(row.display_name || '').toLowerCase().includes(keyword)
        || String(row.root_path || '').toLowerCase().includes(keyword)
        || row.workspace_id.toLowerCase().includes(keyword)
      );
    });
  }, [rows, query, kind]);

  const candidates = useMemo(() => {
    if (!selected) return [];
    const granted = new Set(grants.map((item) => item.grantee));
    return users.filter(
      (user) =>
        user.username !== selected.owner
        && !granted.has(user.username),
    );
  }, [users, grants, selected]);

  async function openShares(row: WorkspaceRow) {
    setSelected(row);
    setGrantee('');
    setGrants([]);
    setGrantsLoading(true);
    try {
      const data = await api.workspaceAcl(row.workspace_id);
      setGrants(data.grants || []);
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setGrantsLoading(false);
    }
  }

  async function grant() {
    if (!selected || !grantee || busy) return;
    setBusy(true);
    try {
      await api.grantWorkspaceAcl(selected.workspace_id, grantee);
      notify(t('grantedToast', { user: grantee }));
      setGrantee('');
      const data = await api.workspaceAcl(selected.workspace_id);
      setGrants(data.grants || []);
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setBusy(false);
    }
  }

  async function revoke(item: WorkspaceAcl) {
    if (!selected || busy) return;
    setBusy(true);
    try {
      await api.revokeWorkspaceAcl(selected.workspace_id, item.grantee);
      notify(t('revokedToast', { user: item.grantee }));
      const data = await api.workspaceAcl(selected.workspace_id);
      setGrants(data.grants || []);
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <p className="hint">{t('workspacesHint')}</p>
        </div>
        <button className="btn ghost small" onClick={() => void load()} disabled={loading}>
          <Icon name="refresh" /> {t('refresh')}
        </button>
      </div>

      <div className="toolbar card">
        <div className="search-box">
          <Icon name="search" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('workspaceSearch')}
            aria-label={t('workspaceSearch')}
          />
        </div>
        <div className="segmented" role="group" aria-label={t('colType')}>
          {(
            [
              ['', t('kindAll')],
              ['sandbox', t('kindSandbox')],
              ['host', t('kindHost')],
            ] as Array<[string, string]>
          ).map(([value, label]) => (
            <button
              key={value || 'all'}
              className={kind === value ? 'active' : ''}
              onClick={() => setKind(value)}
              aria-pressed={kind === value}
            >
              {label}
            </button>
          ))}
        </div>
      </div>

      {error && <p className="error" role="alert">{error}</p>}

      {loading ? (
        <div className="table-skeleton" aria-label={t('loadingLabel')}>
          {[0, 1, 2, 3].map((item) => <span key={item} className="skeleton-row" />)}
        </div>
      ) : filtered.length === 0 ? (
        <EmptyState
          title={rows.length === 0 ? t('noWorkspaces') : t('noMatchWorkspaces')}
          hint={rows.length === 0 ? t('noWorkspacesHint') : t('noMatchWorkspacesHint')}
        />
      ) : (
        <div className="table-card">
          <table className="table">
            <thead>
              <tr>
                <th>{t('colWorkspace')}</th>
                <th>{t('colOwner')}</th>
                <th>{t('colType')}</th>
                <th>{t('colStatus')}</th>
                <th className="right">{t('colUsage')}</th>
                <th className="right">{t('colActions')}</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((row) => (
                <tr key={row.workspace_id}>
                  <td>
                    <strong>{row.display_name || row.workspace_id}</strong>
                    <small className="mono">
                      {row.root_path || row.workspace_id}
                    </small>
                  </td>
                  <td><span className="pill">{row.owner}</span></td>
                  <td>{row.kind === 'host' ? t('kindHost') : t('kindSandbox')}</td>
                  <td>
                    <span className={`pill ${row.status === 'ready' ? 'ok' : ''}`}>
                      {row.status}
                    </span>
                  </td>
                  <td className="right">
                    {formatBytes(row.used_bytes)}
                    {row.quota_bytes > 0
                      ? ` / ${formatBytes(row.quota_bytes)}`
                      : ''}
                    <small>{t('filesCount', { count: row.file_count })}</small>
                  </td>
                  <td>
                    <button
                      className="btn ghost small"
                      onClick={() => void openShares(row)}
                    >
                      {t('manageShares')}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {selected && (
        <div className="card">
          <div className="card-head">
            <div>
              <h3>{t('shareTitle', { name: selected.display_name || selected.workspace_id })}</h3>
              <p className="hint">
                {selected.root_path || selected.workspace_id} · {t('shareOwner', { owner: selected.owner })}
              </p>
            </div>
            <button
              className="btn ghost small"
              onClick={() => setSelected(null)}
            >
              {t('close')}
            </button>
          </div>

          {!readOnly && (
            <div className="toolbar">
              <label className="field-inline">
                {t('member')}
                <select
                  value={grantee}
                  onChange={(event) => setGrantee(event.target.value)}
                >
                  <option value="">{t('selectMember')}</option>
                  {candidates.map((user) => (
                    <option key={user.id} value={user.username}>
                      {user.username}
                    </option>
                  ))}
                </select>
              </label>
              <button
                className="btn primary small"
                disabled={!grantee || busy}
                onClick={() => void grant()}
              >
                {t('grantRead')}
              </button>
            </div>
          )}

          {grantsLoading ? (
            <div className="table-skeleton" aria-label={t('loadingLabel')}>
              {[0, 1].map((item) => <span key={item} className="skeleton-row" />)}
            </div>
          ) : grants.length === 0 ? (
            <p className="hint">{t('noSharesYet')}</p>
          ) : (
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>{t('grantee')}</th>
                    <th>{t('grantedBy')}</th>
                    <th>{t('time')}</th>
                    {!readOnly && <th className="right">{t('colActions')}</th>}
                  </tr>
                </thead>
                <tbody>
                  {grants.map((item) => (
                    <tr key={item.id || item.grantee}>
                      <td><span className="pill">{item.grantee}</span></td>
                      <td>{item.granted_by || t('none')}</td>
                      <td className="muted">{formatDate(item.created_at, locale)}</td>
                      {!readOnly && (
                        <td className="right">
                          <button
                            className="btn small danger"
                            disabled={busy}
                            onClick={() => void revoke(item)}
                          >
                            {t('revoke')}
                          </button>
                        </td>
                      )}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
