"use client";

import { Fragment, useCallback, useEffect, useRef, useState } from "react";

import Timeline from "@/components/Timeline";
import TopBar from "@/components/TopBar";
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
  created_at: string;
};

type Upload = { document_id: string; version_id: string; job_id: string };

export default function Knowledge() {
  const [organization, setOrganization] = useState<string | null>(null);
  const [documents, setDocuments] = useState<Page<Document> | null>(null);
  const [locations, setLocations] = useState<Location[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [file, setFile] = useState<File | null>(null);
  const [title, setTitle] = useState("");
  const [scope, setScope] = useState("");
  const [uploading, setUploading] = useState(false);
  const [job, setJob] = useState<Upload | null>(null);
  const [over, setOver] = useState(false);
  const picker = useRef<HTMLInputElement>(null);

  const [openDoc, setOpenDoc] = useState<string | null>(null);
  const [versions, setVersions] = useState<Record<string, Version[]>>({});

  const load = useCallback(async () => {
    try {
      const [me, docs, locs] = await Promise.all([
        api<{ organization: { name: string } }>("admin", "auth/me"),
        api<Page<Document>>("admin", "documents?limit=100"),
        api<Page<Location>>("admin", "locations?include_inactive=false"),
      ]);
      setOrganization(me.organization.name);
      setDocuments(docs);
      setLocations(locs.items);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load the knowledge base.");
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function upload(event: React.FormEvent) {
    event.preventDefault();
    if (!file) return;

    setUploading(true);
    setError(null);
    setNote(null);
    setJob(null);

    const form = new FormData();
    form.append("file", file);
    if (title.trim()) form.append("title", title.trim());
    if (scope) form.append("location_id", scope);

    try {
      const response = await fetch("/api/admin/documents", { method: "POST", body: form });
      if (!response.ok) {
        const problem = await response.json().catch(() => ({}));
        throw new Error(problem.detail ?? "The upload was refused.");
      }
      setJob((await response.json()) as Upload);
      setFile(null);
      setTitle("");
      if (picker.current) picker.current.value = "";
    } catch (e) {
      setError(e instanceof Error ? e.message : "The upload failed.");
    } finally {
      setUploading(false);
    }
  }

  async function act(id: string, action: () => Promise<void>) {
    setBusyId(id);
    setError(null);
    setNote(null);
    try {
      await action();
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "That did not work.");
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
      const list = await api<Version[]>("admin", `documents/${documentId}/versions`);
      setVersions((prior) => ({ ...prior, [documentId]: list }));
    }
  }

  const branchName = (id: string | null) =>
    id ? (locations.find((l) => l.id === id)?.name ?? "a branch") : null;

  return (
    <div className="shell">
      <TopBar console_="admin" subtitle={organization} />

      {error && <div className="notice error">{error}</div>}
      {note && <div className="notice ok">{note}</div>}

      <section className="card">
        <div className="card-head">
          <h2>Add knowledge</h2>
          <p className="hint">
            <strong>General</strong> knowledge answers for every branch.{" "}
            <strong>Branch</strong> knowledge overrides it on whatever subjects it covers, and
            leaves the rest alone.
          </p>
        </div>

        <form onSubmit={upload}>
          <div
            className={`dropzone ${over ? "over" : ""}`}
            onClick={() => picker.current?.click()}
            onDragOver={(e) => {
              e.preventDefault();
              setOver(true);
            }}
            onDragLeave={() => setOver(false)}
            onDrop={(e) => {
              e.preventDefault();
              setOver(false);
              const dropped = e.dataTransfer.files?.[0];
              if (dropped) setFile(dropped);
            }}
          >
            {file ? (
              <>
                <div className="file">{file.name}</div>
                <p className="hint">{(file.size / 1024).toFixed(0)} KB · click to replace</p>
              </>
            ) : (
              <>
                <div className="file">Drop a file here, or click to choose</div>
                <p className="hint">PDF, Word, Markdown or plain text — up to 50 MB</p>
              </>
            )}
            <input
              ref={picker}
              type="file"
              hidden
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </div>

          <div className="row" style={{ marginTop: 14 }}>
            <div className="field">
              <label htmlFor="title">Title (optional)</label>
              <input
                id="title"
                placeholder="Taken from the filename"
                value={title}
                onChange={(e) => setTitle(e.target.value)}
              />
            </div>
            <div className="field">
              <label htmlFor="scope">Applies to</label>
              <select id="scope" value={scope} onChange={(e) => setScope(e.target.value)}>
                <option value="">General — every branch</option>
                {locations.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name} only
                  </option>
                ))}
              </select>
            </div>
          </div>

          <button className="primary" disabled={!file || uploading}>
            {uploading && <span className="spinner" />}
            {uploading ? "Uploading" : "Upload and process"}
          </button>
        </form>

        {job && (
          <div style={{ marginTop: 22, paddingTop: 20, borderTop: "1px solid var(--line)" }}>
            <Timeline
              jobId={job.job_id}
              onSettled={(status) => {
                setNote(
                  status === "COMPLETED"
                    ? "Processed and live. It will answer questions now."
                    : null,
                );
                void load();
              }}
            />
          </div>
        )}
      </section>

      <section className="card">
        <div className="spread card-head">
          <div>
            <h2>Documents</h2>
            <p className="hint" style={{ margin: 0 }}>
              {documents?.total ?? 0} in this organization
            </p>
          </div>
          <button className="small" onClick={() => void load()}>
            Refresh
          </button>
        </div>

        {documents === null ? (
          <div className="stack">
            {[0, 1, 2].map((i) => (
              <div key={i} className="skeleton" style={{ height: 40 }} />
            ))}
          </div>
        ) : documents.items.length === 0 ? (
          <div className="empty">
            <div className="mark">·</div>
            Nothing here yet. Upload a handbook or policy to get started.
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
                {documents.items.map((doc) => (
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
                          <button
                            className="small"
                            onClick={() => void toggleVersions(doc.id)}
                          >
                            {openDoc === doc.id ? "Hide versions" : "Versions"}
                          </button>
                          <button
                            className="small danger"
                            disabled={busyId === doc.id}
                            onClick={() =>
                              act(doc.id, async () => {
                                await api("admin", `documents/${doc.id}/archive`, {
                                  method: "POST",
                                });
                                setNote(
                                  `“${doc.title}” archived. It has stopped answering questions.`,
                                );
                              })
                            }
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
                            <div className="skeleton" style={{ height: 30 }} />
                          ) : (
                            <div className="stack">
                              {versions[doc.id].map((v) => (
                                <div key={v.id} className="spread">
                                  <span className="status-line">
                                    <span
                                      className={`pill ${v.status === "ACTIVE" ? "ok" : ""}`}
                                    >
                                      v{v.version_number} · {v.status.toLowerCase()}
                                    </span>
                                    <span className="muted" style={{ fontSize: 12 }}>
                                      {v.filename} · {v.chunk_count} chunks
                                      {v.ocr_used && " · OCR"}
                                    </span>
                                  </span>
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
                                            setNote(
                                              `Version ${v.version_number} is live.`,
                                            );
                                            setVersions((p) => {
                                              const next = { ...p };
                                              delete next[doc.id];
                                              return next;
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
    </div>
  );
}
