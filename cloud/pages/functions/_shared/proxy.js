// Shared server-side proxy helpers for the Pages Functions under
// cloud/pages/functions/api/. See DESIGN.md §9 (as corrected — the web app
// is static assets PLUS a server-side Pages Function proxy, not a purely
// static page) and doc/RUNBOOK_INFRA.md Part F.
//
// Why this file exists at all: GET /snapshot and POST /refresh on the
// Worker require the `x-web-read-token` header. A purely static page would
// have to embed that token in client-side JS, where anyone loading the page
// could read it. This module holds the token server-side (as a Pages
// environment secret, `env.WEB_READ_TOKEN`) and injects it on the way to
// the Worker — the browser only ever calls same-origin `/api/...` and never
// sees the token in any request it makes or any response it receives.
//
// Files/folders prefixed with `_` are ignored by Cloudflare Pages'
// file-based Functions router, so this module is never itself reachable as
// a route — only imported by functions/api/snapshot.js and
// functions/api/refresh.js.

// Deployed Worker URL (doc/RUNBOOK_INFRA.md Part B). Overridable via the
// WORKER_BASE_URL Pages environment variable so the URL isn't hardcoded in
// two places (this file and the docs) — set it once in wrangler.toml
// [vars] and it flows through; this is only a last-resort fallback for an
// environment that somehow has no vars configured at all (e.g. a bare
// `wrangler pages dev` invocation with no config file).
export const DEFAULT_WORKER_BASE_URL = "https://etoro-nightwatch-worker.japlie214.workers.dev";

// Exactly the two Worker routes this proxy is allowed to forward to.
// Deliberately NOT a catch-all: adding a new proxied route means adding it
// here explicitly, on purpose, not automatically because some new Worker
// path happened to exist. This is belt-and-braces on top of the fact that
// only two files exist under functions/api/ in the first place (Pages'
// own file-based routing already refuses anything else with a 404) —
// see cloud/pages/README.md.
export const ALLOWED_WORKER_PATHS = new Set(["/snapshot", "/refresh"]);

/** JSON response helper, mirroring the Worker's own utils.js shape. */
export function json(body, init = {}) {
  return new Response(JSON.stringify(body), {
    ...init,
    headers: { "content-type": "application/json; charset=utf-8", ...(init.headers || {}) },
  });
}

/**
 * Checks required config is present and returns what's missing.
 * Mirrors the Worker's own /health philosophy (index.js `handleHealth`):
 * report *that* something required is absent, never leak the value of
 * anything that *is* configured.
 */
export function resolveConfig(env) {
  const workerBaseUrl = (env && env.WORKER_BASE_URL) || DEFAULT_WORKER_BASE_URL;
  const missing = [];
  if (!env || !env.WEB_READ_TOKEN) missing.push("WEB_READ_TOKEN");
  if (!workerBaseUrl) missing.push("WORKER_BASE_URL");
  return { workerBaseUrl, missing };
}

/**
 * Forwards one request to the Worker, injecting the read-token header
 * server-side. `path` must be one of ALLOWED_WORKER_PATHS — this is checked
 * unconditionally, even though today only two call sites exist, so this
 * function can never silently become an open proxy if another route is
 * added carelessly later.
 *
 * Returns a Response whose status/body mirror the Worker's own response
 * (so e.g. a 409 "no device token" or a 502 FCM error passes straight
 * through for the UI to render) — this function never invents its own
 * error shape for an upstream failure, only for local misconfiguration.
 */
export async function proxyToWorker({ env, path, method = "GET", fetchImpl = fetch }) {
  if (!ALLOWED_WORKER_PATHS.has(path)) {
    // Defensive only — not reachable via the current route files, which is
    // exactly the point (see ALLOWED_WORKER_PATHS comment above).
    return json({ error: "path not allowed" }, { status: 400 });
  }

  const { workerBaseUrl, missing } = resolveConfig(env);
  if (missing.length > 0) {
    return json(
      { error: "server not configured", missing, note: "set these as Pages secrets/vars; see cloud/pages/README.md" },
      { status: 500 }
    );
  }

  let upstream;
  try {
    upstream = await fetchImpl(`${workerBaseUrl}${path}`, {
      method,
      headers: { "x-web-read-token": env.WEB_READ_TOKEN },
    });
  } catch (err) {
    return json({ error: "upstream request failed", message: String((err && err.message) || err) }, { status: 502 });
  }

  const text = await upstream.text();
  return new Response(text, {
    status: upstream.status,
    headers: { "content-type": upstream.headers.get("content-type") || "application/json; charset=utf-8" },
  });
}
