// The manual command channel (DESIGN.md §8.2, §9.4): POST /command lets
// the user push one of a fixed, read-only-plus-halt/resume set of Telegram
// commands via FCM instead of Telegram. Anything outside this set is
// rejected here, at the Worker — it must never reach the phone.
export const ALLOWED_COMMANDS = new Set([
  "status",
  "positions",
  "pnl",
  "today",
  "logs",
  "next",
  "halt",
  "resume",
  // Read-only meta-command. Must stay in step with SUPPORTED_COMMANDS in
  // app/src/main/python/nightwatch/notify/commands.py - a command allowed on the
  // phone but rejected here would look like a bug.
  "help",
  // --- Probe/diagnostic commands (android-probe v2). ---------------------
  // These drive the on-device proof APK: they exercise the wake path and push
  // results back to /snapshot. They are read-only or scheduling-only - none of
  // them can open, close or modify a position, which keeps the "a compromised
  // control channel must never be able to move money" invariant (DESIGN 6.I).
  // Must stay in step with the phone-side dispatch in android-probe.
  "ping",
  "runchecks",
  "schedule",
  "selftest",
]);

/** Normalizes "/status", "STATUS", " status " etc. to "status". */
export function normalizeCommand(raw) {
  if (typeof raw !== "string") return null;
  const trimmed = raw.trim().toLowerCase();
  return trimmed.startsWith("/") ? trimmed.slice(1) : trimmed;
}

export function isAllowedCommand(raw) {
  const cmd = normalizeCommand(raw);
  return cmd !== null && ALLOWED_COMMANDS.has(cmd);
}

/**
 * HMAC-SHA256 over "<command>:<counter>", hex-encoded. The phone verifies
 * this the same way (DESIGN.md §8.2) and rejects a counter at or below the
 * last-accepted value as a replay.
 */
export async function hmacCommand(secret, command, counter) {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(`${command}:${counter}`));
  return [...new Uint8Array(sig)].map((b) => b.toString(16).padStart(2, "0")).join("");
}
