import { useEffect, useState } from 'react';
import { api, AuthError, loadRuntimeConfig } from './api';
import type { Me } from './types';
import { ErrorBoundary } from './components/ErrorBoundary';
import { Icon } from './components/Icons';
import { ToastStack, useToasts } from './components/Toasts';
import { formatCost, formatNumber } from './lib/format';
import { Login } from './pages/Login';
import { Users } from './pages/Users';
import { Usage } from './pages/Usage';
import { Providers } from './pages/Providers';
import { Approvals } from './pages/Approvals';

type View = 'users' | 'usage' | 'providers' | 'approvals';

const ACCESS_ROLES = ['owner', 'admin', 'approver', 'auditor'];

// 后端才是授权边界；这里只按角色隐藏入口，避免审计/审批账号
// 点进去处处 403。
const NAV: Array<{
  key: View;
  label: string;
  hint: string;
  icon: 'users' | 'usage' | 'provider' | 'shield';
  roles: string[];
}> = [
  {
    key: 'users',
    label: '用户',
    hint: '账号、额度与上游 Key',
    icon: 'users',
    roles: ['owner', 'admin', 'auditor'],
  },
  {
    key: 'usage',
    label: '用量',
    hint: '按用户与日期汇总',
    icon: 'usage',
    roles: ['owner', 'admin', 'auditor'],
  },
  {
    key: 'providers',
    label: 'Provider',
    hint: '部署可用的模型供应商',
    icon: 'provider',
    roles: ['owner', 'admin', 'auditor'],
  },
  {
    key: 'approvals',
    label: '审批',
    hint: '四眼审批队列（L3）',
    icon: 'shield',
    roles: ['owner', 'admin', 'approver'],
  },
];

const ROLE_LABELS: Record<string, string> = {
  owner: '所有者',
  admin: '管理员',
  approver: '审批人',
  auditor: '审计员',
  member: '成员',
};

export function App() {
  const [ready, setReady] = useState(false);
  const [me, setMe] = useState<Me | null>(null);
  const [view, setView] = useState<View>('users');
  const [navOpen, setNavOpen] = useState(false);
  const [error, setError] = useState('');
  const { toasts, notify, dismiss } = useToasts();

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
      notify('登录成功');
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
        正在加载管理控制台…
      </div>
    );
  }
  if (error) {
    return (
      <div className="boot boot-error">
        <Icon name="warning" />
        <span>{error}</span>
        <button className="btn" onClick={() => window.location.reload()}>重新加载</button>
      </div>
    );
  }
  if (!me) return <Login onSuccess={handleLogin} />;
  if (!ACCESS_ROLES.includes(me.user.role)) {
    return (
      <div className="boot boot-error">
        <Icon name="shield" />
        当前账号（{me.user.username}）没有控制台权限。
        <button className="btn ghost" onClick={handleLogout}>退出</button>
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
      <aside className="sidebar" aria-label="管理导航">
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
            <small>企业控制台</small>
          </div>
          <button
            className="menu-btn"
            aria-label="关闭导航"
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
              <span>{item.label}</span>
            </button>
          ))}
        </nav>

        <div className="sidebar-note">
          <Icon name="database" />
          <div>
            <strong>数据存储</strong>
            <span>账号、额度、用量与上游 Key 均写入控制面数据库。</span>
          </div>
        </div>

        <div className="sidebar-foot">
          <span className="avatar" aria-hidden="true">{me.user.username.slice(0, 2).toUpperCase()}</span>
          <div className="who">
            <strong>{me.user.username}</strong>
            <small>{ROLE_LABELS[me.user.role] || me.user.role}</small>
          </div>
          <button className="icon-btn" onClick={handleLogout} title="退出登录" aria-label="退出登录">
            <Icon name="logout" />
          </button>
        </div>
      </aside>

      <main className="content">
        <header className="topbar">
          <button
            className="menu-btn topbar-menu"
            aria-label="打开导航"
            onClick={() => setNavOpen(true)}
          >
            <Icon name="menu" />
          </button>
          <div className="topbar-title">
            <h1>{active.label}</h1>
            <p>{active.hint}</p>
          </div>
          <div className="topbar-usage" title="当前管理员今日用量">
            <span>
              <strong>{formatNumber(usage.tokens)}</strong> tokens
            </span>
            <span className="divider" aria-hidden="true" />
            <span><strong>{formatCost(usage.cost_cny)}</strong></span>
            {quota.enabled && quota.daily_tokens > 0 && (
              <span className="pill">
                限额 {formatNumber(quota.daily_tokens)}
              </span>
            )}
          </div>
        </header>

        <div className="page-wrap">
          {/* 每个页面单独包一层：某一页崩了不该带走整个控制台。 */}
          {active.key === 'users' && (
            <ErrorBoundary label="用户">
              <Users notify={notify} readOnly={readOnly} />
            </ErrorBoundary>
          )}
          {active.key === 'usage' && (
            <ErrorBoundary label="用量">
              <Usage notify={notify} />
            </ErrorBoundary>
          )}
          {active.key === 'providers' && (
            <ErrorBoundary label="Provider">
              <Providers notify={notify} />
            </ErrorBoundary>
          )}
          {active.key === 'approvals' && (
            <ErrorBoundary label="审批">
              <Approvals notify={notify} />
            </ErrorBoundary>
          )}
        </div>
      </main>

      <button className="nav-scrim" aria-label="关闭导航" onClick={() => setNavOpen(false)} />
      <ToastStack toasts={toasts} onDismiss={dismiss} />
    </div>
  );
}
