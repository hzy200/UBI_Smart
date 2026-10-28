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
      notify('额度已保存');
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
      notify(`${provider} 的 Key 已保存`);
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
      notify(`${deleteTarget} 的 Key 已删除`);
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
      notify('密码已重置，现有会话已失效');
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
          <Icon name="back" /> 返回用户列表
        </button>
        <div className="detail-identity">
          <span className="avatar large" aria-hidden="true">{initials(user.username)}</span>
          <div>
            <h2>{user.username}</h2>
            <p>
              <span className={`pill role-${user.role}`}>
                {user.role === 'owner' ? '所有者' : user.role === 'admin' ? '管理员' : '成员'}
              </span>
              <span className={`pill ${user.status === 'active' ? 'ok' : 'bad'}`}>
                {user.status === 'active' ? '启用' : '禁用'}
              </span>
              <span className="muted">{user.email || '未设置邮箱'}</span>
            </p>
          </div>
        </div>
      </div>

      <div className="detail-grid">
        <div className="card">
          <div className="card-head">
            <div>
              <h3>今日用量</h3>
              <p className="hint">{formatDate(usage.day)} · {usage.runs} 次运行</p>
            </div>
            <span className="pill">{formatNumber(usage.tokens)} tokens</span>
          </div>
          <QuotaBar
            label="Token"
            used={usage.tokens}
            limit={quota.enabled ? quota.daily_tokens : 0}
            format={formatNumber}
          />
          <QuotaBar
            label="成本"
            used={usage.cost_cny}
            limit={quota.enabled ? quota.daily_cost_cny : 0}
            format={formatCost}
          />
          <dl className="mini-stats">
            <div><dt>输入</dt><dd>{formatNumber(usage.input_tokens)}</dd></div>
            <div><dt>输出</dt><dd>{formatNumber(usage.output_tokens)}</dd></div>
            <div><dt>Worker</dt><dd>{formatNumber(usage.worker_input_tokens + usage.worker_output_tokens)}</dd></div>
            <div><dt>预留</dt><dd>{formatNumber(usage.reserved_tokens)}</dd></div>
          </dl>
        </div>

        <form className="card" onSubmit={saveQuota}>
          <div className="card-head">
            <div>
              <h3>每日额度</h3>
              <p className="hint">0 表示不限制；按 UTC 日结算。</p>
            </div>
            <label className="switch">
              <input
                type="checkbox"
                checked={quota.enabled}
                onChange={(event) => setQuota({ ...quota, enabled: event.target.checked })}
              />
              <span>启用</span>
            </label>
          </div>
          <label>
            每日 token 上限
            <input
              type="number"
              min={0}
              value={quota.daily_tokens}
              onChange={(event) => setQuota({ ...quota, daily_tokens: Number(event.target.value) })}
            />
          </label>
          <label>
            每日成本上限（CNY）
            <input
              type="number"
              min={0}
              step="0.01"
              value={quota.daily_cost_cny}
              onChange={(event) => setQuota({ ...quota, daily_cost_cny: Number(event.target.value) })}
            />
          </label>
          <button className="btn primary" type="submit" disabled={busy}>保存额度</button>
        </form>
      </div>

      <div className="card">
        <div className="card-head">
          <div>
            <h3>上游模型 Key</h3>
            <p className="hint">AES-256-GCM 加密保存，只显示后四位；用户端不会拿到明文。</p>
          </div>
          <Icon name="key" />
        </div>
        <div className="key-list">
          {providers.map((row) => (
            <div className="key-row" key={row.provider}>
              <div className="key-info">
                <strong>{row.provider}</strong>
                <span className="muted">{row.model || '—'} · {row.base_url || '环境默认'}</span>
                <span className={`pill ${row.has_key ? 'ok' : ''}`}>
                  {row.has_key ? `已配置 · …${row.last4 || '****'}` : '未配置'}
                </span>
              </div>
              <div className="key-actions">
                <span className="password-wrap">
                  <input
                    type={visible[row.provider] ? 'text' : 'password'}
                    autoComplete="off"
                    placeholder="粘贴新的 API Key"
                    value={keys[row.provider] || ''}
                    onChange={(event) => setKeys({ ...keys, [row.provider]: event.target.value })}
                  />
                  <button
                    type="button"
                    className="password-toggle"
                    aria-label={visible[row.provider] ? '隐藏 Key' : '显示 Key'}
                    onClick={() => setVisible({ ...visible, [row.provider]: !visible[row.provider] })}
                  >
                    <Icon name={visible[row.provider] ? 'eye-off' : 'eye'} />
                  </button>
                </span>
                <button className="btn small primary" onClick={() => void saveKey(row.provider)} disabled={busy}>
                  保存
                </button>
                <button
                  className="btn small ghost"
                  disabled={!row.has_key || busy}
                  onClick={() => setDeleteTarget(row.provider)}
                >
                  删除
                </button>
              </div>
            </div>
          ))}
          {providers.length === 0 && (
            <p className="muted">部署未配置任何上游 provider。</p>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-head">
          <div>
            <h3>登录安全事件</h3>
            <p className="hint">登录成功、失败、登出与限流；不含密码或 token。</p>
          </div>
          <Icon name="shield" />
        </div>
        <div className="event-list">
          {events.map((event) => (
            <div className="event-row" key={event.id}>
              <span className={`pill ${eventTone(event.event)}`}>
                {eventLabel(event.event)}
              </span>
              <span className="mono muted">{formatDate(event.ts)}</span>
              <span className="muted">{event.ip || '—'}</span>
              <span className="muted mono event-req">{event.request_id || ''}</span>
            </div>
          ))}
          {events.length === 0 && <p className="muted">还没有登录事件。</p>}
        </div>
      </div>

      <form className="card narrow" onSubmit={resetPassword}>
        <div className="card-head">
          <div>
            <h3>重置密码</h3>
            <p className="hint">重置后该用户的所有现有会话立即失效。</p>
          </div>
        </div>
        <label>
          新密码
          <input
            type="password"
            minLength={8}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="至少 8 位"
            required
          />
        </label>
        <button className="btn" type="submit" disabled={busy}>重置密码</button>
      </form>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="删除上游 Key"
        message={`删除 ${deleteTarget} 的 Key 后，该用户将无法使用这个 provider 运行任务。`}
        confirmLabel="删除"
        danger
        busy={busy}
        onCancel={() => setDeleteTarget('')}
        onConfirm={() => void deleteKey()}
      />
    </section>
  );
}

function eventLabel(event: string): string {
  return (
    {
      login_ok: '登录成功',
      login_failed: '登录失败',
      login_rate_limited: '触发限流',
      logout: '退出登录',
    }[event] || event
  );
}

function eventTone(event: string): string {
  if (event === 'login_ok') return 'ok';
  if (event === 'logout') return '';
  return 'bad';
}
