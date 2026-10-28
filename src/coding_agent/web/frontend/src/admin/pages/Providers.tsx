import { useEffect, useState } from 'react';
import { api } from '../api';
import type { ProviderKeyRow } from '../types';
import { Icon } from '../components/Icons';
import { EmptyState } from '../components/EmptyState';
import type { Notify } from '../components/Toasts';

export function Providers({ notify }: { notify: Notify }) {
  const [rows, setRows] = useState<ProviderKeyRow[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    void (async () => {
      try {
        const data = await api.providers();
        setRows(data.providers);
      } catch (err) {
        notify(String(err), 'error');
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  return (
    <section className="page">
      <div className="page-head">
        <div>
          <p className="hint">
            Provider 列表来自部署环境（<code>AGENT_LLM_PROVIDERS</code>）；
            每个用户的 Key 在“用户 → 管理”中单独配置。
          </p>
        </div>
      </div>

      {loading ? (
        <div className="card-grid">
          {[0, 1, 2].map((item) => <span key={item} className="skeleton-card" />)}
        </div>
      ) : rows.length === 0 ? (
        <EmptyState title="部署未配置 provider" hint="在 AGENT_LLM_PROVIDERS 中配置后重启 Web 服务。" />
      ) : (
        <div className="card-grid">
          {rows.map((row) => (
            <div className="provider-card" key={row.provider}>
              <div className="provider-head">
                <span className="provider-icon"><Icon name="provider" /></span>
                <div>
                  <strong>{row.provider}</strong>
                  <small>{row.model || '未指定模型'}</small>
                </div>
              </div>
              <dl>
                <div>
                  <dt>Base URL</dt>
                  <dd className="mono">{row.base_url || '环境默认'}</dd>
                </div>
                <div>
                  <dt>环境 Key</dt>
                  <dd>{row.has_key ? '已配置' : '未配置'}</dd>
                </div>
              </dl>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
