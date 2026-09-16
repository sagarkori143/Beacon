"use client";

import { useEffect, useState } from "react";

type Props = {
  open: boolean;
  title: string;
  body: string;
  confirmLabel?: string;
  danger?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
};

/**
 * A deliberate pause before something hard to undo.
 *
 * Used where an action changes what the public site answers -- archiving a
 * document, disabling a person -- rather than on everything, because a
 * confirmation on every button teaches people to dismiss them unread.
 */
export default function Confirm({
  open,
  title,
  body,
  confirmLabel = "Confirm",
  danger,
  onConfirm,
  onCancel,
}: Props) {
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) setBusy(false);
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onCancel();
    }
    if (open) document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onCancel]);

  if (!open) return null;

  return (
    <div className="scrim" onClick={onCancel}>
      <div className="dialog" onClick={(e) => e.stopPropagation()} role="dialog" aria-modal>
        <h2>{title}</h2>
        <p className="hint" style={{ marginBottom: 20 }}>
          {body}
        </p>
        <div className="actions" style={{ justifyContent: "flex-end" }}>
          <button className="ghost" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            className={danger ? "danger" : "primary"}
            disabled={busy}
            onClick={() => {
              setBusy(true);
              onConfirm();
            }}
          >
            {busy && <span className="spinner" />}
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  );
}
