import { useEffect, useState } from 'react';
import { api } from '../api';
import { Icon } from '../components/Icons';

export function Login({ onSuccess }: { onSuccess: () => void | Promise<void> }) {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [visible, setVisible] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [mfaToken, setMfaToken] = useState('');
  const [mfaCode, setMfaCode] = useState('');
  const [oidcEnabled, setOidcEnabled] = useState(false);

  useEffect(() => {
    void api.oidcStatus()
      .then((data) => setOidcEnabled(Boolean(data.enabled)))
      .catch(() => setOidcEnabled(false));
  }, []);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      const result = await api.login(username.trim(), password);
      if (result.mfa_required) {
        setMfaToken(String(result.mfa_token || ''));
        setPassword('');
        return;
      }
      await onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function verifyMfa(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError('');
    try {
      await api.mfaVerify(mfaToken, mfaCode.trim());
      await onSuccess();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function startSso() {
    setError('');
    try {
      const data = await api.oidcStart('/admin');
      if (!data.authorization_url) throw new Error('SSO 登录地址为空');
      window.location.assign(data.authorization_url);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <div className="login-page">
      <form
        className="login-card"
        onSubmit={mfaToken ? verifyMfa : submit}
      >
        <div className="brand">
          <span className="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24"><path d="M7 6l5 6-5 6" /><path d="M14 18h4" /></svg>
          </span>
          <div>
            <strong>CodingAgent</strong>
            <small>企业控制台</small>
          </div>
        </div>
        {mfaToken ? (
          <>
            <p className="login-lead">请输入动态码或恢复码。</p>
            <label>
              动态码
              <input
                autoFocus
                autoComplete="one-time-code"
                value={mfaCode}
                onChange={(event) => setMfaCode(event.target.value)}
                placeholder="6 位动态码或恢复码"
                required
              />
            </label>
          </>
        ) : (
          <>
            <p className="login-lead">使用企业管理员分配的账号登录。</p>
            <label>
              用户名
              <input
                autoFocus
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                placeholder="例如 admin"
                required
              />
            </label>
            <label>
              密码
              <span className="password-wrap">
                <input
                  type={visible ? 'text' : 'password'}
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder="至少 8 位"
                  required
                />
                <button
                  type="button"
                  className="password-toggle"
                  aria-label={visible ? '隐藏密码' : '显示密码'}
                  onClick={() => setVisible((value) => !value)}
                >
                  <Icon name={visible ? 'eye-off' : 'eye'} />
                </button>
              </span>
            </label>
          </>
        )}
        {error && <p className="error" role="alert">{error}</p>}
        <button className="btn primary login-submit" type="submit" disabled={busy}>
          {busy ? '处理中…' : mfaToken ? '验证并登录' : '登录'}
        </button>
        {!mfaToken && oidcEnabled && (
          <button
            type="button"
            className="btn login-submit"
            onClick={() => void startSso()}
          >
            使用 SSO 登录
          </button>
        )}
        <p className="hint">忘记密码请联系企业管理员重置。</p>
      </form>
    </div>
  );
}
