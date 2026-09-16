"use client";

import { useEffect, useState } from "react";

type Theme = "dark" | "light";

/**
 * Light or dark, remembered per browser.
 *
 * The stylesheet defines both palettes; this only stamps `data-theme` on the
 * root. Reading localStorage is wrapped because a private window can throw on
 * access rather than merely return nothing.
 */
export default function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>("dark");

  useEffect(() => {
    let stored: string | null = null;
    try {
      stored = localStorage.getItem("beacon-theme");
    } catch {
      stored = null;
    }
    const initial: Theme = stored === "light" ? "light" : "dark";
    setTheme(initial);
    document.documentElement.setAttribute("data-theme", initial);
  }, []);

  function flip() {
    const next: Theme = theme === "dark" ? "light" : "dark";
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("beacon-theme", next);
    } catch {
      // A preference that cannot be saved is still worth applying for this visit.
    }
  }

  return (
    <button
      className="ghost small"
      onClick={flip}
      aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
      title={`Switch to ${theme === "dark" ? "light" : "dark"} mode`}
    >
      {theme === "dark" ? "☾" : "☀"}
    </button>
  );
}
