import Link from "next/link";

import PublicOrgs from "@/components/PublicOrgs";
import { API_BASE } from "@/lib/api/session";

type Organization = { id: string; name: string; slug: string };

async function loadOrganizations(): Promise<Organization[] | null> {
  try {
    const response = await fetch(`${API_BASE}/api/v1/public/organizations`, {
      cache: "no-store",
    });
    if (!response.ok) return null;
    return (await response.json()) as Organization[];
  } catch {
    // The directory is rendered on the server, so an unreachable API should
    // produce an honest empty state rather than a crashed page.
    return null;
  }
}

export default async function Landing() {
  const organizations = await loadOrganizations();

  return (
    <main className="shell">
      <header className="topbar">
        <div className="brand">
          <span className="mark">B</span>
          Beacon
        </div>
        <nav className="nav">
          <Link href="/admin/login">Organization sign in</Link>
          <Link href="/owner/login">Platform</Link>
        </nav>
      </header>

      <section className="hero">
        <span className="eyebrow">Ask any of them anything</span>
        <h1 className="display">
          Every company here
          <br />
          answers for itself.
        </h1>
        <p className="lede">
          Each one has published its own handbooks, policies and branch details. Pick one and
          ask — answers come with the exact pages they came from. No account needed.
        </p>
      </section>

      {organizations === null ? (
        <div className="notice error">
          The directory is unavailable right now. The API may not be running.
        </div>
      ) : (
        <PublicOrgs organizations={organizations} />
      )}
    </main>
  );
}
