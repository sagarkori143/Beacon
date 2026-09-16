"use client";

import { useEffect, useRef, useState } from "react";

import { STAGES, STAGE_LABEL, formatDuration, stageChips } from "@/lib/ui";

type StageEvent = {
  stage: string;
  status: string;
  progress: number;
  message?: string | null;
  duration_ms?: number | null;
  detail?: Record<string, unknown>;
};

type Props = {
  jobId: string;
  /** Called once the job reaches a terminal state, so a parent can refresh. */
  onSettled?: (status: "COMPLETED" | "FAILED") => void;
};

/**
 * A document moving through the pipeline, as it happens.
 *
 * The stream sends the job's recorded history first and then follows the live
 * feed, so opening this late still shows the whole run rather than starting
 * from whatever happens to be next.
 */
export default function Timeline({ jobId, onSettled }: Props) {
  const [seen, setSeen] = useState<Record<string, StageEvent>>({});
  const [failed, setFailed] = useState<StageEvent | null>(null);
  const [connected, setConnected] = useState(true);
  const settled = useRef(false);

  useEffect(() => {
    const source = new EventSource(`/api/admin/ingestion/jobs/${jobId}/stream`);

    source.addEventListener("stage", (raw) => {
      const event = JSON.parse((raw as MessageEvent).data) as StageEvent;
      setSeen((prior) => ({ ...prior, [event.stage]: event }));
      if (event.status === "FAILED") setFailed(event);
    });

    source.addEventListener("done", () => {
      source.close();
      setConnected(false);
    });

    source.onerror = () => {
      // EventSource reconnects on its own; the stream sends history again on
      // reconnect, so nothing is lost and there is nothing to do here but note
      // that the connection is not currently up.
      setConnected(false);
    };

    return () => source.close();
  }, [jobId]);

  const reached = Object.values(seen);
  const failure = failed ?? null;
  const done = Boolean(seen.COMPLETED) || Boolean(failure);
  const progress = failure ? 1 : Math.max(0, ...reached.map((e) => e.progress), 0.05);

  useEffect(() => {
    if (done && !settled.current) {
      settled.current = true;
      onSettled?.(failure ? "FAILED" : "COMPLETED");
    }
  }, [done, failure, onSettled]);

  // Which stage is currently in flight: the first one not yet reported.
  const currentIndex = STAGES.findIndex((s) => !seen[s]);

  return (
    <div>
      <div className="spread" style={{ marginBottom: 10 }}>
        <span className="status-line">
          {failure ? (
            <span className="pill danger">
              <span className="dot" />
              Failed at {STAGE_LABEL[failure.stage] ?? failure.stage}
            </span>
          ) : done ? (
            <span className="pill ok">
              <span className="dot" />
              Ready
            </span>
          ) : (
            <span className="pill accent">
              <span className="dot live" />
              {connected ? "Processing" : "Reconnecting"}
            </span>
          )}
        </span>
        <span className="muted mono" style={{ fontSize: 12 }}>
          {Math.round(progress * 100)}%
        </span>
      </div>

      <div className="progress-track">
        <div
          className="progress-fill"
          style={{
            width: `${progress * 100}%`,
            background: failure ? "var(--danger)" : undefined,
          }}
        />
      </div>

      <div className="timeline">
        {STAGES.map((stage, index) => {
          const event = seen[stage];
          const isFailure = failure?.stage === stage;
          const state = isFailure
            ? "failed"
            : event
              ? "done"
              : index === currentIndex && !done
                ? "active"
                : "pending";

          // A stage nobody will reach adds noise once the run has stopped.
          if (state === "pending" && done) return null;

          const chips = event?.detail ? stageChips(stage, event.detail) : [];

          return (
            <div key={stage} className={`step ${state}`}>
              <span className="marker">
                {state === "done" ? "✓" : state === "failed" ? "!" : index + 1}
              </span>
              <div className="step-body">
                <div className="step-name">
                  {STAGE_LABEL[stage] ?? stage}
                  {state === "active" && <span className="spinner" />}
                  {event?.duration_ms != null && (
                    <span className="muted" style={{ fontWeight: 400, fontSize: 12 }}>
                      {formatDuration(event.duration_ms)}
                    </span>
                  )}
                </div>

                {isFailure && failure?.message && (
                  <p className="hint" style={{ color: "var(--danger)" }}>
                    {failure.message}
                  </p>
                )}

                {chips.length > 0 && (
                  <div className="step-detail">
                    {chips.map((chip) => (
                      <span key={chip} className="chip">
                        {chip}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
