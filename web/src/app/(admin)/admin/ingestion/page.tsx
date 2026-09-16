"use client";

import { useCallback, useEffect, useState } from "react";

import Timeline from "@/components/Timeline";
import { useToast } from "@/components/Toast";
import TopBar from "@/components/TopBar";
import { ApiError, api, type Page } from "@/lib/api/client";
import { STAGE_LABEL, formatDuration, formatWhen } from "@/lib/ui";

type Job = {
  id: string;
  document_id: string;
  document_version: number;
  status: "QUEUED" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED";
  current_stage: string;
  progress: number;
  attempts: number;
  error_message: string | null;
  error_stage: string | null;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
};

type Document = { id: string; title: string };

const FILTERS = [
  { key: "", label: "All" },
  { key: "RUNNING", label: "Running" },
  { key: "FAILED", label: "Failed" },
  { key: "COMPLETED", label: "Done" },
] as const;

function tone(status: Job["status"]) {
  if (status === "COMPLETED") return "ok";
  if (status === "FAILED") return "danger";
  if (status === "RUNNING") return "accent";
  return "";
}

export default function Ingestion() {
  const toast = useToast();
  const [organization, setOrganization] = useState<string | null>(null);
  const [jobs, setJobs] = useState<Page<Job> | null>(null);
  const [titles, setTitles] = useState<Record<string, string>>({});
  const [status, setStatus] = useState<string>("");
  const [open, setOpen] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const query = status ? `&status=${status}` : "";
      const [me, page, docs] = await Promise.all([
        api<{ organization: { name: string } }>("admin", "auth/me"),
        api<Page<Job>>("admin", `ingestion/jobs?limit=50${query}`),
        api<Page<Document>>("admin", "documents?limit=200"),
      ]);
      setOrganization(me.organization.name);
      setJobs(page);
      setTitles(Object.fromEntries(docs.items.map((d) => [d.id, d.title])));
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not load processing history.");
    }
  }, [status, toast]);

  useEffect(() => {
    void load();
  }, [load]);

  // Anything still moving is worth re-reading; a settled list is not.
  const live = jobs?.items.some((j) => j.status === "RUNNING" || j.status === "QUEUED");
  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => void load(), 4000);
    return () => clearInterval(timer);
  }, [live, load]);

  async function retry(job: Job) {
    setBusyId(job.id);
    try {
      await api("admin", `ingestion/jobs/${job.id}/retry`, { method: "POST" });
      toast("ok", "Queued again — it picks up where it failed, not from the start.");
      setOpen(job.id);
      await load();
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not retry it.");
    } finally {
      setBusyId(null);
    }
  }

  const items = jobs?.items ?? [];

  return (
    <div className="shell">
      <TopBar console_="admin" subtitle={organization} />

      <section style={{ marginBottom: 20 }}>
        <h1 style={{ fontSize: 24 }}>Processing</h1>
        <p className="lede">
          Every document goes through the same stages before it can answer anything. This is
          where to look when one of them does not.
        </p>
      </section>

      <div className="filters">
        <div className="seg">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              aria-pressed={status === f.key}
              onClick={() => setStatus(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>
        <button className="small" onClick={() => void load()}>
          Refresh
        </button>
        {live && (
          <span className="pill accent">
            <span className="dot live" />
            Updating live
          </span>
        )}
      </div>

      <section className="card">
        {jobs === null ? (
          <div className="stack">
            {[0, 1, 2].map((i) => (
              <div key={i} className="skeleton" style={{ height: 44 }} />
            ))}
          </div>
        ) : items.length === 0 ? (
          <div className="empty">
            <div className="mark">·</div>
            {status ? `Nothing ${status.toLowerCase()}.` : "Nothing has been processed yet."}
          </div>
        ) : (
          <div className="stack" style={{ gap: 0 }}>
            {items.map((job) => {
              const expanded = open === job.id;
              const took =
                job.started_at && job.completed_at
                  ? formatDuration(
                      new Date(job.completed_at).getTime() -
                        new Date(job.started_at).getTime(),
                    )
                  : null;

              return (
                <div
                  key={job.id}
                  style={{
                    padding: "14px 0",
                    borderBottom: "1px solid var(--line)",
                  }}
                >
                  <div className="spread">
                    <div style={{ minWidth: 0 }}>
                      <div className="cell-title">
                        {titles[job.document_id] ?? "A document"}
                        <span className="muted" style={{ fontWeight: 400 }}>
                          {" "}
                          · v{job.document_version}
                        </span>
                      </div>
                      <div className="status-line" style={{ marginTop: 5 }}>
                        <span className={`pill ${tone(job.status)}`}>
                          <span
                            className={`dot ${job.status === "RUNNING" ? "live" : ""}`}
                          />
                          {job.status === "RUNNING"
                            ? (STAGE_LABEL[job.current_stage] ?? job.current_stage)
                            : job.status.toLowerCase()}
                        </span>
                        <span className="muted" style={{ fontSize: 12 }}>
                          {formatWhen(job.created_at)}
                          {took && ` · took ${took}`}
                          {job.attempts > 1 && ` · attempt ${job.attempts}`}
                        </span>
                      </div>
                    </div>

                    <div className="actions">
                      <button
                        className="small"
                        onClick={() => setOpen(expanded ? null : job.id)}
                      >
                        {expanded ? "Hide steps" : "See steps"}
                      </button>
                      {job.status === "FAILED" && (
                        <button
                          className="small"
                          disabled={busyId === job.id}
                          onClick={() => void retry(job)}
                        >
                          {busyId === job.id && <span className="spinner" />}
                          Retry
                        </button>
                      )}
                    </div>
                  </div>

                  {job.error_message && !expanded && (
                    <p className="hint" style={{ color: "var(--danger)" }}>
                      {STAGE_LABEL[job.error_stage ?? ""] ?? job.error_stage}:{" "}
                      {job.error_message.slice(0, 160)}
                    </p>
                  )}

                  {expanded && (
                    <div style={{ marginTop: 14 }}>
                      <Timeline jobId={job.id} onSettled={() => void load()} />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </section>
    </div>
  );
}
