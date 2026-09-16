"use client";

import { useRouter } from "next/navigation";

export default function TopBar({
  console_,
  subtitle,
}: {
  console_: "owner" | "admin";
  subtitle?: string | null;
}) {
  const router = useRouter();

  async function signOut() {
    await fetch(`/api/${console_}/session`, { method: "DELETE" });
    router.replace(`/${console_}/login`);
    router.refresh();
  }

  return (
    <header className="topbar">
      <div className="brand">
        <h1>Beacon</h1>
        <span className="badge">{console_ === "owner" ? "platform" : "organization"}</span>
        {subtitle && <span className="muted">{subtitle}</span>}
      </div>
      <button className="link" onClick={signOut}>
        Sign out
      </button>
    </header>
  );
}
