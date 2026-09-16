"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useState } from "react";

import { useToast } from "@/components/Toast";
import TopBar from "@/components/TopBar";
import { ApiError, api, type Organization, type TenantUser } from "@/lib/api/client";
import { initials, tint } from "@/lib/ui";

type Location = { id: string; name: string; slug: string; timezone: string; is_active: boolean };

export default function OrganizationDetail({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const toast = useToast();
  const { id } = use(params);

  const [organization, setOrganization] = useState<Organization | null>(null);
  const [locations, setLocations] = useState<Location[]>([]);
  const [users, setUsers] = useState<TenantUser[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [branch, setBranch] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [role, setRole] = useState<"USER" | "ADMIN">("ADMIN");
  const [pin, setPin] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [org, locs, people] = await Promise.all([
        api<Organization>("owner", `organizations/${id}`),
        api<Location[]>("owner", `organizations/${id}/locations`),
        api<TenantUser[]>("owner", `organizations/${id}/users`),
      ]);
      setOrganization(org);
      setLocations(locs);
      setUsers(people);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not load this organization.");
    }
  }, [id, toast]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(key: string, action: () => Promise<void>) {
    setBusyId(key);
    try {
      await action();
      await load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "That did not work.");
    } finally {
      setBusyId(null);
    }
  }

  async function addBranch(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await api("owner", `organizations/${id}/locations`, {
        method: "POST",
        body: JSON.stringify({ name: branch.trim() }),
      });
      toast("ok", `${branch.trim()} added.`);
      setBranch("");
      await load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not add the branch.");
    } finally {
      setBusy(false);
    }
  }

  async function addUser(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      await api("owner", `organizations/${id}/users`, {
        method: "POST",
        body: JSON.stringify({
          email: email.trim(),
          password,
          role,
          location_id: pin || null,
        }),
      });
      toast("ok", `${email.trim()} added.`);
      setEmail("");
      setPassword("");
      setPin("");
      await load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not add the user.");
    } finally {
      setBusy(false);
    }
  }

  const colour = organization ? tint(organization.slug) : null;

  return (
    <div className="shell">
      <TopBar console_="owner" />

      <div className="spread" style={{ marginBottom: 22 }}>
        <div className="brand" style={{ gap: 12 }}>
          {colour && organization && (
            <span
              className="avatar"
              style={{ background: colour.bg, color: colour.fg }}
            >
              {initials(organization.name)}
            </span>
          )}
          <div>
            <h1 style={{ fontSize: 22 }}>{organization?.name ?? "Loading"}</h1>
            <p className="hint" style={{ margin: 0 }}>
              {organization && (
                <>
                  <span className="mono">{organization.slug}</span>
                  {" · "}
                  {organization.is_public ? (
                    <Link href={`/c/${organization.slug}`}>public page</Link>
                  ) : (
                    <span className="muted">hidden from the public site</span>
                  )}
                </>
              )}
            </p>
          </div>
        </div>
        <Link href="/owner" className="pill">
          ← All organizations
        </Link>
      </div>


      <section className="card">
        <div className="spread">
          <div>
            <h2>Public site</h2>
            <p className="hint" style={{ maxWidth: "52ch" }}>
              {organization?.is_public
                ? "Listed in the directory. Anyone can open it and ask questions, and every answer comes from this organization's own documents."
                : "Hidden. It is not in the directory, its address returns nothing, and no visitor can reach its knowledge."}
            </p>
          </div>
          <button
            className={organization?.is_public ? "danger" : "primary"}
            disabled={!organization || busyId === "visibility"}
            onClick={() =>
              act("visibility", async () => {
                const next = !organization!.is_public;
                await api("owner", `organizations/${id}`, {
                  method: "PATCH",
                  body: JSON.stringify({ is_public: next }),
                });
                toast(
                  "ok",
                  next
                    ? `${organization!.name} is now on the public site.`
                    : `${organization!.name} is hidden from the public site.`,
                );
              })
            }
          >
            {busyId === "visibility" && <span className="spinner" />}
            {organization?.is_public ? "Hide from public site" : "Publish"}
          </button>
        </div>
      </section>

      <section className="card">
        <div className="card-head">
          <h2>Branches</h2>
          <p className="hint">Locations of this business. Each can hold its own knowledge.</p>
        </div>

        <form onSubmit={addBranch} className="row" style={{ alignItems: "flex-end" }}>
          <div className="field" style={{ marginBottom: 0 }}>
            <label htmlFor="branch">Name</label>
            <input
              id="branch"
              required
              placeholder="Ginza"
              value={branch}
              onChange={(e) => setBranch(e.target.value)}
            />
          </div>
          <button style={{ flex: "0 0 auto" }} disabled={busy || !branch.trim()}>
            Add branch
          </button>
        </form>

        {locations.length > 0 && (
          <div className="actions" style={{ marginTop: 16 }}>
            {locations.map((l) => (
              <span key={l.id} className={`pill ${l.is_active ? "" : "danger"}`}>
                {l.name}
                <span className="muted"> · {l.timezone}</span>
              </span>
            ))}
          </div>
        )}
      </section>

      <section className="card">
        <div className="card-head">
          <h2>Add a person</h2>
          <p className="hint">
            An administrator manages this organization. A user can only read its knowledge.
          </p>
        </div>

        <form onSubmit={addUser}>
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
              <label htmlFor="u-pass">Password (at least 12 characters)</label>
              <input
                id="u-pass"
                required
                minLength={12}
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </div>
          </div>
          <div className="row">
            <div className="field">
              <label htmlFor="u-role">Role</label>
              <select
                id="u-role"
                value={role}
                onChange={(e) => setRole(e.target.value as "USER" | "ADMIN")}
              >
                <option value="ADMIN">Administrator</option>
                <option value="USER">User</option>
              </select>
            </div>
            <div className="field">
              <label htmlFor="u-pin">Branch</label>
              <select id="u-pin" value={pin} onChange={(e) => setPin(e.target.value)}>
                <option value="">Whole organization</option>
                {locations.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <button className="primary" disabled={busy}>
            {busy && <span className="spinner" />}
            Add person
          </button>
        </form>
      </section>

      <section className="card">
        <div className="card-head">
          <h2>People</h2>
          <p className="hint">{users.length} in this organization</p>
        </div>

        {users.length === 0 ? (
          <div className="empty">Nobody here yet.</div>
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
                {users.map((u) => (
                  <tr key={u.id}>
                    <td>
                      <div className="cell-title">{u.full_name ?? u.email}</div>
                      {u.full_name && <div className="muted">{u.email}</div>}
                    </td>
                    <td>
                      <span className={`pill ${u.role === "ADMIN" ? "accent" : ""}`}>
                        {u.role === "ADMIN" ? "admin" : "user"}
                      </span>
                    </td>
                    <td>
                      <span className={`pill ${u.is_active ? "ok" : "danger"}`}>
                        {u.is_active ? "active" : "disabled"}
                      </span>
                    </td>
                    <td>
                      <button
                        className={`small ${u.is_active ? "danger" : ""}`}
                        disabled={busyId === u.id}
                        onClick={() =>
                          act(u.id, async () => {
                            await api(
                              "owner",
                              `organizations/${id}/users/${u.id}/${
                                u.is_active ? "disable" : "enable"
                              }`,
                              { method: "POST" },
                            );
                            toast("ok", `${u.email} ${u.is_active ? "disabled" : "enabled"}.`);
                          })
                        }
                      >
                        {u.is_active ? "Disable" : "Enable"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>
    </div>
  );
}
