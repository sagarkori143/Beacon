"use client";

import { useCallback, useEffect, useState } from "react";

import { useToast } from "@/components/Toast";
import TopBar from "@/components/TopBar";
import { ApiError, api, type Page, type TenantUser } from "@/lib/api/client";

type Me = {
  user: TenantUser;
  organization: { id: string; name: string; slug: string };
  location: { id: string; name: string } | null;
};

type ResetResult = { user: TenantUser; password: string | null };

export default function AdminConsole() {
  const toast = useToast();
  const [me, setMe] = useState<Me | null>(null);
  const [page, setPage] = useState<Page<TenantUser> | null>(null);
  const [pending, setPending] = useState<string | null>(null);

  const [email, setEmail] = useState("");
  const [fullName, setFullName] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"USER" | "ADMIN">("USER");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [profile, users] = await Promise.all([
        api<Me>("admin", "auth/me"),
        api<Page<TenantUser>>("admin", "users?limit=200"),
      ]);
      setMe(profile);
      setPage(users);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not load this organization.");
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  /** Run one mutation, surfacing the backend's own message when it refuses. */
  async function run(id: string, action: () => Promise<void>) {
    setPending(id);
    try {
      await action();
      await load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "That did not work.");
    } finally {
      setPending(null);
    }
  }

  async function createUser(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await api<TenantUser>("admin", "auth/users", {
        method: "POST",
        body: JSON.stringify({
          email,
          password,
          role,
          full_name: fullName.trim() || null,
        }),
      });
      toast("ok", `${email} added.`);
      setEmail("");
      setFullName("");
      setPassword("");
      setRole("USER");
      await load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not create the user.");
    } finally {
      setBusy(false);
    }
  }

  const users = page?.items ?? [];

  return (
    <div className="shell">
      <TopBar console_="admin" subtitle={me?.organization.name} />


      <section className="card">
        <h2>Add someone</h2>
        <p className="hint">
          An <strong>administrator</strong> manages this organization. A <strong>user</strong>{" "}
          can search and chat over its knowledge, nothing else.
        </p>

        <form onSubmit={createUser}>
          <div className="row">
            <div className="field">
              <label htmlFor="u-email">Email</label>
              <input
                id="u-email"
                type="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="u-name">Name</label>
              <input
                id="u-name"
                value={fullName}
                onChange={(e) => setFullName(e.target.value)}
              />
            </div>
          </div>
          <div className="row">
            <div className="field">
              <label htmlFor="u-password">Password (at least 12 characters)</label>
              <input
                id="u-password"
                type="text"
                required
                minLength={12}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="u-role">Role</label>
              <select
                id="u-role"
                value={role}
                onChange={(e) => setRole(e.target.value as "USER" | "ADMIN")}
              >
                <option value="USER">User</option>
                <option value="ADMIN">Administrator</option>
              </select>
            </div>
          </div>
          <button className="primary" disabled={busy}>
            {busy ? "Adding…" : "Add person"}
          </button>
        </form>
      </section>

      <section className="card">
        <div className="spread" style={{ marginBottom: 14 }}>
          <div>
            <h2>People</h2>
            <p className="hint" style={{ margin: 0 }}>
              {page?.total ?? 0} in {me?.organization.name ?? "this organization"}
            </p>
          </div>
          <button className="small" onClick={() => void load()}>
            Refresh
          </button>
        </div>

        {page === null ? (
          <p className="empty">Loading…</p>
        ) : users.length === 0 ? (
          <p className="empty">Nobody here yet.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Person</th>
                  <th>Role</th>
                  <th>Status</th>
                  <th style={{ width: 1 }} />
                </tr>
              </thead>
              <tbody>
                {users.map((u) => {
                  const isMe = u.id === me?.user.id;
                  const working = pending === u.id;
                  return (
                    <tr key={u.id}>
                      <td>
                        {u.full_name ?? <span className="muted">—</span>}
                        <div className="muted">{u.email}</div>
                      </td>
                      <td>
                        <span className={`pill ${u.role === "ADMIN" ? "accent" : ""}`}>
                          {u.role === "ADMIN" ? "admin" : "user"}
                        </span>
                        {isMe && <span className="muted"> · you</span>}
                      </td>
                      <td>
                        <span className={`pill ${u.is_active ? "ok" : "danger"}`}>
                          {u.is_active ? "active" : "disabled"}
                        </span>
                      </td>
                      <td>
                        <div className="actions">
                          <button
                            className="small"
                            disabled={working}
                            onClick={() =>
                              run(u.id, async () => {
                                await api("admin", `users/${u.id}`, {
                                  method: "PATCH",
                                  body: JSON.stringify({
                                    role: u.role === "ADMIN" ? "USER" : "ADMIN",
                                  }),
                                });
                                toast("ok", `${u.email} is now ${
                                    u.role === "ADMIN" ? "a user" : "an administrator"
                                  }. Their sessions were ended.`,
                                );
                              })
                            }
                          >
                            {u.role === "ADMIN" ? "Make user" : "Make admin"}
                          </button>

                          <button
                            className="small"
                            disabled={working}
                            onClick={() =>
                              run(u.id, async () => {
                                const result = await api<ResetResult>(
                                  "admin",
                                  `users/${u.id}/reset-password`,
                                  { method: "POST", body: JSON.stringify({}) },
                                );
                                if (result.password) {
                                  toast(
                                    "info",
                                    `New password for ${u.email} — shown once, stored only as a hash.`,
                                    result.password,
                                  );
                                } else {
                                  toast("ok", `Password changed for ${u.email}.`);
                                }
                              })
                            }
                          >
                            Reset password
                          </button>

                          <button
                            className="small danger"
                            disabled={working}
                            onClick={() =>
                              run(u.id, async () => {
                                await api(
                                  "admin",
                                  `users/${u.id}/${u.is_active ? "disable" : "enable"}`,
                                  { method: "POST" },
                                );
                                toast("ok", `${u.email} ${u.is_active ? "disabled" : "enabled"}.`,
                                );
                              })
                            }
                          >
                            {u.is_active ? "Disable" : "Enable"}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
