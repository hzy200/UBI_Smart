import { useEffect, useState } from 'react';
import { api } from '../api';
import { Icon } from '../components/Icons';
import { useAdminI18n } from '../i18n';

export function Login({ onSuccess }: { onSuccess: () => void | Promise<void> }) {
  const { t, locale, setLocale } = useAdminI18n();
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
      if (!data.authorization_url) throw new Error(t('loginSsoEmpty'));
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
            <strong>UBISmart</strong>
            <small>{t('brand')}</small>
          </div>
          <div className="segmented" role="group" aria-label={t('language')}>
            <button
              type="button"
              className={locale === 'zh-CN' ? 'active' : ''}
              onClick={() => setLocale('zh-CN')}
              aria-pressed={locale === 'zh-CN'}
            >
              中文
            </button>
            <button
              type="button"
              className={locale === 'en-US' ? 'active' : ''}
              onClick={() => setLocale('en-US')}
              aria-pressed={locale === 'en-US'}
            >
              EN
            </button>
          </div>
        </div>
        {mfaToken ? (
          <>
            <p className="login-lead">{t('loginMfaLead')}</p>
            <label>
              {t('loginMfaCode')}
              <input
                autoFocus
                autoComplete="one-time-code"
                value={mfaCode}
                onChange={(event) => setMfaCode(event.target.value)}
                placeholder={t('loginMfaPlaceholder')}
                required
              />
            </label>
          </>
        ) : (
          <>
            <p className="login-lead">{t('loginLead')}</p>
            <label>
              {t('loginUsername')}
              <input
                autoFocus
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                placeholder={t('loginUsernamePlaceholder')}
                required
              />
            </label>
            <label>
              {t('loginPassword')}
              <span className="password-wrap">
                <input
                  type={visible ? 'text' : 'password'}
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder={t('loginPasswordPlaceholder')}
                  required
                />
                <button
                  type="button"
                  className="password-toggle"
                  aria-label={visible ? t('loginHidePassword') : t('loginShowPassword')}
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
          {busy ? t('loginProcessing') : mfaToken ? t('loginVerify') : t('loginSubmit')}
        </button>
        {!mfaToken && oidcEnabled && (
          <button
            type="button"
            className="btn login-submit"
            onClick={() => void startSso()}
          >
            {t('loginSso')}
          </button>
        )}
        <p className="hint">{t('loginForgot')}</p>
      </form>
    </div>
  );
}
