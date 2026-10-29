import { useEffect, useRef } from 'react';
import { Icon } from './Icons';
import { useAdminI18n } from '../i18n';

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = '确认',
  danger = false,
  busy = false,
  onCancel,
  onConfirm,
}: {
  open: boolean;
  title: string;
  message: string;
  confirmLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const { t } = useAdminI18n();
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    cancelRef.current?.focus();
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onCancel}>
      <div
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-label={title}
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className={`modal-icon ${danger ? 'danger' : ''}`}>
          <Icon name={danger ? 'warning' : 'check'} />
        </div>
        <h2>{title}</h2>
        <p>{message}</p>
        <div className="modal-actions">
          <button ref={cancelRef} className="btn ghost" onClick={onCancel} disabled={busy}>
            {t('cancel')}
          </button>
          <button
            className={`btn ${danger ? 'danger-solid' : 'primary'}`}
            onClick={onConfirm}
            disabled={busy}
          >
            {busy ? t('processing') : confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
