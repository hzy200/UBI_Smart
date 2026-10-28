import { useEffect, useState } from 'react';
import { api } from '../api';
import type { UsageReport } from '../types';
import { Icon } from '../components/Icons';
import { EmptyState } from '../components/EmptyState';
import type { Notify } from '../components/Toasts';
import { dayString, daysAgo, formatCost, formatNumber } from '../lib/format';

export function Usage({ notify }: { notify: Notify }) {
  const [from, setFrom] = useState(dayString(daysAgo(6)));
  const [to, setTo] = useState(dayString(new Date()));
  const [user, setUser] = useState('');
  const [report, setReport] = useState<UsageReport | null>(null);
  const [loading, setLoading] = useState(true);

  async function load(nextFrom = from, nextTo = to, nextUser = user) {
    setLoading(true);
    try {
      setReport(await api.usage(nextFrom, nextTo, nextUser.trim()));
    } catch (err) {
      notify(String(err), 'error');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  function preset(days: number) {
    const nextFrom = dayString(daysAgo(days - 1));
    const nextTo = dayString(new Date());
    setFrom(nextFrom);
    setTo(nextTo);
    void load(nextFrom, nextTo);
  }

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <p className="hint">成本按人民币展示；上游价格仍以 USD 记账，按配置汇率换算。</p>
        </div>
      </div>

      <form
        className="toolbar card"
        onSubmit={(event) => {
          event.preventDefault();
          void load();
        }}
      >
        <div className="date-presets" role="group" aria-label="日期快捷选择">
          <button type="button" className="btn small ghost" onClick={() => preset(7)}>近 7 天</button>
          <button type="button" className="btn small ghost" onClick={() => preset(30)}>近 30 天</button>
          <button type="button" className="btn small ghost" onClick={() => preset(90)}>近 90 天</button>
        </div>
        <label className="field-inline">
          从
          <input type="date" value={from} onChange={(event) => setFrom(event.target.value)} />
        </label>
        <label className="field-inline">
          到
          <input type="date" value={to} onChange={(event) => setTo(event.target.value)} />
        </label>
        <label className="field-inline">
          用户
          <input
            value={user}
            onChange={(event) => setUser(event.target.value)}
            placeholder="留空表示全部"
          />
        </label>
        <span className="spacer" />
        <button className="btn primary" type="submit" disabled={loading}>
          <Icon name="search" /> {loading ? '查询中…' : '查询'}
        </button>
      </form>

      {report && (
        <div className="stat-grid">
          <Stat label="总 tokens" value={formatNumber(report.totals.tokens)} tone="accent" />
          <Stat label="成本" value={formatCost(report.totals.cost_cny)} />
          <Stat label="运行次数" value={formatNumber(report.totals.runs)} />
          <Stat label="预留 tokens" value={formatNumber(report.totals.reserved_tokens)} />
        </div>
      )}

      {loading ? (
        <div className="table-skeleton" aria-label="加载中">
          {[0, 1, 2, 3, 4].map((item) => <span key={item} className="skeleton-row" />)}
        </div>
      ) : !report || report.rows.length === 0 ? (
        <EmptyState
          title="该区间没有用量"
          hint="运行任务后，token 与成本会按用户、按天汇总到这里。"
        />
      ) : (
        <div className="table-card">
          <table className="table">
            <thead>
              <tr>
                <th>日期</th>
                <th>用户</th>
                <th className="right">输入</th>
                <th className="right">输出</th>
                <th className="right">Worker</th>
                <th className="right">成本</th>
                <th className="right">运行</th>
              </tr>
            </thead>
            <tbody>
              {report.rows.map((row) => (
                <tr key={`${row.day}:${row.user}`}>
                  <td className="mono">{row.day}</td>
                  <td><span className="pill">{row.user}</span></td>
                  <td className="right">{formatNumber(row.input_tokens)}</td>
                  <td className="right">{formatNumber(row.output_tokens)}</td>
                  <td className="right">{formatNumber(row.worker_input_tokens + row.worker_output_tokens)}</td>
                  <td className="right">{formatCost(row.cost_cny)}</td>
                  <td className="right">{formatNumber(row.runs)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Stat({ label, value, tone = '' }: { label: string; value: string; tone?: string }) {
  return (
    <div className={`stat ${tone}`}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}
