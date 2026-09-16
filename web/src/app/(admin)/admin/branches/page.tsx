"use client";

import { useCallback, useEffect, useState } from "react";

import { useToast } from "@/components/Toast";
import TopBar from "@/components/TopBar";
import { ApiError, api, type Page } from "@/lib/api/client";

type Location = {
  id: string;
  name: string;
  slug: string;
  timezone: string;
  is_active: boolean;
};

const ZONES = [
  "UTC",
  "Asia/Kolkata",
  "Asia/Tokyo",
  "Asia/Dubai",
  "Europe/London",
  "Europe/Berlin",
  "America/New_York",
  "America/Los_Angeles",
];

export default function Branches() {
  const toast = useToast();
  const [organization, setOrganization] = useState<string | null>(null);
  const [branches, setBranches] = useState<Location[] | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draftName, setDraftName] = useState("");

  const [name, setName] = useState("");
  const [timezone, setTimezone] = useState("UTC");
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    try {
      const [me, list] = await Promise.all([
        api<{ organization: { name: string } }>("admin", "auth/me"),
        api<Page<Location>>("admin", "locations?include_inactive=true"),
      ]);
      setOrganization(me.organization.name);
      setBranches(list.items);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not load the branches.");
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  async function act(id: string, action: () => Promise<void>) {
    setBusyId(id);
    try {
      await action();
      await load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "That did not work.");
    } finally {
      setBusyId(null);
    }
  }

  async function create(event: React.FormEvent) {
    event.preventDefault();
    setCreating(true);
    try {
      const slug = name
        .trim()
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, "-")
        .replace(/^-|-$/g, "")
        .slice(0, 100);
      await api("admin", "locations", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), slug, timezone }),
      });
      toast("ok", `${name.trim()} added.`);
      setName("");
      await load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not add the branch.");
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="shell">
      <TopBar console_="admin" subtitle={organization} />


      <section className="card">
        <div className="card-head">
          <h2>Add a branch</h2>
          <p className="hint">
            A branch is a location of this business. Knowledge uploaded for one answers only
            for it, and takes precedence over the general version on the same subjects.
          </p>
        </div>

        <form onSubmit={create}>
          <div className="row">
            <div className="field">
              <label htmlFor="b-name">Name</label>
              <input
                id="b-name"
                required
                placeholder="Ginza"
                value={name}
                onChange={(e) => setName(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="b-tz">Timezone</label>
              <select id="b-tz" value={timezone} onChange={(e) => setTimezone(e.target.value)}>
                {ZONES.map((z) => (
                  <option key={z} value={z}>
                    {z}
                  </option>
                ))}
              </select>
            </div>
          </div>
          <button className="primary" disabled={creating || !name.trim()}>
            {creating && <span className="spinner" />}
            {creating ? "Adding" : "Add branch"}
          </button>
        </form>
      </section>

      <section className="card">
        <div className="spread card-head">
          <div>
            <h2>Branches</h2>
            <p className="hint" style={{ margin: 0 }}>
              {branches?.filter((b) => b.is_active).length ?? 0} active
            </p>
          </div>
          <button className="small" onClick={() => void load()}>
            Refresh
          </button>
        </div>

        {branches === null ? (
          <div className="stack">
            {[0, 1].map((i) => (
              <div key={i} className="skeleton" style={{ height: 38 }} />
            ))}
          </div>
        ) : branches.length === 0 ? (
          <div className="empty">
            <div className="mark">·</div>
            No branches yet. Without one, all knowledge applies everywhere.
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Branch</th>
                  <th>Timezone</th>
                  <th>Status</th>
                  <th style={{ width: 1 }} />
                </tr>
              </thead>
              <tbody>
                {branches.map((b) => (
                  <tr key={b.id}>
                    <td>
                      {editing === b.id ? (
                        <input
                          autoFocus
                          value={draftName}
                          onChange={(e) => setDraftName(e.target.value)}
                          onKeyDown={(e) => {
                            if (e.key === "Escape") setEditing(null);
                            if (e.key === "Enter") {
                              void act(b.id, async () => {
                                await api("admin", `locations/${b.id}`, {
                                  method: "PATCH",
                                  body: JSON.stringify({ name: draftName.trim() }),
                                });
                                setEditing(null);
                                toast("ok", "Branch renamed.");
                              });
                            }
                          }}
                        />
                      ) : (
                        <>
                          <div className="cell-title">{b.name}</div>
                          <div className="muted mono" style={{ fontSize: 12 }}>
                            {b.slug}
                          </div>
                        </>
                      )}
                    </td>
                    <td className="muted">{b.timezone}</td>
                    <td>
                      <span className={`pill ${b.is_active ? "ok" : ""}`}>
                        {b.is_active ? "active" : "inactive"}
                      </span>
                    </td>
                    <td>
                      <div className="actions">
                        {editing === b.id ? (
                          <button className="small ghost" onClick={() => setEditing(null)}>
                            Cancel
                          </button>
                        ) : (
                          <button
                            className="small"
                            onClick={() => {
                              setEditing(b.id);
                              setDraftName(b.name);
                            }}
                          >
                            Rename
                          </button>
                        )}
                        <button
                          className={`small ${b.is_active ? "danger" : ""}`}
                          disabled={busyId === b.id}
                          onClick={() =>
                            act(b.id, async () => {
                              await api("admin", `locations/${b.id}`, {
                                method: "PATCH",
                                body: JSON.stringify({ is_active: !b.is_active }),
                              });
                              toast("ok", `${b.name} ${b.is_active ? "deactivated" : "reactivated"}.`,
                              );
                            })
                          }
                        >
                          {b.is_active ? "Deactivate" : "Reactivate"}
                        </button>
                      </div>
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
