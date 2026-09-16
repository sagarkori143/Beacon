"use client";

import { useCallback, useEffect, useState } from "react";

import { useToast } from "@/components/Toast";
import TopBar from "@/components/TopBar";
import { ApiError, api } from "@/lib/api/client";

type Operator = { id: string; email: string; full_name: string | null; is_active: boolean };

export default function Operators() {
  const toast = useToast();
  const [me, setMe] = useState<{ email: string } | null>(null);

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setMe(await api<{ email: string }>("owner", "auth/me"));
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not load your account.");
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const created = await api<Operator>("owner", "operators", {
        method: "POST",
        body: JSON.stringify({ email: email.trim(), password }),
      });
      toast("ok", `${created.email} can now sign in to the platform console.`);
      setEmail("");
      setPassword("");
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not add the operator.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="shell narrow">
      <TopBar console_="owner" />

      <section style={{ marginBottom: 20 }}>
        <span className="eyebrow">Signed in as {me?.email ?? "…"}</span>
        <h1 style={{ fontSize: 24, marginTop: 8 }}>Platform operators</h1>
        <p className="lede">
          Operators create organizations and the people inside them. They deliberately cannot
          read any organization&rsquo;s documents, searches or conversations.
        </p>
      </section>


      <section className="card">
        <div className="card-head">
          <h2>Add an operator</h2>
          <p className="hint">
            The very first one is created with <span className="mono">scripts/create_owner.py</span>,
            not here — an endpoint that mints the first all-powerful account is an open door
            until somebody remembers to close it.
          </p>
        </div>

        <form onSubmit={create}>
          <div className="row">
            <div className="field">
              <label htmlFor="o-email">Email</label>
              <input
                id="o-email"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="o-pass">Password (at least 12 characters)</label>
              <input
                id="o-pass"
                required
                minLength={12}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
          </div>
          <button className="primary" disabled={busy}>
            {busy && <span className="spinner" />}
            Add operator
          </button>
        </form>
      </section>
    </div>
  );
}
