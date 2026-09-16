import Link from "next/link";
import { notFound } from "next/navigation";

import Chat from "@/components/Chat";
import { API_BASE } from "@/lib/api/session";
import { initials, tint } from "@/lib/ui";

type Organization = { id: string; name: string; slug: string };

async function loadOrganization(slug: string): Promise<Organization | null> {
  try {
    const response = await fetch(
      `${API_BASE}/api/v1/public/organizations/${encodeURIComponent(slug)}`,
      { cache: "no-store" },
    );
    if (!response.ok) return null;
    return (await response.json()) as Organization;
  } catch {
    return null;
  }
}

export default async function CompanyChat({
  params,
}: {
  params: Promise<{ slug: string }>;
}) {
  const { slug } = await params;
  const organization = await loadOrganization(slug);
  if (!organization) notFound();

  const colour = tint(organization.slug);

  return (
    <main className="shell narrow">
      <header className="topbar" style={{ marginBottom: 8 }}>
        <div className="brand">
          <span
            className="avatar"
            style={{ background: colour.bg, color: colour.fg, width: 30, height: 30, fontSize: 12 }}
          >
            {initials(organization.name)}
          </span>
          {organization.name}
        </div>
        <nav className="nav">
          <Link href="/">← All companies</Link>
        </nav>
      </header>

      <Chat slug={organization.slug} name={organization.name} />
    </main>
  );
}
