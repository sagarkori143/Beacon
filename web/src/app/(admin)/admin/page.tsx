"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { useToast } from "@/components/Toast";
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
  error_message: string | null;
  created_at: string;
};

export default function Overview() {
  const toast = useToast();
  const [me, setMe] = useState<Me | null>(null);
  const [counts, setCounts] = useState({ documents: 0, branches: 0, people: 0 });
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async () => {
    try {
      const [profile, docs, branches, people, recent] = await Promise.all([
        api<Me>("admin", "auth/me"),
        api<Page<unknown>>("admin", "documents?limit=1"),
        api<Page<unknown>>("admin", "locations"),
        api<Page<unknown>>("admin", "users?limit=1"),
        api<Page<Job>>("admin", "ingestion/jobs?limit=5"),
      ]);
      setMe(profile);
      setCounts({
        documents: docs.total ?? 0,
        branches: branches.total ?? 0,
        people: people.total ?? 0,
      });
      setJobs(recent.items);
      setLoaded(true);
    } catch (e) {
      toast("error", e instanceof ApiError ? e.message : "Could not load this organization.");
    }
  }, [toast]);

  useEffect(() => {
    void load();
  }, [load]);

  const busy = jobs.some((j) => j.status === "RUNNING" || j.status === "QUEUED");
  useEffect(() => {
    if (!busy) return;
    const timer = setInterval(() => void load(), 5000);
    return () => clearInterval(timer);
  }, [busy, load]);

  const failures = jobs.filter((j) => j.status === "FAILED").length;

  const tiles = [
    { label: "Documents", value: counts.documents, href: "/admin/knowledge", cta: "Manage" },
    { label: "Branches", value: counts.branches, href: "/admin/branches", cta: "Manage" },
    { label: "People", value: counts.people, href: "/admin/people", cta: "Manage" },
  ];

  return (
    <div className="shell">
      <TopBar console_="admin" subtitle={me?.organization.name} />

      <section style={{ marginBottom: 22 }}>
        <span className="eyebrow">Signed in as {me?.user.email ?? "…"}</span>
        <h1 style={{ marginTop: 8 }}>{me?.organization.name ?? " "}</h1>
        <p className="lede">
          Anyone can ask this organization a question at{" "}
          {me ? (
            <Link href={`/c/${me.organization.slug}`}>
              beacon/c/{me.organization.slug}
            </Link>
          ) : (
            "…"
          )}
          , and the answers come from whatever is in Knowledge.
        </p>
      </section>

      {counts.documents === 0 && loaded && (
        <div className="notice">
          <div>
            <strong>Nothing to answer from yet.</strong> Until a document finishes processing,
            every question comes back empty.{" "}
            <Link href="/admin/knowledge">Add your first document →</Link>
          </div>
        </div>
      )}

      {failures > 0 && (
        <div className="notice error">
          <div>
            {failures} document{failures === 1 ? "" : "s"} failed to process and{" "}
            {failures === 1 ? "is" : "are"} not answering anything.{" "}
            <Link href="/admin/ingestion">See why →</Link>
          </div>
        </div>
      )}

      <div className="grid" style={{ marginBottom: 18 }}>
        {tiles.map((tile) => (
          <Link key={tile.label} href={tile.href} className="stat">
            <span className="eyebrow">{tile.label}</span>
            <span className="value">{loaded ? tile.value : "—"}</span>
            <span className="go">{tile.cta} →</span>
          </Link>
        ))}
      </div>

      <section className="card">
        <div className="spread card-head">
          <div>
            <h2>Recent processing</h2>
            <p className="hint" style={{ margin: 0 }}>
              Every upload runs through the pipeline before it can answer.
            </p>
          </div>
          <Link href="/admin/ingestion" className="pill">
            See all
          </Link>
        </div>

        {!loaded ? (
          <div className="stack">
            {[0, 1].map((i) => (
              <div key={i} className="skeleton" style={{ height: 30 }} />
            ))}
          </div>
        ) : jobs.length === 0 ? (
          <div className="empty">
            <div className="mark">·</div>
            Nothing processed yet.
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
                    <span className={`dot ${job.status === "RUNNING" ? "live" : ""}`} />
                    {job.status === "RUNNING"
                      ? (STAGE_LABEL[job.current_stage] ?? job.current_stage)
                      : job.status.toLowerCase()}
                  </span>
                  <span className="muted" style={{ fontSize: 12 }}>
                    {formatWhen(job.created_at)}
                  </span>
                </span>
                {job.status === "FAILED" && (
                  <Link href="/admin/ingestion" className="pill danger">
                    Retry
                  </Link>
                )}
              </div>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
