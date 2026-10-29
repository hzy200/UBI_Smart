import { useEffect, useState } from 'react';
import { api } from '../api';
import { EmptyState } from '../components/EmptyState';
import { Icon } from '../components/Icons';
import type { Notify } from '../components/Toasts';
import type { ApprovalRow } from '../types';

export function Approvals({ notify }: { notify: Notify }) {
  const [rows, setRows] = useState<ApprovalRow[]>([]);
  const [enabled, setEnabled] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState('');

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
    const timer = window.setInterval(() => void load(), 5000);
    return () => window.clearInterval(timer);
  }, []);

  async function decide(id: string, approved: boolean) {
    setBusy(id);
    try {
      await api.decideApproval(id, approved);
      notify(approved ? '已批准' : '已拒绝');
      await load();
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
          title="四眼审批未启用"
          hint="设置 AGENT_APPROVAL_FOUR_EYES=true 后，L3 审批会出现在这里。"
        />
      </section>
    );
  }

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <p className="hint">
            发起人不能批准自己的 L3 操作；批准后请让发起人在会话里点击“刷新并继续”。
          </p>
        </div>
        <button className="btn ghost" onClick={() => void load()} disabled={loading}>
          <Icon name="refresh" /> 刷新
        </button>
      </div>
      {rows.length === 0 ? (
        <EmptyState title="没有待审批请求" hint="新的 L3 审批会自动出现在这里。" />
      ) : (
        <div className="card table-wrap">
          <table>
            <thead>
              <tr>
                <th>发起人</th>
                <th>等级</th>
                <th>工具</th>
                <th>命令</th>
                <th>时间</th>
                <th aria-label="操作" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={row.id}>
                  <td>{row.requested_by}</td>
                  <td><span className="pill">{row.level || `L${row.level_int}`}</span></td>
                  <td>{row.tool}</td>
                  <td><code className="mono">{row.command}</code></td>
                  <td>{row.created_at}</td>
                  <td className="row-actions">
                    <button
                      className="btn small primary"
                      disabled={busy === row.id}
                      onClick={() => void decide(row.id, true)}
                    >
                      批准
                    </button>
                    <button
                      className="btn small danger"
                      disabled={busy === row.id}
                      onClick={() => void decide(row.id, false)}
                    >
                      拒绝
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
