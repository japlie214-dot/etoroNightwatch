# etoroNightwatch — Cloudflare Pages web app

Static dashboard plus a server-side **Pages Function proxy** to the etoroNightwatch Worker.
See `doc/DESIGN.md` §9 (web app / Worker architecture — corrected: this is not a purely static
page) and §4.5 (sleeve accounting). The user-facing setup walkthrough (creating the Pages
project, setting secrets, enabling Cloudflare Access) lives in `doc/RUNBOOK_INFRA.md` Part F —
this file is developer-focused only and does not repeat that.

## T083 local protected demo controls — not deployed

This source supersedes T081's unchanged-Functions/no-controls inventory below. The
public asset inventory is still five files; `app.js` and `index.html` now expose only
read-only demo broker reconciliation and pause-new-demo-orders requests, not legacy
stored-upload refresh. Server status is loaded again on reopen or explicit reload.
Send acceptance is not device application; expiry is not proof of non-execution.

New `functions/_middleware.js` fails closed for every asset, alias and API until a
trusted `DEMO_CONTROL_AUTH` service binding verifies visitor credentials/ACL and returns
an explicit operator read or write grant. An arbitrary identity header is not a grant.
All protected responses disable caching. This is local source, not Access attestation.

New `functions/api/demo-control.js` uses only a `DEMO_CONTROL_WORKER` service binding;
it never injects `WEB_READ_TOKEN` or accepts an arbitrary upstream route. It preserves
visitor credentials for independent Worker authorization. POST requires same-origin
Origin/Fetch Metadata and JSON. The Worker's `DEMO_CONTROL_ORIGIN` must match this
protected Pages origin. No device acknowledgment route is exposed through Pages.

The exact verifier/dispatch bindings, envelope and device obligations are documented
in `../worker/README.md`, section T083. No configuration, authentication deployment,
transport, device or broker change is made here. Missing bindings explicitly disable
controls (or return protected-monitor unavailable); do not deploy this source assuming
existing snapshot credentials are sufficient. Retain the separate anonymous-denial,
verified APK and observed device-round-trip release gates.

## T081 local release candidate — private demo only

This is release preparation, not a deployment or current Access attestation.
Use the protected site's root `/` as the single Telegram `/dashboard` landing:

- `/` (`public/index.html`, `style.css`, `app.js`, `staleness.js`) is the existing
  live-source snapshot monitor. It reads the latest stored `/api/snapshot` response,
  not a streaming feed. Environment (DEMO / REAL / UNKNOWN), ARMED / DRY, cloud
  receipt time and portfolio source freshness remain separate. A response explicitly
  marked `stub: true` is labeled **API FIXTURE**, never live evidence. Missing/error
  responses do not fall back to synthetic values.
- `/station.html` is the approved standalone synthetic Market station design.
  The always-visible mode banner and return link distinguish it from the monitor;
  charts, capital map, receipts, example orders and optional what-if lab are fictional.
  Its scripts/data, bundled Lightweight Charts 4.2.3 and embedded Apache 2.0 license
  are retained unchanged. No CDN or API dependency is added. Its resource policy
  denies network connections; the existing TradingView attribution links remain.
- Both views link prominently to each other without JavaScript. Station values are
  never imported into the monitor or uploaded to the Worker. `stub-snapshot.json`
  stays outside `public/`; it is test input, not a served asset or rollout payload.
- Release static contents are exactly those five files in `public/`. Keep the existing
  three Pages Functions (`functions/api/snapshot.js`, `functions/api/refresh.js`,
  `functions/_shared/proxy.js`) in the Pages project unchanged. This is not a new
  route, backend, remote-pause control or Telegram polling implementation.

Safe, dependency-free local check (from this directory):

```sh
node --test test/dashboard.node-check.mjs
```

This exercises rendering against mocked responses, legacy/missing/incomplete snapshots,
fixture labeling, view links, the static asset inventory and station script syntax.
It does not execute chart interactions in a browser or verify deployed protection.
The separate Vitest suite requires its already-installed dependencies; do not install
packages or run Wrangler as part of this offline release-prep check.

### Deployment gates (for the separately authorized rollout)

- Deploy from this Pages directory with `public/` as static output and the existing
  sibling `functions/` included. A static-only upload omits the protected proxy.
  Never publish the repository root, scratch source, tests, stub or local secret files.
- Verify present-day anonymous denial for `/`, `/station.html` (including Pages'
  extensionless `/station` alias) and `/api/snapshot`, including any alternate hostname
  or preview URL being shared, before sending any demo data. The Worker read token
  alone is not visitor authentication. If protection is missing, stop; this candidate
  neither changes authentication nor authorizes an authentication change.
- Verify authorized synthetic rendering without overwriting the live snapshot.
  Then reconcile the approved demo-only unattended post-session upload and Telegram
  `/dashboard` reply against the same Z10 session. Source time, completeness and
  execution mode must be checked, not inferred from a recent cloud receipt.
- Telegram's configured address must be the protected absolute HTTPS root URL, with
  no embedded credentials, controls/whitespace, query or fragment. Do not put a token
  in the link. Bot settings and phone configuration are not changed here. Replies can
  be delayed until the phone's next eligible session; no faster polling is added.
- Real-account data, secret/config changes, APK build/install, device access and live
  API calls are not part of this local candidate. The historical commands below are
  background developer documentation, not steps executed or authorization to run them.

## Why a Function, not a purely static page

`GET /snapshot` and `POST /refresh` on the Worker both require the `x-web-read-token` header. A
purely static page has no server of its own, so it would have to embed that token in
client-side JavaScript — at which point anyone who loads the page can read it out of the page
source or the network tab. That is exactly the flaw DESIGN.md §9.3 uses to reject the
Telegram-bot-as-API idea, and it applies here just as much: the token gates real portfolio data.

The fix: `functions/api/*.js` are **Cloudflare Pages Functions** — they run server-side, on
Cloudflare's edge, before the response reaches the browser. They hold `WEB_READ_TOKEN` as a
Pages **environment secret** and inject it into the outgoing request to the Worker. The browser
only ever calls same-origin `/api/snapshot` and `/api/refresh` and never sees the token in any
request it sends or any response it receives — verified below (see **Verification**).

## Layout

```
cloud/pages/
  functions/
    api/
      snapshot.js    GET  /api/snapshot  -> Worker GET  /snapshot
      refresh.js     POST /api/refresh   -> Worker POST /refresh
    _shared/
      proxy.js       shared proxy logic: header injection, path allowlist, config checks
                      (Pages ignores `_`-prefixed paths for routing, so this is never itself
                      a reachable route)
  public/            static assets served as-is, no build step
    index.html       Telegram landing / live-source stored snapshot monitor
    station.html     disconnected synthetic Market station, self-contained
    style.css
    app.js           renders the dashboard from /api/snapshot; calls /api/refresh
    staleness.js     pure staleness-classification function, imported by app.js AND by tests
  stub-snapshot.json  hand-written stub matching the real snapshot shape (see below)
  test/               vitest suite (no real credentials, no network — fetch is mocked)
  wrangler.toml       Pages project config: build output dir, WORKER_BASE_URL var
```

Only two proxied routes exist, on purpose (DESIGN.md's corrected §9 requirement — no open
proxy): `functions/_shared/proxy.js` allowlists exactly `/snapshot` and `/refresh` even though,
today, only two call sites exist that could ever pass anything else. Any other `/api/*` path has
no matching Function file at all, so Cloudflare Pages' own file-based router never invokes a
Function for it (it falls through to the static SPA shell — see **A note on unmatched `/api/*`
paths** below).

## Prerequisites

- Node v26+, npm 11+ (matches `cloud/worker/`).
- `npx wrangler` (pinned via `devDependencies`, currently wrangler 4.x — same version as the
  Worker for consistency).

Install once:

```sh
cd cloud/pages
npm install
```

## Running locally

```sh
npx wrangler pages dev public
```

This serves `public/` as static assets and wires up `functions/api/*.js` automatically —
`wrangler` detects the `functions/` directory next to the given asset directory. Default port is
`8788` unless already taken (pass `--port` to pin it).

### Setting local secrets

`npx wrangler pages secret put` only affects the **deployed** Pages project. For local
`wrangler pages dev`, create `cloud/pages/.dev.vars` (gitignored — see the repo's `.gitignore`):

```
WEB_READ_TOKEN=<the same value as the Worker's WEB_READ_TOKEN secret>
WORKER_BASE_URL=https://etoro-nightwatch-worker.japlie214.workers.dev
```

`WEB_READ_TOKEN` must be the **same value** already set on the deployed Worker (see
`cloud/worker/wrangler.toml`) — this is the credential the Worker's `GET /snapshot` and
`POST /refresh` routes check via the `x-web-read-token` header. `WORKER_BASE_URL` only needs to
be set locally if you want to point at something other than the deployed Worker (e.g. a second
`wrangler dev` instance of the Worker itself); it otherwise falls back to the deployed URL as a
documented default (see `wrangler.toml` `[vars]` and `functions/_shared/proxy.js`).

Without `.dev.vars`, every `/api/*` call returns a structured `500` naming exactly what's
missing (never a stack trace, never a guess) — this mirrors the Worker's own `/health`
philosophy of reporting presence/absence as booleans, never leaking a value.

### A note on unmatched `/api/*` paths

`wrangler pages dev`'s default SPA-style fallback means a path with **no** matching static asset
and **no** matching Function file (e.g. `/api/device-token`, which intentionally has no
`functions/api/device-token.js`) is served the `index.html` shell with a `200`, not a `404`.
This is standard Cloudflare Pages behavior, not a proxy hole: no Function runs for that request,
nothing is forwarded to the Worker, and the token is never touched. The actual security boundary
is that `functions/_shared/proxy.js`'s `ALLOWED_WORKER_PATHS` allowlist — and the fact that only
two Function files exist at all — makes it structurally impossible for `/api/*` to reach any
Worker route beyond `/snapshot` and `/refresh`.

## Tests

```sh
npx vitest run
```

Covers (see `test/proxy.test.js` and `test/staleness.test.js`):

- The proxy injects `x-web-read-token` into the outgoing request to the Worker.
- The token never appears in the response body or headers sent back to the caller.
- The Worker's own status/body (e.g. a `409` "no device token registered yet", or an FCM error)
  passes through unchanged for the UI to render.
- The path allowlist rejects anything outside `/snapshot` and `/refresh` **without ever calling
  `fetch`** — i.e. a misuse of `proxyToWorker` can't become an open proxy even in principle.
- Missing `WEB_READ_TOKEN` produces a structured `500` naming what's missing, again without
  calling `fetch`.
- The pure staleness classifier (`public/staleness.js`): fresh (< 1h, green) / stale (1h–24h,
  amber) / very-stale (>= 24h, red) / none (no snapshot yet), including the exact 1-hour boundary
  and a future-timestamp (clock skew) edge case.

## The stub snapshot

`stub-snapshot.json` is a hand-written JSON document matching the real shape the phone will
eventually push (derived from `doc/DESIGN.md` §3 `portfolio_snapshots` and §4.5 sleeve fields).
It exists purely to eyeball the full UI before real data exists — it is **not** consumed
automatically by anything; there is no code path that loads it. To use it locally:

```sh
curl -s -X POST http://127.0.0.1:8787/snapshot \
  -H "x-phone-push-secret: <the Worker's PHONE_PUSH_SECRET, e.g. from .dev.vars in cloud/worker>" \
  -H "content-type: application/json" \
  --data-binary @../pages/stub-snapshot.json
```

against a locally running `cloud/worker` (`npx wrangler dev` there, default port `8787`), then
load `http://127.0.0.1:8788/` here. This was **not** run against the live deployed Worker as part
of building this app, to avoid overwriting the existing manual-testing stub sitting in the real
KV — see the caveat below instead.

The UI must also survive the **much smaller** shape actually sitting in the live Worker KV right
now (`{"stub":true,"equity_usd":0}`, left over from manual testing) — `public/app.js` treats
every snapshot field as optional and renders `—` for anything absent, rather than crashing. This
*was* exercised against the real deployed Worker (`GET /api/snapshot` through a locally running
`wrangler pages dev` pointed at the production `WORKER_BASE_URL`); see the top-level task report
for the exact commands and output.

## Deploying

```sh
npx wrangler pages deploy public --project-name etoro-nightwatch-pages
```

(Create the project first — see `doc/RUNBOOK_INFRA.md` Part F.) Set the production secret once,
before or after the first deploy:

```sh
npx wrangler pages secret put WEB_READ_TOKEN --project-name etoro-nightwatch-pages
```

**Before treating a deployment as done, enable Cloudflare Access in front of it** — see
`doc/RUNBOOK_INFRA.md` Part F. Without it, `/api/snapshot` is an unauthenticated public endpoint
serving portfolio data to anyone who has the URL; the Worker-side token only stops the *browser*
from seeing the credential, it does nothing to stop a stranger from loading the page and calling
the Function directly.

## Env vars / secrets summary

| Name | Kind | Set via | Purpose |
|---|---|---|---|
| `WEB_READ_TOKEN` | secret | `npx wrangler pages secret put WEB_READ_TOKEN` (prod) / `.dev.vars` (local) | Same value as the Worker's `WEB_READ_TOKEN` secret. Injected server-side into `x-web-read-token`. Never reaches the browser. |
| `WORKER_BASE_URL` | var | `wrangler.toml` `[vars]` (prod default already set to the deployed Worker URL) / `.dev.vars` to override locally | Base URL the Function proxies to. Not hardcoded in two places — one default, in one file. |
