"use client";

import { useRef, useState } from "react";

import Timeline from "@/components/Timeline";
import { useToast } from "@/components/Toast";

type Location = { id: string; name: string };

type Queued = {
  key: string;
  file: File;
  state: "waiting" | "uploading" | "processing" | "done" | "failed";
  jobId?: string;
  error?: string;
};

const ACCEPT = ".pdf,.md,.markdown,.txt,.docx,.doc,.html,.htm";
const MAX_BYTES = 50 * 1024 * 1024;

/**
 * Upload one or more documents and watch each one process.
 *
 * Files are sent one at a time rather than all at once: the pipeline is the
 * bottleneck, not the upload, and a queue that finishes in order is far easier
 * to follow than six timelines advancing at once.
 */
export default function Uploader({
  locations,
  onFinished,
}: {
  locations: Location[];
  onFinished: () => void;
}) {
  const toast = useToast();
  const [queue, setQueue] = useState<Queued[]>([]);
  const [scope, setScope] = useState("");
  const [over, setOver] = useState(false);
  const [running, setRunning] = useState(false);
  const picker = useRef<HTMLInputElement>(null);

  function add(files: FileList | null) {
    if (!files?.length) return;
    const accepted: Queued[] = [];
    for (const file of Array.from(files)) {
      if (file.size > MAX_BYTES) {
        toast("error", `${file.name} is larger than 50 MB.`);
        continue;
      }
      accepted.push({ key: `${file.name}-${file.size}-${Math.random()}`, file, state: "waiting" });
    }
    setQueue((prior) => [...prior, ...accepted]);
  }

  function update(key: string, patch: Partial<Queued>) {
    setQueue((prior) => prior.map((q) => (q.key === key ? { ...q, ...patch } : q)));
  }

  async function start() {
    setRunning(true);
    const pending = queue.filter((q) => q.state === "waiting");

    for (const item of pending) {
      update(item.key, { state: "uploading" });

      const form = new FormData();
      form.append("file", item.file);
      if (scope) form.append("location_id", scope);

      try {
        const response = await fetch("/api/admin/documents", { method: "POST", body: form });
        if (!response.ok) {
          const problem = await response.json().catch(() => ({}));
          throw new Error(problem.detail ?? `Upload refused (${response.status})`);
        }
        const { job_id } = (await response.json()) as { job_id: string };
        update(item.key, { state: "processing", jobId: job_id });
      } catch (error) {
        const message = error instanceof Error ? error.message : "Upload failed.";
        update(item.key, { state: "failed", error: message });
        toast("error", `${item.file.name}: ${message}`);
      }
    }
    setRunning(false);
  }

  const waiting = queue.filter((q) => q.state === "waiting").length;
  const active = queue.some((q) => q.state === "uploading" || q.state === "processing");

  return (
    <>
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
          add(e.dataTransfer.files);
        }}
      >
        <div className="file">Drop files here, or click to choose</div>
        <p className="hint">PDF, Word, Markdown, HTML or plain text · up to 50 MB each</p>
        <input
          ref={picker}
          type="file"
          multiple
          accept={ACCEPT}
          hidden
          onChange={(e) => {
            add(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      <div className="row" style={{ marginTop: 14, alignItems: "flex-end" }}>
        <div className="field" style={{ marginBottom: 0 }}>
          <label htmlFor="scope">These documents apply to</label>
          <select
            id="scope"
            value={scope}
            disabled={active}
            onChange={(e) => setScope(e.target.value)}
          >
            <option value="">Everyone — all branches</option>
            {locations.map((l) => (
              <option key={l.id} value={l.id}>
                {l.name} only
              </option>
            ))}
          </select>
        </div>
        <button
          className="primary"
          style={{ flex: "0 0 auto" }}
          disabled={!waiting || running}
          onClick={() => void start()}
        >
          {running && <span className="spinner" />}
          {waiting > 1 ? `Process ${waiting} files` : "Process"}
        </button>
      </div>

      {scope && (
        <p className="hint">
          Branch knowledge wins over the general version on whatever subjects it covers, and
          leaves everything else alone.
        </p>
      )}

      {queue.length > 0 && (
        <div style={{ marginTop: 20 }}>
          {queue.map((item) => (
            <div key={item.key} className="queue-item">
              <div className="queue-head">
                <span className="filename">{item.file.name}</span>
                {item.state === "waiting" && (
                  <button
                    className="ghost small"
                    onClick={() => setQueue((p) => p.filter((q) => q.key !== item.key))}
                  >
                    Remove
                  </button>
                )}
                {item.state === "uploading" && (
                  <span className="pill accent">
                    <span className="spinner" />
                    Uploading
                  </span>
                )}
                {item.state === "failed" && <span className="pill danger">Failed</span>}
              </div>

              {item.error && (
                <p className="hint" style={{ color: "var(--danger)" }}>
                  {item.error}
                </p>
              )}

              {item.jobId && (
                <div style={{ marginTop: 14 }}>
                  <Timeline
                    jobId={item.jobId}
                    onSettled={(status) => {
                      update(item.key, { state: status === "COMPLETED" ? "done" : "failed" });
                      if (status === "COMPLETED") {
                        toast("ok", `${item.file.name} is live and answering questions.`);
                      } else {
                        toast("error", `${item.file.name} could not be processed.`);
                      }
                      onFinished();
                    }}
                  />
                </div>
              )}
            </div>
          ))}

          {!active && queue.length > 0 && (
            <button
              className="ghost small"
              style={{ marginTop: 12 }}
              onClick={() => setQueue([])}
            >
              Clear list
            </button>
          )}
        </div>
      )}
    </>
  );
}
