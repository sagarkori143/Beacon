"use client";

import { useCallback, useEffect, useState } from "react";

import TopBar from "@/components/TopBar";
import { ApiError, api, type Organization } from "@/lib/api/client";

type Provisioned = {
  organization: Organization;
  admin_email: string | null;
  admin_password: string | null;
};

export default function OwnerConsole() {
  const [organizations, setOrganizations] = useState<Organization[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<Provisioned | null>(null);

  const [name, setName] = useState("");
  const [adminEmail, setAdminEmail] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setOrganizations(await api<Organization[]>("owner", "organizations"));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load organizations.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function createOrganization(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const result = await api<Provisioned>("owner", "organizations", {
        method: "POST",
        body: JSON.stringify({
          name,
          admin_email: adminEmail.trim() ? adminEmail.trim() : null,
        }),
      });
      setCreated(result);
      setName("");
      setAdminEmail("");
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not create the organization.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="shell">
      <TopBar console_="owner" />

      <section className="card">
        <h2>New organization</h2>
        <p className="hint">
          Its first administrator is created in the same step, because an organization with
          nobody to manage it cannot do anything.
        </p>

        {error && <div className="notice error">{error}</div>}

        {created && (
          <div className="notice secret">
            <strong>{created.organization.name}</strong> created.
            {created.admin_password ? (
              <>
                {" "}
                Give <strong>{created.admin_email}</strong> this password — it is stored only
                as a hash and will never be shown again.
                <br />
                <code>{created.admin_password}</code>
              </>
            ) : (
              " No administrator was created for it yet."
            )}
          </div>
        )}

        <form onSubmit={createOrganization}>
          <div className="row">
            <div className="field">
              <label htmlFor="org-name">Name</label>
              <input
                id="org-name"
                required
                placeholder="Northwind Dental"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="admin-email">First administrator&rsquo;s email</label>
              <input
                id="admin-email"
                type="email"
                placeholder="admin@northwind.example"
                value={adminEmail}
                onChange={(e) => setAdminEmail(e.target.value)}
              />
            </div>
          </div>
          <button className="primary" disabled={busy}>
            {busy ? "Creating…" : "Create organization"}
          </button>
        </form>
      </section>

      <section className="card">
        <div className="spread" style={{ marginBottom: 14 }}>
          <div>
            <h2>Organizations</h2>
            <p className="hint" style={{ margin: 0 }}>
              Every tenant on this deployment. Names only — an operator cannot read anyone&rsquo;s
              knowledge.
            </p>
          </div>
          <button className="small" onClick={() => void load()}>
            Refresh
          </button>
        </div>

        {organizations === null ? (
          <p className="empty">Loading…</p>
        ) : organizations.length === 0 ? (
          <p className="empty">No organizations yet.</p>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Name</th>
                  <th>Slug</th>
                  <th>Status</th>
                  <th>Created</th>
                </tr>
              </thead>
              <tbody>
                {organizations.map((o) => (
                  <tr key={o.id}>
                    <td>{o.name}</td>
                    <td className="muted">{o.slug}</td>
                    <td>
                      <span className={`pill ${o.is_active ? "on" : "off"}`}>
                        {o.is_active ? "active" : "inactive"}
                      </span>
                    </td>
                    <td className="muted">
                      {new Date(o.created_at).toLocaleDateString()}
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
