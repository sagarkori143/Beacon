/** Small shared helpers for presentation. Nothing here talks to the API. */

/** A stable, pleasant colour per organization, derived from its name.
 *
 * Deterministic so the same company keeps the same colour between visits --
 * random-per-render colours make a directory feel unstable, and people
 * genuinely navigate by colour before they finish reading.
 */
export function tint(seed: string): { bg: string; fg: string } {
  let hash = 0;
  for (let i = 0; i < seed.length; i += 1) {
    hash = (hash * 31 + seed.charCodeAt(i)) % 360;
  }
  return {
    bg: `linear-gradient(145deg, hsl(${hash} 62% 58%), hsl(${(hash + 42) % 360} 64% 50%))`,
    fg: "#05070d",
  };
}

/** Up to two letters, the way an avatar wants them. */
export function initials(name: string): string {
  const words = name.trim().split(/\s+/).filter(Boolean);
  if (words.length === 0) return "?";
  if (words.length === 1) return words[0].slice(0, 2).toUpperCase();
  return (words[0][0] + words[words.length - 1][0]).toUpperCase();
}

export function formatDuration(ms: number | null | undefined): string | null {
  if (ms == null) return null;
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  return `${Math.floor(ms / 60_000)}m ${Math.round((ms % 60_000) / 1000)}s`;
}

export function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86_400) return `${Math.floor(seconds / 3600)}h ago`;
  return new Date(iso).toLocaleDateString();
}

/** The pipeline, in the order a watcher sees it. */
export const STAGES = [
  "UPLOADED",
  "PARSING",
  "OCR",
  "CLEANING",
  "CHUNKING",
  "EMBEDDING",
  "INDEXING",
  "VALIDATING",
  "ACTIVATING",
  "COMPLETED",
] as const;

export const STAGE_LABEL: Record<string, string> = {
  UPLOADED: "Received",
  PARSING: "Reading the file",
  OCR: "Checking for scanned pages",
  CLEANING: "Tidying the text",
  CHUNKING: "Splitting into sections",
  EMBEDDING: "Building the index",
  INDEXING: "Storing",
  VALIDATING: "Checking it is usable",
  ACTIVATING: "Going live",
  COMPLETED: "Ready",
  FAILED: "Failed",
};

/** Turn a stage's raw detail into short, readable chips. */
export function stageChips(stage: string, detail: Record<string, unknown>): string[] {
  const chips: string[] = [];
  const n = (key: string) => (typeof detail[key] === "number" ? (detail[key] as number) : null);

  switch (stage) {
    case "PARSING": {
      const pages = n("pages");
      if (pages) chips.push(`${pages} page${pages === 1 ? "" : "s"}`);
      break;
    }
    case "OCR": {
      if (detail.skipped) chips.push("not a PDF");
      if (detail.mode) chips.push(String(detail.mode).toLowerCase());
      const ocrPages = n("ocr_page_count");
      if (ocrPages) chips.push(`${ocrPages} page${ocrPages === 1 ? "" : "s"} scanned`);
      if (Array.isArray(detail.reasons)) {
        chips.push(...(detail.reasons as string[]).slice(0, 2));
      }
      break;
    }
    case "CHUNKING": {
      const chunks = n("chunks");
      const sections = n("sections");
      if (chunks) chips.push(`${chunks} chunks`);
      if (sections) chips.push(`${sections} sections`);
      break;
    }
    case "EMBEDDING": {
      const vectors = n("vectors");
      if (vectors) chips.push(`${vectors} vectors`);
      if (detail.model) chips.push(String(detail.model));
      break;
    }
    case "INDEXING": {
      const written = n("written");
      if (written) chips.push(`${written} written`);
      break;
    }
    case "ACTIVATING": {
      const v = n("activated_version");
      if (v) chips.push(`version ${v} live`);
      const replaced = n("replaced_version");
      if (replaced) chips.push(`replaced v${replaced}`);
      break;
    }
    case "CLEANING": {
      const chars = n("characters");
      if (chars) chips.push(`${chars.toLocaleString()} characters`);
      break;
    }
  }
  return chips.filter(Boolean).slice(0, 4);
}
