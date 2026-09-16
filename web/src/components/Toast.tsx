"use client";

import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react";

type Tone = "ok" | "error" | "info";

type Toast = {
  id: number;
  tone: Tone;
  message: string;
  /** Shown once, copyable, and never auto-dismissed -- generated passwords. */
  secret?: string;
};

type Push = (tone: Tone, message: string, secret?: string) => void;

const ToastContext = createContext<Push>(() => {});

/** Announce something without moving the page underneath the reader.
 *
 * Inline notices reflow the layout the moment they appear, which moves the
 * button someone is about to click. These float instead.
 */
export function useToast(): Push {
  return useContext(ToastContext);
}

const TONE_ICON: Record<Tone, string> = { ok: "✓", error: "!", info: "·" };

export default function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const next = useRef(1);

  const dismiss = useCallback((id: number) => {
    setToasts((prior) => prior.filter((t) => t.id !== id));
  }, []);

  const push = useCallback<Push>((tone, message, secret) => {
    const id = next.current++;
    setToasts((prior) => [...prior, { id, tone, message, secret }]);
  }, []);

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((toast) => (
          <ToastRow key={toast.id} toast={toast} onDismiss={() => dismiss(toast.id)} />
        ))}
      </div>
    </ToastContext.Provider>
  );
}

function ToastRow({ toast, onDismiss }: { toast: Toast; onDismiss: () => void }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    // A one-time secret stays until it is dismissed on purpose: it cannot be
    // shown again, so taking it away on a timer would lose it.
    if (toast.secret) return;
    const timer = setTimeout(onDismiss, toast.tone === "error" ? 8000 : 4500);
    return () => clearTimeout(timer);
  }, [toast, onDismiss]);

  return (
    <div className={`toast ${toast.tone}`}>
      <span className="toast-icon">{TONE_ICON[toast.tone]}</span>
      <div className="toast-body">
        <div>{toast.message}</div>
        {toast.secret && (
          <>
            <div className="secret-value" style={{ marginTop: 8 }}>
              {toast.secret}
            </div>
            <button
              className="small"
              style={{ marginTop: 8 }}
              onClick={async () => {
                try {
                  await navigator.clipboard.writeText(toast.secret!);
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1800);
                } catch {
                  setCopied(false);
                }
              }}
            >
              {copied ? "Copied" : "Copy"}
            </button>
          </>
        )}
      </div>
      <button className="toast-close ghost small" onClick={onDismiss} aria-label="Dismiss">
        ✕
      </button>
    </div>
  );
}
