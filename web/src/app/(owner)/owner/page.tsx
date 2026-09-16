"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { useToast } from "@/components/Toast";
import TopBar from "@/components/TopBar";
import { ApiError, api, type Organization } from "@/lib/api/client";
import { initials, tint } from "@/lib/ui";

type Provisioned = {
  organization: Organization;
  admin_email: string | null;
  admin_password: string | null;
};

export default function OwnerConsole() {
  const toast = useToast();
  const [organizations, setOrganizations] = useState<Organization[] | null>(null);

  const [name, setName] = useState("");
  const [adminEmail, setAdminEmail] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setOrganizations(await api<Organization[]>("owner", "organizations"));
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not load organizations.");
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  async function createOrganization(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    try {
      const result = await api<Provisioned>("owner", "organizations", {
        method: "POST",
        body: JSON.stringify({
          name,
          admin_email: adminEmail.trim() ? adminEmail.trim() : null,
        }),
      });
      if (result.admin_password) {
        toast(
          "info",
          `${result.organization.name} created. Give ${result.admin_email} this password — it is stored only as a hash and will never be shown again.`,
          result.admin_password,
        );
      } else {
        toast("ok", `${result.organization.name} created.`);
      }
      setName("");
      setAdminEmail("");
      await load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not create the organization.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="shell">
      <TopBar console_="owner" />

      <section className="card">
        <div className="card-head">
          <h2>New organization</h2>
          <p className="hint">
          Its first administrator is created in the same step, because an organization with
            nobody to manage it cannot do anything.
          </p>
        </div>


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
            {busy && <span className="spinner" />}
            {busy ? "Creating" : "Create organization"}
          </button>
        </form>
      </section>

      <section className="card">
        <div className="spread card-head">
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
          <div className="grid">
            {[0, 1, 2].map((i) => (
              <div key={i} className="skeleton" style={{ height: 118, borderRadius: 18 }} />
            ))}
          </div>
        ) : organizations.length === 0 ? (
          <div className="empty">
            <div className="mark">·</div>
            No organizations yet. Create the first one above.
          </div>
        ) : (
          <div className="grid">
            {organizations.map((o, index) => {
              const colour = tint(o.slug);
              return (
                <Link
                  key={o.id}
                  href={`/owner/organizations/${o.id}`}
                  className="org-card"
                  style={{ animationDelay: `${Math.min(index, 8) * 35}ms` }}
                >
                  <div className="spread" style={{ gap: 10 }}>
                    <span
                      className="avatar"
                      style={{ background: colour.bg, color: colour.fg }}
                    >
                      {initials(o.name)}
                    </span>
                    <span className="actions">
                      {!o.is_active && <span className="pill danger">suspended</span>}
                      {o.is_active && !o.is_public && (
                        <span className="pill">hidden</span>
                      )}
                      {o.is_active && o.is_public && (
                        <span className="pill ok">
                          <span className="dot" />
                          live
                        </span>
                      )}
                    </span>
                  </div>
                  <span className="name">{o.name}</span>
                  <span className="muted mono" style={{ fontSize: 12 }}>
                    {o.slug}
                  </span>
                  <span className="go">Manage →</span>
                </Link>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
