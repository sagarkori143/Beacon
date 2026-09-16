"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import TopBar from "@/components/TopBar";
import { ApiError, api, type Page } from "@/lib/api/client";
import { STAGE_LABEL, formatWhen } from "@/lib/ui";

type Me = {
  user: { email: string; full_name: string | null };
  organization: { id: string; name: string; slug: string };
};

type Job = {
  id: string;
  document_id: string;
  status: string;
  current_stage: string;
  progress: number;
  error_message: string | null;
  created_at: string;
};

export default function Overview() {
  const [me, setMe] = useState<Me | null>(null);
  const [counts, setCounts] = useState({ documents: 0, branches: 0, people: 0 });
  const [jobs, setJobs] = useState<Job[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [profile, docs, branches, people, recent] = await Promise.all([
        api<Me>("admin", "auth/me"),
        api<Page<unknown>>("admin", "documents?limit=1"),
        api<Page<unknown>>("admin", "locations"),
        api<Page<unknown>>("admin", "users?limit=1"),
        api<Page<Job>>("admin", "ingestion/jobs?limit=6"),
      ]);
      setMe(profile);
      setCounts({
        documents: docs.total ?? 0,
        branches: branches.total ?? 0,
        people: people.total ?? 0,
      });
      setJobs(recent.items);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not load this organization.");
    }
  }, []);

  useEffect(() => {
    void load();
    // Jobs move on their own; a light refresh keeps the overview honest without
    // the machinery a live stream would need on a page that only summarises.
    const timer = setInterval(() => void load(), 8000);
    return () => clearInterval(timer);
  }, [load]);

  async function retry(job: Job) {
    setBusyId(job.id);
    setError(null);
    try {
      await api("admin", `ingestion/jobs/${job.id}/retry`, { method: "POST" });
      setNote("Queued again. It will pick up where it failed.");
      await load();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Could not retry it.");
    } finally {
      setBusyId(null);
    }
  }

  const tiles = [
    { label: "Documents", value: counts.documents, href: "/admin/knowledge" },
    { label: "Branches", value: counts.branches, href: "/admin/branches" },
    { label: "People", value: counts.people, href: "/admin/people" },
  ];

  return (
    <div className="shell">
      <TopBar console_="admin" subtitle={me?.organization.name} />

      {error && <div className="notice error">{error}</div>}
      {note && <div className="notice ok">{note}</div>}

      <section style={{ marginBottom: 20 }}>
        <span className="eyebrow">Signed in as {me?.user.email ?? "…"}</span>
        <h1 style={{ marginTop: 8 }}>{me?.organization.name ?? "Loading"}</h1>
        <p className="lede">
          Your knowledge answers questions on the public site at{" "}
          {me ? (
            <Link href={`/c/${me.organization.slug}`}>/c/{me.organization.slug}</Link>
          ) : (
            "…"
          )}
          .
        </p>
      </section>

      <div className="grid" style={{ marginBottom: 18 }}>
        {tiles.map((tile) => (
          <Link key={tile.label} href={tile.href} className="org-card">
            <span className="eyebrow">{tile.label}</span>
            <span style={{ fontSize: 30, fontWeight: 660, letterSpacing: "-0.03em" }}>
              {tile.value}
            </span>
            <span className="go">Manage →</span>
          </Link>
        ))}
      </div>

      <section className="card">
        <div className="card-head">
          <h2>Recent processing</h2>
          <p className="hint">Every upload runs through the pipeline before it can answer.</p>
        </div>

        {jobs.length === 0 ? (
          <div className="empty">
            <div className="mark">·</div>
            Nothing processed yet.{" "}
            <Link href="/admin/knowledge">Upload a document</Link>.
          </div>
        ) : (
          <div className="stack" style={{ gap: 12 }}>
            {jobs.map((job) => (
              <div key={job.id} className="spread">
                <span className="status-line">
                  <span
                    className={`pill ${
                      job.status === "COMPLETED"
                        ? "ok"
                        : job.status === "FAILED"
                          ? "danger"
                          : "accent"
                    }`}
                  >
                    <span
                      className={`dot ${job.status === "RUNNING" ? "live" : ""}`}
                    />
                    {STAGE_LABEL[job.current_stage] ?? job.current_stage}
                  </span>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {formatWhen(job.created_at)}
                  </span>
                  {job.error_message && (
                    <span className="muted" style={{ fontSize: 12 }}>
                      {job.error_message.slice(0, 80)}
                    </span>
                  )}
                </span>

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
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
