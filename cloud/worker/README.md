# etoroNightwatch — Cloudflare Worker

Snapshot store, FCM relay (device-token registration, on-demand refresh, and
the manual command channel), and the redundant daily cron trigger. See
`doc/DESIGN.md` §9 (web app / Worker architecture), §7.2 (the DST-safe cron
trigger), and §8 (Telegram / FCM inbound). The user-facing setup walkthrough
(Cloudflare account, Firebase project, KV namespace creation in the
dashboard, etc.) lives in `doc/RUNBOOK_INFRA.md` — this file is
developer-focused only and does not repeat that.

## Layout

```
cloud/worker/
  src/
    index.js      routes + scheduled() cron handler
    fcm.js         FCM HTTP v1 client (OAuth token mint/cache, sendPush)
    jwt.js         RS256 service-account JWT signing (Web Crypto only)
    holidays.js     US market holiday/half-day table + ET/DST decision logic
    commands.js     /command allowlist + HMAC-of-(command, counter)
    utils.js        constant-time secret compare, base64url, JSON helpers
  test/            vitest suite (no real credentials, no network)
  wrangler.toml     Worker config, cron schedule, KV binding, secret list
```

No build step: `wrangler dev` / `wrangler deploy` run `src/index.js`
directly (plain ES modules, no bundler config needed beyond wrangler's
built-in esbuild pass).

## Prerequisites

- Node v26+, npm 11+ (verified against the versions in this repo's dev
  environment).
- `npx wrangler` (pinned via `devDependencies`, currently wrangler 4.x).

Install once:

```sh
cd cloud/worker
npm install
```

## Running locally — no real credentials needed

Everything is designed to fail loudly and specifically rather than crash
when secrets are absent, so you can exercise the whole route surface with
no Cloudflare account and no Firebase project yet:

```sh
npx wrangler dev
```

This starts a local dev server (default `http://localhost:8787`) backed by
Miniflare, including a local, ephemeral KV store — no real KV namespace id
is required for `wrangler dev` (the placeholder `id` in `wrangler.toml` is
only used for `wrangler deploy`).

Hit `/health` first — it's the diagnostic endpoint, unauthenticated by
design, and reports which secrets/vars/KV are configured **as booleans
only**, never their values:

```sh
curl -s http://localhost:8787/health | python -m json.tool
```

With nothing configured you'll see every entry under `secrets_present` and
`vars_present` as `false`, `kv_bound: true` (wrangler dev always binds a
local KV store), and `ready: false`.

Every other route requires a secret and returns `401` without one:

```sh
curl -i -X POST http://localhost:8787/snapshot -d '{"equity": 100}'
# -> 401
```

### Setting local secrets for a fuller local test

`npx wrangler secret put` only affects the *deployed* Worker. For local
`wrangler dev`, create `cloud/worker/.dev.vars` (already gitignored):

```
FIREBASE_SERVICE_ACCOUNT_JSON={"type":"service_account","project_id":"...","private_key":"-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n","client_email":"...@....iam.gserviceaccount.com", ...}
PHONE_PUSH_SECRET=dev-phone-secret
WEB_READ_TOKEN=dev-web-token
COMMAND_HMAC_SECRET=dev-command-secret
FIREBASE_PROJECT_ID=your-firebase-project-id
```

Until a real Firebase project exists, you can still exercise every route
except the two that actually call FCM (`/refresh`, `/command`) — those will
fail with a clear `fcm not configured` / `FIREBASE_...` error rather than
a stack trace, once you set a real service-account JSON.

Docs: https://developers.cloudflare.com/workers/configuration/secrets/#local-development-with-secrets

## Tests

```sh
npx vitest run
```

No network access and no real credentials are used. Where a real
service-account private key would normally be involved (JWT signing, OAuth
token exchange, FCM send), the tests generate a throwaway RSA keypair with
Web Crypto at test time and stub `fetch`. KV is a small in-memory
stand-in implementing only the `get`/`put`/`delete` surface this code
uses.

Covered: every route's auth rejection (missing/wrong secret), `/health`'s
secret-presence reporting (and that it never leaks a value), a full
snapshot round trip through the fake KV, `/command` rejecting any command
outside the DESIGN.md §8.2 read-only + halt/resume set, the cron ET/DST
decision logic (`shouldFireSessionTrigger`) across an EST date, an EDT
date, a weekend, a full holiday, and a half day, and the JWT signing path
(structure + claims + a real Web Crypto signature verification against the
throwaway public key).

## Deploying

```sh
npx wrangler login          # once, opens a browser
npx wrangler kv namespace create NIGHTWATCH_SNAPSHOTS
# -> paste the printed id into wrangler.toml's [[kv_namespaces]] id field
npx wrangler deploy
```

Then set the real secrets on the deployed Worker (never committed, never
put in `wrangler.toml`):

```sh
npx wrangler secret put FIREBASE_SERVICE_ACCOUNT_JSON
npx wrangler secret put PHONE_PUSH_SECRET
npx wrangler secret put WEB_READ_TOKEN
npx wrangler secret put COMMAND_HMAC_SECRET
```

`FIREBASE_PROJECT_ID` is not a secret (it's a public project identifier) —
set it in `wrangler.toml`'s `[vars]` block instead.

After deploying, `curl https://<your-worker>.workers.dev/health` should
report `ready: true` once all four secrets and the KV namespace are in
place.

## Route summary

| Route | Auth header | Purpose |
|---|---|---|
| `GET /health` | none | build/config diagnostic; booleans only |
| `POST /snapshot` | `x-phone-push-secret` | phone pushes a portfolio snapshot |
| `GET /snapshot` | `x-web-read-token` | web app reads the latest snapshot + `updated_at` |
| `POST /refresh` | `x-web-read-token` | fires a high-priority `refresh` FCM push |
| `POST /command` | `x-web-read-token` | HMAC-signs and pushes one allowlisted command |
| `POST /device-token` | `x-phone-push-secret` | phone registers/updates its FCM token |

`/command` body: `{"command": "status"}` (or any of `positions`, `pnl`,
`today`, `logs`, `next`, `halt`, `resume` — see `src/commands.js`). Anything
else is rejected with `400` before it ever reaches the phone.

## What's stubbed / not independently verified

- FCM HTTP v1 request/response shapes and the OAuth2 service-account JWT
  flow are implemented per Google's own documentation (cited inline in
  `src/fcm.js` and `src/jwt.js`), but have **not** been exercised against
  real Firebase/Google endpoints — there is no Firebase project yet. The
  JWT signing path itself *is* verified end-to-end in tests (a real Web
  Crypto RS256 signature, checked against the matching public key), just
  not the network round trip to `oauth2.googleapis.com` / `fcm.googleapis.com`.
- `wrangler dev`'s local KV emulation is used for local testing; the real
  KV namespace has not been created yet (no Cloudflare account).
- `@cloudflare/vitest-pool-workers` was intentionally not used — plain
  `vitest` with the handlers called directly and `fetch`/KV stubbed is
  simpler here and avoids pulling in Miniflare's pool machinery just to
  unit-test route logic that doesn't touch anything Workers-specific
  beyond Web Crypto (which Node also implements natively).
