"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type Citation = {
  ref: string;
  source: string;
  locator: string;
  section: string | null;
  scope: string;
};

type Turn = {
  role: "you" | "them";
  text: string;
  citations?: Citation[];
  error?: boolean;
};

const SUGGESTIONS = [
  "What are your opening hours?",
  "What is the cancellation policy?",
  "Do you allow pets?",
];

export default function Chat({ slug, name }: { slug: string; name: string }) {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [draft, setDraft] = useState("");
  const [stage, setStage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const conversation = useRef<string | null>(null);
  const bottom = useRef<HTMLDivElement>(null);
  const box = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, stage]);

  const ask = useCallback(
    async (question: string) => {
      const text = question.trim();
      if (!text || busy) return;

      setDraft("");
      setBusy(true);
      setStage("Thinking");
      setTurns((prior) => [...prior, { role: "you", text }, { role: "them", text: "" }]);

      try {
        const response = await fetch(`/api/public/${slug}/chat/stream`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            message: text,
            conversation_id: conversation.current,
          }),
        });

        if (!response.ok || !response.body) {
          const problem = await response.json().catch(() => ({}));
          throw new Error(problem.detail ?? "That did not work.");
        }

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const citations: Citation[] = [];
        let buffer = "";

        // SSE frames are separated by a blank line, and a chunk boundary can
        // land anywhere -- including mid-frame -- so the tail is carried over
        // rather than parsed.
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });

          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";

          for (const frame of frames) {
            const event = /^event: (.+)$/m.exec(frame)?.[1];
            const raw = /^data: (.*)$/m.exec(frame)?.[1];
            if (!event || !raw) continue;

            let data: Record<string, unknown>;
            try {
              data = JSON.parse(raw);
            } catch {
              continue;
            }

            if (event === "stage") {
              setStage(labelFor(String(data.stage ?? "")));
            } else if (event === "citation") {
              citations.push(data as unknown as Citation);
            } else if (event === "token") {
              setStage(null);
              setTurns((prior) => {
                const next = [...prior];
                next[next.length - 1] = {
                  ...next[next.length - 1],
                  text: next[next.length - 1].text + String(data.text ?? ""),
                };
                return next;
              });
            } else if (event === "error") {
              throw new Error(String(data.message ?? "Something went wrong."));
            } else if (event === "done") {
              if (data.conversation_id) conversation.current = String(data.conversation_id);
              setTurns((prior) => {
                const next = [...prior];
                const answer = next[next.length - 1];
                // Show only sources the answer actually referenced.
                const used = citations.filter((c) => answer.text.includes(c.ref));
                next[next.length - 1] = {
                  ...answer,
                  citations: used.length ? used : citations.slice(0, 3),
                };
                return next;
              });
            }
          }
        }
      } catch (error) {
        setTurns((prior) => {
          const next = [...prior];
          next[next.length - 1] = {
            role: "them",
            text: error instanceof Error ? error.message : "Something went wrong.",
            error: true,
          };
          return next;
        });
      } finally {
        setBusy(false);
        setStage(null);
        box.current?.focus();
      }
    },
    [busy, slug],
  );

  return (
    <div className="chat">
      <div className="thread">
        {turns.length === 0 && (
          <div className="rise" style={{ paddingTop: 26 }}>
            <h2 style={{ fontSize: 19 }}>Ask {name} anything</h2>
            <p className="hint" style={{ maxWidth: "52ch" }}>
              Answers come from documents {name} published, and each one shows the page it
              came from. Nothing you type is tied to an account.
            </p>
            <div className="suggestions">
              {SUGGESTIONS.map((s) => (
                <button key={s} className="suggestion" onClick={() => void ask(s)}>
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}

        {turns.map((turn, index) => (
          <div key={index} className={`turn ${turn.role}`}>
            <span className="who">{turn.role === "you" ? "You" : "◆"}</span>
            <div className="bubble">
              {turn.error ? (
                <div className="notice error" style={{ margin: 0 }}>
                  {turn.text}
                </div>
              ) : (
                <p className={busy && index === turns.length - 1 ? "caret" : undefined}>
                  {turn.text}
                </p>
              )}

              {turn.citations && turn.citations.length > 0 && (
                <div className="cites">
                  {turn.citations.map((c) => (
                    <span
                      key={c.ref}
                      className="cite"
                      title={c.section ?? undefined}
                    >
                      <b>{c.ref}</b>
                      {c.locator}
                      {c.scope === "LOCATION" && <span className="muted">· branch</span>}
                    </span>
                  ))}
                </div>
              )}
            </div>
          </div>
        ))}

        {stage && (
          <div className="turn them">
            <span className="who">◆</span>
            <div className="bubble status-line">
              <span className="spinner" />
              {stage}…
            </div>
          </div>
        )}
        <div ref={bottom} />
      </div>

      <div className="composer">
        <div className="composer-inner">
          <textarea
            ref={box}
            rows={1}
            placeholder={`Ask ${name}…`}
            value={draft}
            disabled={busy}
            onChange={(e) => {
              setDraft(e.target.value);
              e.target.style.height = "auto";
              e.target.style.height = `${Math.min(e.target.scrollHeight, 180)}px`;
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void ask(draft);
              }
            }}
          />
          <button
            className="primary"
            disabled={busy || !draft.trim()}
            onClick={() => void ask(draft)}
            aria-label="Send"
          >
            {busy ? <span className="spinner" /> : "Ask"}
          </button>
        </div>
        <p className="hint" style={{ textAlign: "center" }}>
          Answers are generated from {name}&rsquo;s own documents. Check anything important.
        </p>
      </div>
    </div>
  );
}

function labelFor(stage: string): string {
  switch (stage) {
    case "planning":
      return "Thinking";
    case "retrieving":
      return `Looking through the documents`;
    case "tools":
      return "Checking";
    case "generating":
      return "Writing the answer";
    default:
      return "Working";
  }
}
