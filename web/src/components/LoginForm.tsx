"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

type Props = {
  console_: "owner" | "admin";
  title: string;
  hint: string;
  otherHref: string;
  otherLabel: string;
};

export default function LoginForm({ console_, title, hint, otherHref, otherLabel }: Props) {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);

    const response = await fetch(`/api/${console_}/session`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ email, password }),
    });

    if (!response.ok) {
      const problem = await response.json().catch(() => ({}));
      setError(problem.detail ?? "Sign in failed.");
      setBusy(false);
      return;
    }
    router.replace(`/${console_}`);
    router.refresh();
  }

  return (
    <main className="center">
      <form className="card" onSubmit={submit}>
        <div className="brand" style={{ marginBottom: 4 }}>
          <h1>Beacon</h1>
          <span className="badge">{console_ === "owner" ? "platform" : "organization"}</span>
        </div>
        <h2 style={{ marginTop: 12 }}>{title}</h2>
        <p className="hint">{hint}</p>

        {error && <div className="notice error">{error}</div>}

        <div className="field">
          <label htmlFor="email">Email</label>
          <input
            id="email"
            type="email"
            autoComplete="username"
            required
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="password">Password</label>
          <input
            id="password"
            type="password"
            autoComplete="current-password"
            required
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>

        <button className="primary" type="submit" disabled={busy} style={{ width: "100%" }}>
          {busy ? "Signing in…" : "Sign in"}
        </button>

        <p className="hint" style={{ margin: "16px 0 0", textAlign: "center" }}>
          <a href={otherHref}>{otherLabel}</a>
        </p>
      </form>
    </main>
  );
}
