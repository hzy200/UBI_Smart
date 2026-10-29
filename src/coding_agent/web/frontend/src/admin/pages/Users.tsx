import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { AdminUser } from '../types';
import { Icon } from '../components/Icons';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { EmptyState } from '../components/EmptyState';
import type { Notify } from '../components/Toasts';
import { formatDate, initials } from '../lib/format';
import { useAdminI18n } from '../i18n';
import { UserDetail } from './UserDetail';

type StatusFilter = 'all' | 'active' | 'disabled';
type Role = 'admin' | 'approver' | 'auditor' | 'member';

const ROLE_KEYS: Record<string, string> = {
  owner: 'roleOwner',
  admin: 'roleAdmin',
  approver: 'roleApprover',
  auditor: 'roleAuditor',
  member: 'roleMember',
};

export function Users({
  notify,
  readOnly = false,
}: {
  notify: Notify;
  readOnly?: boolean;
}) {
  const { t, locale } = useAdminI18n();
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [selected, setSelected] = useState<AdminUser | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<StatusFilter>('all');
  const [createOpen, setCreateOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [pendingStatus, setPendingStatus] = useState<AdminUser | null>(null);
  const [form, setForm] = useState({
    username: '',
    email: '',
    role: 'member' as Role,
    password: '',
  });

  async function load() {
    setLoading(true);
    try {
      const data = await api.listUsers();
      setUsers(data.users);
      setError('');
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  const filtered = useMemo(() => {
    const keyword = query.trim().toLowerCase();
    return users.filter((user) => {
      if (filter !== 'all' && user.status !== filter) return false;
      if (!keyword) return true;
      return (
        user.username.toLowerCase().includes(keyword) ||
        String(user.email || '').toLowerCase().includes(keyword)
      );
    });
  }, [users, query, filter]);

  async function createUser(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await api.createUser(form);
      setForm({ username: '', email: '', role: 'member', password: '' });
      setCreateOpen(false);
      notify(t('userCreated'));
      await load();
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setBusy(false);
    }
  }

  async function toggleStatus() {
    if (!pendingStatus) return;
    setBusy(true);
    try {
      await api.updateUser(pendingStatus.id, {
        status: pendingStatus.status === 'active' ? 'disabled' : 'active',
      });
      notify(
        pendingStatus.status === 'active'
          ? t('userDisabled')
          : t('userEnabled'),
      );
      setPendingStatus(null);
      await load();
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setBusy(false);
    }
  }

  if (selected) {
    return (
      <UserDetail
        user={selected}
        notify={notify}
        onBack={() => {
          setSelected(null);
          void load();
        }}
      />
    );
  }

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <p className="hint">{t('usersHint')}</p>
        </div>
        {!readOnly && (
          <button className="btn primary" onClick={() => setCreateOpen(true)}>
            <Icon name="plus" /> {t('newUser')}
          </button>
        )}
      </div>

      <div className="toolbar card">
        <div className="search-box">
          <Icon name="search" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder={t('searchUsers')}
            aria-label={t('searchUsers')}
          />
        </div>
        <div className="segmented" role="group" aria-label={t('colStatus')}>
          {(
            [
              ['all', t('all')],
              ['active', t('active')],
              ['disabled', t('disabled')],
            ] as Array<[StatusFilter, string]>
          ).map(([key, label]) => (
            <button
              key={key}
              className={filter === key ? 'active' : ''}
              onClick={() => setFilter(key)}
              aria-pressed={filter === key}
            >
              {label}
            </button>
          ))}
        </div>
        <span className="spacer" />
        <button className="btn ghost small" onClick={() => void load()} disabled={loading}>
          <Icon name="refresh" /> {t('refresh')}
        </button>
      </div>

      {error && <p className="error" role="alert">{error}</p>}

      {loading ? (
        <div className="table-skeleton" aria-label={t('loadingLabel')}>
          {[0, 1, 2, 3].map((item) => <span key={item} className="skeleton-row" />)}
        </div>
      ) : filtered.length === 0 ? (
        <EmptyState
          title={users.length === 0 ? t('noUsers') : t('noMatchUsers')}
          hint={users.length === 0 ? t('noUsersHint') : t('noMatchUsersHint')}
          action={
            users.length === 0 && !readOnly ? (
              <button className="btn primary" onClick={() => setCreateOpen(true)}>
                <Icon name="plus" /> {t('newUser')}
              </button>
            ) : undefined
          }
        />
      ) : (
        <div className="table-card">
          <table className="table">
            <thead>
              <tr>
                <th>{t('colUser')}</th>
                <th>{t('colRole')}</th>
                <th>{t('colStatus')}</th>
                <th>{t('colLastLogin')}</th>
                <th className="right">{t('colActions')}</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((user) => (
                <tr key={user.id}>
                  <td>
                    <div className="user-cell">
                      <span className="avatar" aria-hidden="true">{initials(user.username)}</span>
                      <div>
                        <strong>{user.username}</strong>
                        <small>{user.email || t('emailUnset')}</small>
                      </div>
                    </div>
                  </td>
                  <td>
                    <span className={`pill role-${user.role}`}>
                      {t(ROLE_KEYS[user.role] || 'roleMember')}
                    </span>
                  </td>
                  <td>
                    <span className={`pill ${user.status === 'active' ? 'ok' : 'bad'}`}>
                      {user.status === 'active' ? t('active') : t('disabled')}
                    </span>
                  </td>
                  <td className="muted">{formatDate(user.last_login_at, locale)}</td>
                  <td>
                    <div className="row-actions">
                      {!readOnly && (
                        <>
                          <button className="btn small" onClick={() => setSelected(user)}>{t('manage')}</button>
                          <button className="btn small ghost" onClick={() => setPendingStatus(user)}>
                            {user.status === 'active' ? t('disable') : t('enable')}
                          </button>
                        </>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {createOpen && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setCreateOpen(false)}>
          <form
            className="modal modal-form"
            role="dialog"
            aria-modal="true"
            aria-label={t('newUser')}
            onSubmit={createUser}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <h2>{t('newUser')}</h2>
            <p className="hint">{t('createUserHint')}</p>
            <div className="form-grid two">
              <label>
                {t('username')}
                <input
                  value={form.username}
                  onChange={(event) => setForm({ ...form, username: event.target.value })}
                  pattern="[A-Za-z0-9_.-]{1,64}"
                  autoFocus
                  required
                />
              </label>
              <label>
                {t('email')}
                <input
                  type="email"
                  value={form.email}
                  onChange={(event) => setForm({ ...form, email: event.target.value })}
                />
              </label>
              <label>
                {t('colRole')}
                <select
                  value={form.role}
                  onChange={(event) => setForm({ ...form, role: event.target.value as Role })}
                >
                  <option value="member">{t('roleMember')}</option>
                  <option value="approver">{t('roleApprover')}</option>
                  <option value="auditor">{t('roleAuditor')}</option>
                  <option value="admin">{t('roleAdmin')}</option>
                </select>
              </label>
              <label>
                {t('initialPassword')}
                <input
                  type="password"
                  minLength={8}
                  value={form.password}
                  onChange={(event) => setForm({ ...form, password: event.target.value })}
                  required
                />
              </label>
            </div>
            <div className="modal-actions">
              <button className="btn ghost" type="button" onClick={() => setCreateOpen(false)}>{t('cancel')}</button>
              <button className="btn primary" type="submit" disabled={busy}>
                {busy ? t('creating') : t('create')}
              </button>
            </div>
          </form>
        </div>
      )}

      <ConfirmDialog
        open={Boolean(pendingStatus)}
        title={pendingStatus?.status === 'active' ? t('disableUserTitle') : t('enableUserTitle')}
        message={
          pendingStatus?.status === 'active'
            ? t('disableUserMessage', { user: pendingStatus?.username })
            : t('enableUserMessage', { user: pendingStatus?.username })
        }
        confirmLabel={pendingStatus?.status === 'active' ? t('disable') : t('enable')}
        danger={pendingStatus?.status === 'active'}
        busy={busy}
        onCancel={() => setPendingStatus(null)}
        onConfirm={() => void toggleStatus()}
      />
    </section>
  );
}
