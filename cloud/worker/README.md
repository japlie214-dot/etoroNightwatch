# etoroNightwatch — Cloudflare Worker

Snapshot store, FCM relay (device-token registration, on-demand refresh, and
the manual command channel), and the redundant daily cron trigger. See
`doc/DESIGN.md` §9 (web app / Worker architecture), §7.2 (the DST-safe cron
trigger), and §8 (Telegram / FCM inbound). The user-facing setup walkthrough
(Cloudflare account, Firebase project, KV namespace creation in the
dashboard, etc.) lives in `doc/RUNBOOK_INFRA.md` — this file is
developer-focused only and does not repeat that.

## T083 local control boundary — unavailable until separately wired

This source adds `GET/POST /demo-control` and device-only `POST /demo-control/ack`.
It does not configure or deploy any binding, verifier or device transport. Historical
legacy `/refresh`, `/command` and snapshot routes below are unchanged and are not this
control protocol. A snapshot reader credential grants no authority on these new routes.

Required integration boundaries (configuration changes need separate approval):

- `DEMO_CONTROL_STORE`: dedicated Durable Object namespace exporting `DemoControlStore`
  from `src/index.js`, with its own migration/storage. All callers use the same
  `demo-control-v1` object. Never substitute eventually consistent snapshot KV.
- `DEMO_CONTROL_AUTH`: trusted service binding whose `fetch(Request)` verifies the
  original credentials, issuer/audience, expiry and operator ACL or device identity.
  It returns 401/403 for denied credentials, non-2xx for unavailable verification,
  or JSON containing server-owned `deviceId`, `environment: "demo"`, `capabilities`
  (the supported actions), and explicit `operatorReadAuthorized` and
  `operatorWriteAuthorized` booleans. Never derive these from caller identity headers,
  snapshot credentials, capability claims or receipt JSON flags.
- For `/demo-control/ack`, AUTH must verify the **v1 canonical receipt signature**, bind all
  evidence to that identity/execution, and return `proof` with `authenticated: true`,
  matching `deviceId` and `environment: "demo"`, signed `issuedAt`/`deadlineAt`, plus verified
  action-specific evidence below. Merely authenticating a phone session is not evidence
  verification. The inactive cryptographic adapter and exact Kotlin contract are below.
- `DEMO_CONTROL_DISPATCH`: trusted service binding accepting only POST `/dispatch`
  with the version-1 JSON envelope below. It must authenticate/sign the complete
  envelope for the designated demo-capable device, not forward a generic command.
  A 2xx means transport acceptance only. Non-2xx is `send_failed`; transport exceptions
  or a crash leave `unconfirmed`. Neither establishes non-execution.
- `DEMO_CONTROL_ORIGIN`: exact protected Pages HTTPS origin, without trailing slash.
  Operator writes require that Origin, `Sec-Fetch-Site: same-origin` and JSON. Device
  acknowledgments instead require the independently verified signed device proof.

Browser POST body (no caller timestamps/authority/extra fields):

```json
{"requestId":"12345678-1234-4234-8234-123456789abc","action":"refresh_demo_broker","deviceId":"synthetic-device","environment":"demo"}
```

Dispatch envelope (timestamps are integer Unix milliseconds, deadline = issue + 120000):

```json
{"version":1,"requestId":"12345678-1234-4234-8234-123456789abc","action":"refresh_demo_broker","deviceId":"synthetic-device","environment":"demo","issuedAt":1000000,"deadlineAt":1120000}
```

Device acknowledgment JSON carries the same requestId/action/deviceId/environment and
`executedAt`. Refresh additionally carries signed `brokerObservation` evidence:
`{observedAt, account:true, positions:true, openOrders:true}`; observation must be strictly
post-issue and no later than execution. Pause uses `action: "pause_demo_orders"` with
signed `pausePersisted:true` and `finalSubmissionGuard:true` evidence. AUTH, not the
payload, supplies the verified proof to the reducer. No raw broker records are stored.

Device bridge must persist UUID replay protection across restart **before effects**,
verify demo mode/capability and deadline before execution, perform refresh through
read-only reconciliation (never strategy execution), and persist pause at the actual
final new-order guard. Execution at/after deadline is rejected; a timely execution may
be reported later. No resume, order submission, cancellation or position closing exists
in this protocol. Z10 restart/guard proof and a correlated demo round trip remain release gates.

UUID reservations and accepted receipts are atomic and permanent. A crash between
reservation and dispatch deliberately does not auto-resend. Status reads derive
`pending`, `expired_unconfirmed` or immutable `applied` separately from delivery.
`GET /demo-control` returns latest records per action for the authorized device;
`?requestId=<UUID>` retrieves an older record with the same device authorization.
Missing verification, storage or transport is explicit unavailability, never applied state.

### Device receipt cryptographic adapter v1 — inactive

`src/demo-control-device-verifier.js` exports:

- `deviceReceiptSigningBytes(payload)` for the canonical unsigned payload;
- `verifyDeviceReceipt(envelope, { pins, now })` for verification and reducer-compatible
  device proof (integer server milliseconds for `now`);
- `createDeviceReceiptVerifier({ pins, now: () => serverMilliseconds })`, an AUTH-compatible
  `fetch(Request)` adapter restricted to POST `/demo-control/ack`. It grants neither
  operator read nor write authority. Compose it with independent operator authentication;
  it is not a new deployed endpoint and is not activated by importing this module.

`pins` is a **server-owned Map**, indexed by keyId. Each pin must contain a public Web
Crypto `CryptoKey` in `publicKey`, the bound `deviceId`, `environment: "demo"`, an explicit
supported-action `capabilities` array, and `revoked: false`. No pin/key/certificate is
accepted from the receipt. The key must be RSA at least 2048 bits, algorithm
`RSASSA-PKCS1-v1_5` with SHA-256, with verify usage. Private CryptoKeys are rejected.
Provisioning/import of an approved public SPKI key and composing the auth service remain
separately authorized integration work. No real pin, key, secret file or binding is read,
created or configured here. Pins are checked each call and again after verification to
reject an in-flight revocation/replacement.

For Kotlin use `Signature.getInstance("SHA256withRSA")`: PKCS#1 v1.5, **not RSA-PSS**.
Sign the following exact UTF-8 bytes, using ASCII LF (`0x0a`) after **every** line,
including the final line; no CR, BOM, spaces or blank lines:

```text
nightwatch.demo-control.receipt.v1
alg=RS256
keyId=synthetic-pin-v1
requestId=12345678-1234-4234-8234-123456789abc
action=refresh_demo_broker
deviceId=synthetic-device
environment=demo
issuedAt=1000000
deadlineAt=1120000
executedAt=1001000
observedAt=1000500
account=true
positions=true
openOrders=true
```

For pause, replace the action line with `action=pause_demo_orders` and replace the four
refresh-evidence lines with exactly `pausePersisted=true` then
`finalSubmissionGuard=true`, again with a final LF. Evidence booleans are literal
lowercase `true`/`false`, not `1`/`0`; a correctly signed false/partial claim still cannot
establish an applied transition. Field order is fixed in **signing bytes**, independent
of JSON property order. Domain/version and fixed algorithm are part of the signed bytes.

HTTP envelope example (signature placeholder, not a usable fixture):

```json
{"version":1,"keyId":"synthetic-pin-v1","requestId":"12345678-1234-4234-8234-123456789abc","action":"refresh_demo_broker","deviceId":"synthetic-device","environment":"demo","issuedAt":1000000,"deadlineAt":1120000,"executedAt":1001000,"brokerObservation":{"observedAt":1000500,"account":true,"positions":true,"openOrders":true},"signature":"<unpadded-base64url-signature>"}
```

The signature is unpadded base64url (`A-Z a-z 0-9 - _`), with no wrapping. HTTP body must
be compact UTF-8 JSON: no whitespace, BOM, duplicate/escaped field aliases, alternate
number encodings or trailing bytes. Property order may vary. The adapter requires JSON
content type and enforces the existing 8192-byte body limit before parsing/verification.
Only the exact shown top-level fields and exact action-specific evidence fields are
recognized; pause uses its two top-level boolean evidence fields, not brokerObservation.
No `alg`, publicKey, receivedAt, raw broker data or authority claims are accepted.
UUIDs must be lowercase. keyId/deviceId are 1–128 ASCII characters from
`A-Z a-z 0-9 . _ : -`. Timestamps are nonnegative safe integer decimal milliseconds,
not strings, fractional/exponential values or negative zero, and must leave room for
the 120000-ms deadline arithmetic.

**Issue time is server authority.** The device echoes the issuedAt/deadlineAt it received;
it does not choose them. After signature verification, `DemoControlStore` atomically
compares proof.issuedAt and proof.deadlineAt with its stored server-created request.
Even a valid device signature over a shifted deadline is rejected there. Execution must
be within that original window, refresh observation must be strictly post-issue and no
later than execution, and future device times are rejected against the server clock.
Timely execution can still be reported after expiry. Cloud receivedAt is created only by
the server and never accepted as a signed device field.

Synthetic test keys are generated in memory only. From repository root:

```sh
node --test cloud/worker/test/demo-control-device-verifier.node.mjs
```

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
