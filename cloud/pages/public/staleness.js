// Pure staleness-classification logic, shared by public/app.js (browser)
// and test/staleness.test.js (vitest) via the same ES module — no build
// step, so this file is loaded directly by both a <script type="module">
// tag and a plain `import` in tests.
//
// DESIGN.md §9 calls out snapshot staleness as a risk: "it must be
// impossible to misread stale data as live." The thresholds below are
// deliberately simple and are the single source of truth for both the
// dashboard's color and its text.

export const STALENESS_THRESHOLDS_MS = {
  fresh: 60 * 60 * 1000, // < 1 hour: green
  stale: 24 * 60 * 60 * 1000, // < 1 day: amber
  // >= 1 day: red
};

/**
 * Classifies snapshot age.
 *
 * @param {string|null|undefined} updatedAtIso - the snapshot's `updated_at`
 *   ISO8601 string, or null/undefined if no snapshot has ever been pushed.
 * @param {Date|number} [now] - injectable "current time" for tests;
 *   defaults to `new Date()`.
 * @returns {{level: "none"|"fresh"|"stale"|"very-stale", ageMs: number|null}}
 */
export function classifyStaleness(updatedAtIso, now = new Date()) {
  if (!updatedAtIso) return { level: "none", ageMs: null };

  const updatedAtMs = new Date(updatedAtIso).getTime();
  if (Number.isNaN(updatedAtMs)) return { level: "none", ageMs: null };

  const nowMs = now instanceof Date ? now.getTime() : now;
  const ageMs = Math.max(0, nowMs - updatedAtMs);

  if (ageMs < STALENESS_THRESHOLDS_MS.fresh) return { level: "fresh", ageMs };
  if (ageMs < STALENESS_THRESHOLDS_MS.stale) return { level: "stale", ageMs };
  return { level: "very-stale", ageMs };
}

/** Human-readable "how long ago" string for the "as of" line. */
export function formatAge(ageMs) {
  if (ageMs === null || ageMs === undefined) return "unknown";
  const seconds = Math.floor(ageMs / 1000);
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}
