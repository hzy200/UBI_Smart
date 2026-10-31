import { useEffect, useState } from 'react';
import { api } from '../api';
import type {
  AdminUser,
  AuthEvent,
  ProviderKeyRow,
  Quota,
  UsageToday,
} from '../types';
import { Icon } from '../components/Icons';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { QuotaBar } from '../components/QuotaBar';
import type { Notify } from '../components/Toasts';
import { formatCost, formatDate, formatNumber, initials } from '../lib/format';
import { useAdminI18n } from '../i18n';

const EMPTY_USAGE: UsageToday = {
  day: '',
  input_tokens: 0,
  output_tokens: 0,
  worker_input_tokens: 0,
  worker_output_tokens: 0,
  tokens: 0,
  cost_usd: 0,
  billed_usd: 0,
  cost_cny: 0,
  billed_cny: 0,
  currency: 'CNY',
  runs: 0,
  reserved_tokens: 0,
  reserved_usd: 0,
};

export function UserDetail({
  user,
  notify,
  onBack,
}: {
  user: AdminUser;
  notify: Notify;
  onBack: () => void;
}) {
  const { t, locale } = useAdminI18n();
  const [quota, setQuota] = useState<Quota>({ daily_tokens: 0, daily_cost_cny: 0, enabled: false });
  const [usage, setUsage] = useState<UsageToday>(EMPTY_USAGE);
  const [providers, setProviders] = useState<ProviderKeyRow[]>([]);
  const [events, setEvents] = useState<AuthEvent[]>([]);
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [visible, setVisible] = useState<Record<string, boolean>>({});
  const [password, setPassword] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<string>('');
  const [busy, setBusy] = useState(false);

  async function loadAll() {
    try {
      const [quotaData, providerData, eventData] = await Promise.all([
        api.userQuota(user.id),
        api.userProviders(user.id),
        api.authEvents(user.username, 20),
      ]);
      setQuota(quotaData.quota);
      setUsage(quotaData.usage || EMPTY_USAGE);
      setProviders(providerData.providers);
      setEvents(eventData.events);
    } catch (err) {
      notify(String(err), 'error');
    }
  }

  useEffect(() => {
    void loadAll();
  }, [user.id]);

  async function saveQuota(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const data = await api.saveQuota(user.id, quota);
      setQuota(data.quota);
      notify(t('quotaSaved'));
      await loadAll();
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setBusy(false);
    }
  }

  async function saveKey(provider: string) {
    const value = (keys[provider] || '').trim();
    if (!value) return;
    setBusy(true);
    try {
      const data = await api.saveProviderKey(user.id, provider, value);
      setKeys({ ...keys, [provider]: '' });
      setVisible({ ...visible, [provider]: false });
      setProviders((rows) => rows.map((row) => (
        row.provider === provider ? { ...row, has_key: true, last4: data.last4 } : row
      )));
      notify(t('keySaved', { provider }));
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setBusy(false);
    }
  }

  async function deleteKey() {
    if (!deleteTarget) return;
    setBusy(true);
    try {
      await api.deleteProviderKey(user.id, deleteTarget);
      setProviders((rows) => rows.map((row) => (
        row.provider === deleteTarget ? { ...row, has_key: false, last4: '' } : row
      )));
      notify(t('keyDeleted', { provider: deleteTarget }));
      setDeleteTarget('');
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setBusy(false);
    }
  }

  async function resetPassword(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await api.resetPassword(user.id, password);
      setPassword('');
      notify(t('passwordReset'));
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="page">
      <div className="detail-head">
        <button className="btn ghost small" onClick={onBack}>
          <Icon name="back" /> {t('detailBack')}
        </button>
        <div className="detail-identity">
          <span className="avatar large" aria-hidden="true">{initials(user.username)}</span>
          <div>
            <h2>{user.username}</h2>
            <p>
              <span className={`pill role-${user.role}`}>
                {t(
                  user.role === 'owner'
                    ? 'roleOwner'
                    : user.role === 'admin'
                      ? 'roleAdmin'
                      : user.role === 'approver'
                        ? 'roleApprover'
                        : user.role === 'auditor'
                          ? 'roleAuditor'
                          : 'roleMember',
                )}
              </span>
              <span className={`pill ${user.status === 'active' ? 'ok' : 'bad'}`}>
                {user.status === 'active' ? t('active') : t('disabled')}
              </span>
              <span className="muted">{user.email || t('emailUnset')}</span>
            </p>
          </div>
        </div>
      </div>

      <div className="detail-grid">
        <div className="card">
          <div className="card-head">
            <div>
              <h3>{t('todayUsageTitle')}</h3>
              <p className="hint">{formatDate(usage.day, locale)} · {t('runsCount', { count: usage.runs })}</p>
            </div>
            <span className="pill">{formatNumber(usage.tokens, locale)} tokens</span>
          </div>
          <QuotaBar
            label="Token"
            used={usage.tokens}
            limit={quota.enabled ? quota.daily_tokens : 0}
            format={(value: number) => formatNumber(value, locale)}
          />
          <QuotaBar
            label={t('cost')}
            used={usage.cost_cny}
            limit={quota.enabled ? quota.daily_cost_cny : 0}
            format={formatCost}
          />
          <dl className="mini-stats">
            <div><dt>{t('input')}</dt><dd>{formatNumber(usage.input_tokens, locale)}</dd></div>
            <div><dt>{t('output')}</dt><dd>{formatNumber(usage.output_tokens, locale)}</dd></div>
            <div><dt>{t('worker')}</dt><dd>{formatNumber(usage.worker_input_tokens + usage.worker_output_tokens, locale)}</dd></div>
            <div><dt>{t('reserved')}</dt><dd>{formatNumber(usage.reserved_tokens, locale)}</dd></div>
          </dl>
        </div>

        <form className="card" onSubmit={saveQuota}>
          <div className="card-head">
            <div>
              <h3>{t('dailyQuota')}</h3>
              <p className="hint">{t('dailyQuotaHint')}</p>
            </div>
            <label className="switch">
              <input
                type="checkbox"
                checked={quota.enabled}
                onChange={(event) => setQuota({ ...quota, enabled: event.target.checked })}
              />
              <span>{t('enableQuota')}</span>
            </label>
          </div>
          <label>
            {t('dailyTokens')}
            <input
              type="number"
              min={0}
              value={quota.daily_tokens}
              onChange={(event) => setQuota({ ...quota, daily_tokens: Number(event.target.value) })}
            />
          </label>
          <label>
            {t('dailyCost')}
            <input
              type="number"
              min={0}
              step="0.01"
              value={quota.daily_cost_cny}
              onChange={(event) => setQuota({ ...quota, daily_cost_cny: Number(event.target.value) })}
            />
          </label>
          <button className="btn primary" type="submit" disabled={busy}>{t('saveQuota')}</button>
        </form>
      </div>

      <div className="card">
        <div className="card-head">
          <div>
            <h3>{t('providerKeys')}</h3>
            <p className="hint">{t('providerKeysHint')}</p>
          </div>
          <Icon name="key" />
        </div>
        <div className="key-list">
          {providers.map((row) => (
            <div className="key-row" key={row.provider}>
              <div className="key-info">
                <strong>{row.provider}</strong>
                <span className="muted">{row.model || t('none')} · {row.base_url || t('envDefault')}</span>
                <span className={`pill ${row.has_key ? 'ok' : ''}`}>
                  {row.has_key ? t('keyConfigured', { last4: row.last4 || '****' }) : t('keyUnconfigured')}
                </span>
              </div>
              <div className="key-actions">
                <span className="password-wrap">
                  <input
                    type={visible[row.provider] ? 'text' : 'password'}
                    autoComplete="off"
                    placeholder={t('keyPlaceholder')}
                    value={keys[row.provider] || ''}
                    onChange={(event) => setKeys({ ...keys, [row.provider]: event.target.value })}
                  />
                  <button
                    type="button"
                    className="password-toggle"
                    aria-label={visible[row.provider] ? t('hideKey') : t('showKey')}
                    onClick={() => setVisible({ ...visible, [row.provider]: !visible[row.provider] })}
                  >
                    <Icon name={visible[row.provider] ? 'eye-off' : 'eye'} />
                  </button>
                </span>
                <button className="btn small primary" onClick={() => void saveKey(row.provider)} disabled={busy}>
                  {t('save')}
                </button>
                <button
                  className="btn small ghost"
                  disabled={!row.has_key || busy}
                  onClick={() => setDeleteTarget(row.provider)}
                >
                  {t('delete')}
                </button>
              </div>
            </div>
          ))}
          {providers.length === 0 && (
            <p className="muted">{t('noProviders')}</p>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <div>
            <h3>{t('authEvents')}</h3>
            <p className="hint">{t('authEventsHint')}</p>
          </div>
          <Icon name="shield" />
        </div>
        <div className="event-list">
          {events.map((event) => (
            <div className="event-row" key={event.id}>
              <span className={`pill ${eventTone(event.event)}`}>
                {eventLabel(event.event, t)}
              </span>
              <span className="mono muted">{formatDate(event.ts, locale)}</span>
              <span className="muted">{event.ip || '—'}</span>
              <span className="muted mono event-req">{event.request_id || ''}</span>
            </div>
          ))}
          {events.length === 0 && <p className="muted">{t('noAuthEvents')}</p>}
        </div>
      </div>

      <form className="card narrow" onSubmit={resetPassword}>
        <div className="card-head">
          <div>
            <h3>{t('resetPassword')}</h3>
            <p className="hint">{t('resetPasswordHint')}</p>
          </div>
        </div>
        <label>
          {t('newPassword')}
          <input
            type="password"
            minLength={8}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder={t('loginPasswordPlaceholder')}
            required
          />
        </label>
        <button className="btn" type="submit" disabled={busy}>{t('resetPasswordButton')}</button>
      </form>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title={t('deleteKeyTitle')}
        message={t('deleteKeyMessage', { provider: deleteTarget })}
        confirmLabel={t('delete')}
        danger
        busy={busy}
        onCancel={() => setDeleteTarget('')}
        onConfirm={() => void deleteKey()}
      />
    </section>
  );
}

function eventLabel(
  event: string,
  t: (key: string) => string,
): string {
  return (
    {
      login_ok: t('loginOk'),
      login_failed: t('loginFailed'),
      login_rate_limited: t('loginRateLimited'),
      logout: t('logoutEvent'),
    }[event] || event
  );
}

function eventTone(event: string): string {
  if (event === 'login_ok') return 'ok';
  if (event === 'logout') return '';
  return 'bad';
}
