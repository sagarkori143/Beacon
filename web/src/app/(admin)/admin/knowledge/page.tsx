"use client";

import { Fragment, useCallback, useEffect, useMemo, useState } from "react";

import Confirm from "@/components/Confirm";
import { useToast } from "@/components/Toast";
import TopBar from "@/components/TopBar";
import Uploader from "@/components/Uploader";
import { ApiError, api, type Page } from "@/lib/api/client";
import { formatWhen } from "@/lib/ui";

type Location = { id: string; name: string; is_active: boolean };

type Document = {
  id: string;
  title: string;
  description: string | null;
  location_id: string | null;
  document_type: string | null;
  scope: "ORGANIZATION" | "LOCATION";
  created_at: string;
};

type Version = {
  id: string;
  version_number: number;
  status: string;
  filename: string;
  size_bytes: number;
  chunk_count: number;
  ocr_used: boolean;
  ocr_page_count: number;
  page_count: number | null;
  error_message: string | null;
  created_at: string;
};

export default function Knowledge() {
  const toast = useToast();
  const [organization, setOrganization] = useState<string | null>(null);
  const [documents, setDocuments] = useState<Page<Document> | null>(null);
  const [locations, setLocations] = useState<Location[]>([]);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [search, setSearch] = useState("");
  const [scopeFilter, setScopeFilter] = useState("");

  const [openDoc, setOpenDoc] = useState<string | null>(null);
  const [versions, setVersions] = useState<Record<string, Version[]>>({});
  const [confirming, setConfirming] = useState<Document | null>(null);
  const [showUpload, setShowUpload] = useState(false);

  const load = useCallback(async () => {
    try {
      const [me, docs, locs] = await Promise.all([
        api<{ organization: { name: string } }>("admin", "auth/me"),
        api<Page<Document>>("admin", "documents?limit=200"),
        api<Page<Location>>("admin", "locations"),
      ]);
      setOrganization(me.organization.name);
      setDocuments(docs);
      setLocations(locs.items);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not load the knowledge base.");
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  const branchName = useCallback(
    (id: string | null) => (id ? (locations.find((l) => l.id === id)?.name ?? "a branch") : null),
    [locations],
  );

  const shown = useMemo(() => {
    const all = documents?.items ?? [];
    const needle = search.trim().toLowerCase();
    return all.filter((d) => {
      if (scopeFilter === "org" && d.location_id !== null) return false;
      if (scopeFilter && scopeFilter !== "org" && d.location_id !== scopeFilter) return false;
      if (!needle) return true;
      return (
        d.title.toLowerCase().includes(needle) ||
        (d.document_type ?? "").toLowerCase().includes(needle)
      );
    });
  }, [documents, search, scopeFilter]);

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

  async function toggleVersions(documentId: string) {
    if (openDoc === documentId) {
      setOpenDoc(null);
      return;
    }
    setOpenDoc(documentId);
    if (!versions[documentId]) {
      try {
        const list = await api<Version[]>("admin", `documents/${documentId}/versions`);
        setVersions((prior) => ({ ...prior, [documentId]: list }));
      } catch (e) {
        toast("error", e instanceof ApiError ? e.message : "Could not load the versions.");
      }
    }
  }

  return (
    <div className="shell">
      <TopBar console_="admin" subtitle={organization} />

      <div className="spread" style={{ marginBottom: 20 }}>
        <div>
          <h1 style={{ fontSize: 24 }}>Knowledge</h1>
          <p className="lede" style={{ marginTop: 6 }}>
            What {organization ?? "this organization"} answers from. Everything here is
            searchable the moment it finishes processing.
          </p>
        </div>
        <button className="primary" onClick={() => setShowUpload((v) => !v)}>
          {showUpload ? "Close" : "Add documents"}
        </button>
      </div>

      {showUpload && (
        <section className="card rise">
          <div className="card-head">
            <h2>Add documents</h2>
            <p className="hint">
              Handbooks, policies, price lists, FAQs — anything a person might be asked about.
            </p>
          </div>
          <Uploader
            locations={locations.filter((l) => l.is_active)}
            onFinished={() => void load()}
          />
        </section>
      )}

      <section className="card">
        <div className="filters">
          <input
            placeholder="Search documents"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            aria-label="Search documents"
          />
          <select
            value={scopeFilter}
            onChange={(e) => setScopeFilter(e.target.value)}
            aria-label="Filter by scope"
          >
            <option value="">All scopes</option>
            <option value="org">General only</option>
            {locations.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name} only
              </option>
            ))}
          </select>
          <span className="muted" style={{ fontSize: 12.5 }}>
            {shown.length} of {documents?.total ?? 0}
          </span>
        </div>

        {documents === null ? (
          <div className="stack">
            {[0, 1, 2].map((i) => (
              <div key={i} className="skeleton" style={{ height: 42 }} />
            ))}
          </div>
        ) : shown.length === 0 ? (
          <div className="empty">
            <div className="mark">·</div>
            {documents.items.length === 0
              ? "Nothing here yet. Add a handbook or policy to get started."
              : "Nothing matches those filters."}
          </div>
        ) : (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Document</th>
                  <th>Applies to</th>
                  <th>Added</th>
                  <th style={{ width: 1 }} />
                </tr>
              </thead>
              <tbody>
                {shown.map((doc) => (
                  <Fragment key={doc.id}>
                    <tr>
                      <td>
                        <div className="cell-title">{doc.title}</div>
                        {doc.document_type && (
                          <div className="muted" style={{ fontSize: 12 }}>
                            {doc.document_type}
                          </div>
                        )}
                      </td>
                      <td>
                        {doc.scope === "ORGANIZATION" ? (
                          <span className="pill">General</span>
                        ) : (
                          <span className="pill accent">{branchName(doc.location_id)}</span>
                        )}
                      </td>
                      <td className="muted">{formatWhen(doc.created_at)}</td>
                      <td>
                        <div className="actions">
                          <button className="small" onClick={() => void toggleVersions(doc.id)}>
                            {openDoc === doc.id ? "Hide" : "Versions"}
                          </button>
                          <button
                            className="small danger"
                            disabled={busyId === doc.id}
                            onClick={() => setConfirming(doc)}
                          >
                            Archive
                          </button>
                        </div>
                      </td>
                    </tr>

                    {openDoc === doc.id && (
                      <tr>
                        <td colSpan={4} style={{ background: "var(--surface-2)" }}>
                          {!versions[doc.id] ? (
                            <div className="skeleton" style={{ height: 32 }} />
                          ) : (
                            <div className="stack" style={{ gap: 10 }}>
                              {versions[doc.id].map((v) => (
                                <div key={v.id} className="spread">
                                  <div style={{ minWidth: 0 }}>
                                    <span className="status-line">
                                      <span
                                        className={`pill ${
                                          v.status === "ACTIVE"
                                            ? "ok"
                                            : v.status === "FAILED"
                                              ? "danger"
                                              : ""
                                        }`}
                                      >
                                        v{v.version_number} · {v.status.toLowerCase()}
                                      </span>
                                      <span className="muted" style={{ fontSize: 12 }}>
                                        {v.filename}
                                      </span>
                                    </span>
                                    <div className="step-detail">
                                      <span className="chip">
                                        <b>{v.chunk_count}</b> chunks
                                      </span>
                                      {v.page_count != null && (
                                        <span className="chip">
                                          <b>{v.page_count}</b> pages
                                        </span>
                                      )}
                                      {v.ocr_used && (
                                        <span className="chip">
                                          OCR on <b>{v.ocr_page_count}</b> pages
                                        </span>
                                      )}
                                      <span className="chip">
                                        {(v.size_bytes / 1024).toFixed(0)} KB
                                      </span>
                                    </div>
                                    {v.error_message && (
                                      <p className="hint" style={{ color: "var(--danger)" }}>
                                        {v.error_message.slice(0, 180)}
                                      </p>
                                    )}
                                  </div>

                                  <div className="actions">
                                    <a
                                      className="pill"
                                      href={`/api/admin/documents/versions/${v.id}/download`}
                                    >
                                      Download
                                    </a>
                                    {v.status !== "ACTIVE" && v.status !== "FAILED" && (
                                      <button
                                        className="small"
                                        onClick={() =>
                                          act(doc.id, async () => {
                                            await api(
                                              "admin",
                                              `documents/versions/${v.id}/activate`,
                                              { method: "POST" },
                                            );
                                            toast(
                                              "ok",
                                              `Version ${v.version_number} is now the one answering.`,
                                            );
                                            setVersions((p) => {
                                              const n = { ...p };
                                              delete n[doc.id];
                                              return n;
                                            });
                                          })
                                        }
                                      >
                                        Make live
                                      </button>
                                    )}
                                  </div>
                                </div>
                              ))}
                            </div>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <Confirm
        open={confirming !== null}
        title={`Archive “${confirming?.title ?? ""}”?`}
        body="It stops answering questions immediately and leaves this list. Nothing is deleted — you can restore it, and every version is kept."
        confirmLabel="Archive"
        danger
        onCancel={() => setConfirming(null)}
        onConfirm={() => {
          const doc = confirming;
          setConfirming(null);
          if (!doc) return;
          void act(doc.id, async () => {
            const result = await api<{ chunks_withdrawn: number }>(
              "admin",
              `documents/${doc.id}/archive`,
              { method: "POST" },
            );
            toast(
              "ok",
              `“${doc.title}” archived — ${result.chunks_withdrawn} passages stopped answering.`,
            );
          });
        }}
      />
    </div>
  );
}
