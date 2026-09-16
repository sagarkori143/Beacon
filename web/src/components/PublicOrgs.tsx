"use client";

import Link from "next/link";
import { useMemo, useState } from "react";

import { initials, tint } from "@/lib/ui";

type Organization = { id: string; name: string; slug: string };

export default function PublicOrgs({ organizations }: { organizations: Organization[] }) {
  const [query, setQuery] = useState("");

  const shown = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return organizations;
    return organizations.filter(
      (o) =>
        o.name.toLowerCase().includes(needle) || o.slug.toLowerCase().includes(needle),
    );
  }, [organizations, query]);

  if (organizations.length === 0) {
    return (
      <div className="empty">
        <div className="mark">·</div>
        No companies have been published yet.
      </div>
    );
  }

  return (
    <>
      <div className="spread" style={{ marginBottom: 16 }}>
        <div>
          <h2>Companies</h2>
          <p className="hint" style={{ margin: 0 }}>
            {shown.length} of {organizations.length}
          </p>
        </div>
        {organizations.length > 4 && (
          <input
            style={{ maxWidth: 260 }}
            placeholder="Search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            aria-label="Search companies"
          />
        )}
      </div>

      {shown.length === 0 ? (
        <div className="empty">Nothing matches “{query}”.</div>
      ) : (
        <div className="grid">
          {shown.map((o, index) => {
            const colour = tint(o.slug);
            return (
              <Link
                key={o.id}
                href={`/c/${o.slug}`}
                className="org-card"
                style={{ animationDelay: `${Math.min(index, 8) * 35}ms` }}
              >
                <span className="avatar" style={{ background: colour.bg, color: colour.fg }}>
                  {initials(o.name)}
                </span>
                <span className="name">{o.name}</span>
                <span className="go">Ask a question →</span>
              </Link>
            );
          })}
        </div>
      )}
    </>
  );
}
