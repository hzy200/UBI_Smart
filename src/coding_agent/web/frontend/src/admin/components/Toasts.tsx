import { useCallback, useRef, useState } from 'react';

export type ToastKind = 'success' | 'error' | 'info';
export type Notify = (text: string, kind?: ToastKind) => void;

export interface ToastItem {
  id: number;
  text: string;
  kind: ToastKind;
}

export function useToasts() {
  const [toasts, setToasts] = useState<ToastItem[]>([]);
  const seq = useRef(0);

  const dismiss = useCallback((id: number) => {
    setToasts((items) => items.filter((item) => item.id !== id));
  }, []);

  const notify = useCallback((text: string, kind: ToastKind = 'success') => {
    const id = ++seq.current;
    setToasts((items) => [...items, { id, text, kind }]);
    window.setTimeout(() => {
      setToasts((items) => items.filter((item) => item.id !== id));
    }, 3600);
  }, []);

  return { toasts, notify, dismiss };
}

export function ToastStack({
  toasts,
  onDismiss,
}: {
  toasts: ToastItem[];
  onDismiss: (id: number) => void;
}) {
  return (
    <div className="toast-stack" role="status" aria-live="polite">
      {toasts.map((item) => (
        <button
          key={item.id}
          className={`toast toast-${item.kind}`}
          onClick={() => onDismiss(item.id)}
          title="点击关闭"
        >
          <span className="toast-dot" aria-hidden="true" />
          <span>{item.text}</span>
        </button>
      ))}
    </div>
  );
}
