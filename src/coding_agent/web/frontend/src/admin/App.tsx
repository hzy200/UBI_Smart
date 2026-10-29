import { useEffect, useState } from 'react';
import { api, AuthError, loadRuntimeConfig } from './api';
import type { Me } from './types';
import { ErrorBoundary } from './components/ErrorBoundary';
import { Icon } from './components/Icons';
import { ToastStack, useToasts } from './components/Toasts';
import { formatCost, formatNumber } from './lib/format';
import { useAdminI18n } from './i18n';
import { Login } from './pages/Login';
import { Users } from './pages/Users';
import { Usage } from './pages/Usage';
import { Audit } from './pages/Audit';
import { Health } from './pages/Health';
import { Workspaces } from './pages/Workspaces';
import { Providers } from './pages/Providers';
import { Approvals } from './pages/Approvals';

type View =
  | 'users'
  | 'usage'
  | 'audit'
  | 'health'
  | 'workspaces'
  | 'providers'
  | 'approvals';

const ACCESS_ROLES = ['owner', 'admin', 'approver', 'auditor'];

// 后端才是授权边界；这里只按角色隐藏入口，避免审计/审批账号
// 点进去处处 403。
const NAV: Array<{
  key: View;
  labelKey: string;
  hintKey: string;
  icon:
    | 'users'
    | 'usage'
    | 'provider'
    | 'shield'
    | 'eye'
    | 'database'
    | 'folder';
  roles: string[];
}> = [
  {
    key: 'users',
    labelKey: 'navUsers',
    hintKey: 'hintUsers',
    icon: 'users',
    roles: ['owner', 'admin', 'auditor'],
  },
  {
    key: 'workspaces',
    labelKey: 'navWorkspaces',
    hintKey: 'hintWorkspaces',
    icon: 'folder',
    roles: ['owner', 'admin', 'auditor'],
  },
  {
    key: 'usage',
    labelKey: 'navUsage',
    hintKey: 'hintUsage',
    icon: 'usage',
    roles: ['owner', 'admin', 'auditor'],
  },
  {
    key: 'audit',
    labelKey: 'navAudit',
    hintKey: 'hintAudit',
    icon: 'eye',
    roles: ['owner', 'admin', 'auditor'],
  },
  {
    key: 'health',
    labelKey: 'navHealth',
    hintKey: 'hintHealth',
    icon: 'database',
    roles: ['owner', 'admin', 'auditor'],
  },
  {
    key: 'providers',
    labelKey: 'navProviders',
    hintKey: 'hintProviders',
    icon: 'provider',
    roles: ['owner', 'admin', 'auditor'],
  },
  {
    key: 'approvals',
    labelKey: 'navApprovals',
    hintKey: 'hintApprovals',
    icon: 'shield',
    roles: ['owner', 'admin', 'approver'],
  },
];

const ROLE_KEYS: Record<string, string> = {
  owner: 'roleOwner',
  admin: 'roleAdmin',
  approver: 'roleApprover',
  auditor: 'roleAuditor',
  member: 'roleMember',
};

export function App() {
  const [ready, setReady] = useState(false);
  const [me, setMe] = useState<Me | null>(null);
  const [view, setView] = useState<View>('users');
  const [navOpen, setNavOpen] = useState(false);
  const [error, setError] = useState('');
  const { toasts, notify, dismiss } = useToasts();
  const { t, locale, setLocale } = useAdminI18n();

  async function refreshMe() {
    const data = await api.me();
    setMe(data);
  }

  useEffect(() => {
    void (async () => {
      await loadRuntimeConfig();
      try {
        await refreshMe();
      } catch (err) {
        if (!(err instanceof AuthError)) setError(String(err));
      } finally {
        setReady(true);
      }
    })();
  }, []);

  async function handleLogin() {
    try {
      await refreshMe();
      setError('');
      notify(t('loginOk'));
    } catch (err) {
      setError(String(err));
    }
  }

  async function handleLogout() {
    try {
      await api.logout();
    } catch (err) {
      // 服务端失败也要回登录页
    }
    setMe(null);
    setNavOpen(false);
  }

  if (!ready) {
    return (
      <div className="boot">
        <span className="spinner" aria-hidden="true" />
        {t('loading')}
      </div>
    );
  }
  if (error) {
    return (
      <div className="boot boot-error">
        <Icon name="warning" />
        <span>{error}</span>
        <button className="btn" onClick={() => window.location.reload()}>{t('reload')}</button>
      </div>
    );
  }
  if (!me) return <Login onSuccess={handleLogin} />;
  if (!ACCESS_ROLES.includes(me.user.role)) {
    return (
      <div className="boot boot-error">
        <Icon name="shield" />
        {t('noAccess', { user: me.user.username })}
        <button className="btn ghost" onClick={handleLogout}>{t('logout')}</button>
      </div>
    );
  }

  const visibleNav = NAV.filter((item) => item.roles.includes(me.user.role));
  const active = visibleNav.find((item) => item.key === view) || visibleNav[0];
  const readOnly = me.user.role === 'auditor';
  const quota = me.quotas;
  const usage = me.usage_today;

  return (
    <div className={`shell ${navOpen ? 'nav-open' : ''}`}>
      <aside className="sidebar" aria-label={t('brand')}>
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <img
              className="brand-logo"
              src="/static/icon-192.png"
              alt=""
              width="28"
              height="28"
            />
          </span>
          <div>
            <strong>CodingAgent</strong>
            <small>{t('brand')}</small>
          </div>
          <button
            className="menu-btn"
            aria-label={t('close')}
            onClick={() => setNavOpen(false)}
          >
            <Icon name="close" />
          </button>
        </div>

        <nav>
          {visibleNav.map((item) => (
            <button
              key={item.key}
              className={`nav-item ${active.key === item.key ? 'active' : ''}`}
              onClick={() => {
                setView(item.key);
                setNavOpen(false);
              }}
            >
              <Icon name={item.icon} />
              <span>{t(item.labelKey)}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-note">
          <Icon name="database" />
          <div>
            <strong>{t('storageTitle')}</strong>
            <span>{t('storageHint')}</span>
          </div>
        </div>

        <div className="sidebar-foot">
          <span className="avatar" aria-hidden="true">{me.user.username.slice(0, 2).toUpperCase()}</span>
          <div className="who">
            <strong>{me.user.username}</strong>
            <small>{t(ROLE_KEYS[me.user.role] || 'roleMember')}</small>
          </div>
          <button className="icon-btn" onClick={handleLogout} title={t('logout')} aria-label={t('logout')}>
            <Icon name="logout" />
          </button>
        </div>
      </aside>

      <main className="content">
        <header className="topbar">
          <button
            className="menu-btn topbar-menu"
            aria-label={t('openNav')}
            onClick={() => setNavOpen(true)}
          >
            <Icon name="menu" />
          </button>
          <div className="topbar-title">
            <h1>{t(active.labelKey)}</h1>
            <p>{t(active.hintKey)}</p>
          </div>
          <div className="segmented" role="group" aria-label={t('language')}>
            <button
              className={locale === 'zh-CN' ? 'active' : ''}
              onClick={() => setLocale('zh-CN')}
              aria-pressed={locale === 'zh-CN'}
            >
              中文
            </button>
            <button
              className={locale === 'en-US' ? 'active' : ''}
              onClick={() => setLocale('en-US')}
              aria-pressed={locale === 'en-US'}
            >
              EN
            </button>
          </div>
          <div className="topbar-usage" title={t('todayUsage')}>
            <span>
              <strong>{formatNumber(usage.tokens)}</strong> tokens
            </span>
            <span className="divider" aria-hidden="true" />
            <span><strong>{formatCost(usage.cost_cny)}</strong></span>
            {quota.enabled && quota.daily_tokens > 0 && (
              <span className="pill">
                {t('quotaLabel', {
                  value: formatNumber(quota.daily_tokens),
                })}
              </span>
            )}
          </div>
        </header>

        <div className="page-wrap">
          {/* 每个页面单独包一层：某一页崩了不该带走整个控制台。 */}
          {active.key === 'users' && (
            <ErrorBoundary label={t('navUsers')}>
              <Users notify={notify} readOnly={readOnly} />
            </ErrorBoundary>
          )}
          {active.key === 'workspaces' && (
            <ErrorBoundary label={t('navWorkspaces')}>
              <Workspaces notify={notify} readOnly={readOnly} />
            </ErrorBoundary>
          )}
          {active.key === 'usage' && (
            <ErrorBoundary label={t('navUsage')}>
              <Usage notify={notify} />
            </ErrorBoundary>
          )}
          {active.key === 'audit' && (
            <ErrorBoundary label={t('navAudit')}>
              <Audit notify={notify} />
            </ErrorBoundary>
          )}
          {active.key === 'health' && (
            <ErrorBoundary label={t('navHealth')}>
              <Health notify={notify} readOnly={readOnly} />
            </ErrorBoundary>
          )}
          {active.key === 'providers' && (
            <ErrorBoundary label={t('navProviders')}>
              <Providers notify={notify} />
            </ErrorBoundary>
          )}
          {active.key === 'approvals' && (
            <ErrorBoundary label={t('navApprovals')}>
              <Approvals notify={notify} />
            </ErrorBoundary>
          )}
        </div>
      </main>

      <button className="nav-scrim" aria-label={t('closeNav')} onClick={() => setNavOpen(false)} />
      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
