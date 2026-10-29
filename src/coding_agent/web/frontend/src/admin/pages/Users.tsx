import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { AdminUser } from '../types';
import { Icon } from '../components/Icons';
import { ConfirmDialog } from '../components/ConfirmDialog';
import { EmptyState } from '../components/EmptyState';
import type { Notify } from '../components/Toasts';
import { formatDate, initials } from '../lib/format';
import { UserDetail } from './UserDetail';

type StatusFilter = 'all' | 'active' | 'disabled';
type Role = 'admin' | 'approver' | 'auditor' | 'member';

const ROLE_LABELS: Record<string, string> = {
  owner: '所有者',
  admin: '管理员',
  approver: '审批人',
  auditor: '审计员',
  member: '成员',
};

export function Users({
  notify,
  readOnly = false,
}: {
  notify: Notify;
  readOnly?: boolean;
}) {
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
      notify('用户已创建');
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
      notify(pendingStatus.status === 'active' ? '用户已禁用' : '用户已启用');
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
          <p className="hint">注册企业成员，分配上游模型 Key 与每日 token / 成本额度。</p>
        </div>
        {!readOnly && (
          <button className="btn primary" onClick={() => setCreateOpen(true)}>
            <Icon name="plus" /> 新建用户
          </button>
        )}
      </div>

      <div className="toolbar card">
        <div className="search-box">
          <Icon name="search" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索用户名或邮箱"
            aria-label="搜索用户"
          />
        </div>
        <div className="segmented" role="group" aria-label="状态筛选">
          {(
            [
              ['all', '全部'],
              ['active', '启用'],
              ['disabled', '禁用'],
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
          <Icon name="refresh" /> 刷新
        </button>
      </div>

      {error && <p className="error" role="alert">{error}</p>}

      {loading ? (
        <div className="table-skeleton" aria-label="加载中">
          {[0, 1, 2, 3].map((item) => <span key={item} className="skeleton-row" />)}
        </div>
      ) : filtered.length === 0 ? (
        <EmptyState
          title={users.length === 0 ? '还没有用户' : '没有匹配的用户'}
          hint={users.length === 0 ? '创建第一个企业成员后即可分配额度。' : '调整搜索词或状态筛选。'}
          action={
            users.length === 0 && !readOnly ? (
              <button className="btn primary" onClick={() => setCreateOpen(true)}>
                <Icon name="plus" /> 新建用户
              </button>
            ) : undefined
          }
        />
      ) : (
        <div className="table-card">
          <table className="table">
            <thead>
              <tr>
                <th>用户</th>
                <th>角色</th>
                <th>状态</th>
                <th>最近登录</th>
                <th className="right">操作</th>
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
                        <small>{user.email || '未设置邮箱'}</small>
                      </div>
                    </div>
                  </td>
                  <td>
                    <span className={`pill role-${user.role}`}>
                      {ROLE_LABELS[user.role] || user.role}
                    </span>
                  </td>
                  <td>
                    <span className={`pill ${user.status === 'active' ? 'ok' : 'bad'}`}>
                      {user.status === 'active' ? '启用' : '禁用'}
                    </span>
                  </td>
                  <td className="muted">{formatDate(user.last_login_at)}</td>
                  <td>
                    <div className="row-actions">
                      {!readOnly && (
                        <>
                          <button className="btn small" onClick={() => setSelected(user)}>管理</button>
                          <button className="btn small ghost" onClick={() => setPendingStatus(user)}>
                            {user.status === 'active' ? '禁用' : '启用'}
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
            aria-label="新建用户"
            onSubmit={createUser}
            onMouseDown={(event) => event.stopPropagation()}
          >
            <h2>新建用户</h2>
            <p className="hint">密码只在创建时提交，服务端只保存 scrypt 哈希。</p>
            <div className="form-grid two">
              <label>
                用户名
                <input
                  value={form.username}
                  onChange={(event) => setForm({ ...form, username: event.target.value })}
                  pattern="[A-Za-z0-9_.-]{1,64}"
                  autoFocus
                  required
                />
              </label>
              <label>
                邮箱
                <input
                  type="email"
                  value={form.email}
                  onChange={(event) => setForm({ ...form, email: event.target.value })}
                />
              </label>
              <label>
                角色
                <select
                  value={form.role}
                  onChange={(event) => setForm({ ...form, role: event.target.value as Role })}
                >
                  <option value="member">成员</option>
                  <option value="approver">审批人</option>
                  <option value="auditor">审计员</option>
                  <option value="admin">管理员</option>
                </select>
              </label>
              <label>
                初始密码
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
              <button className="btn ghost" type="button" onClick={() => setCreateOpen(false)}>取消</button>
              <button className="btn primary" type="submit" disabled={busy}>
                {busy ? '创建中…' : '创建'}
              </button>
            </div>
          </form>
        </div>
      )}

      <ConfirmDialog
        open={Boolean(pendingStatus)}
        title={pendingStatus?.status === 'active' ? '禁用用户' : '启用用户'}
        message={
          pendingStatus?.status === 'active'
            ? `禁用 ${pendingStatus?.username} 后，其现有会话会立即失效。`
            : `启用 ${pendingStatus?.username} 后，该账号可以重新登录。`
        }
        confirmLabel={pendingStatus?.status === 'active' ? '禁用' : '启用'}
        danger={pendingStatus?.status === 'active'}
        busy={busy}
        onCancel={() => setPendingStatus(null)}
        onConfirm={() => void toggleStatus()}
      />
    </section>
  );
}
