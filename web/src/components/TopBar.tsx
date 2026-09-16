"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";

import ThemeToggle from "@/components/ThemeToggle";

const TABS: Record<"owner" | "admin", { href: string; label: string }[]> = {
  owner: [
    { href: "/owner", label: "Organizations" },
    { href: "/owner/operators", label: "Operators" },
  ],
  admin: [
    { href: "/admin", label: "Overview" },
    { href: "/admin/knowledge", label: "Knowledge" },
    { href: "/admin/ingestion", label: "Processing" },
    { href: "/admin/branches", label: "Branches" },
    { href: "/admin/people", label: "People" },
  ],
};

export default function TopBar({
  console_,
  subtitle,
}: {
  console_: "owner" | "admin";
  subtitle?: string | null;
}) {
  const router = useRouter();
  const pathname = usePathname();

  async function signOut() {
    await fetch(`/api/${console_}/session`, { method: "DELETE" });
    router.replace(`/${console_}/login`);
    router.refresh();
  }

  return (
    <header className="topbar">
      <div className="brand">
        <span className="mark">B</span>
        {subtitle ?? "Beacon"}
        <span className="pill">{console_ === "owner" ? "platform" : "organization"}</span>
      </div>

      <nav className="nav">
        {TABS[console_].map((tab) => (
          <Link
            key={tab.href}
            href={tab.href}
            aria-current={pathname === tab.href ? "page" : undefined}
          >
            {tab.label}
          </Link>
        ))}
        <ThemeToggle />
        <button className="ghost small" onClick={signOut}>
          Sign out
        </button>
      </nav>
    </header>
  );
}
