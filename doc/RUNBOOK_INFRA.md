# RUNBOOK_INFRA — Firebase + Cloudflare + Telegram, from zero

**Status:** infrastructure only. No Android app, no eToro credentials, no phone involved yet.
This is deliberate — see DESIGN §11 step 1: the question being answered here is *"does the
free-tier push/relay stack actually work,"* answerable with nothing but a laptop, a Google
account, and a Cloudflare account.

**Audience marker used throughout:**

- 🖱️ **YOU** — a console click, a credential, a decision only you can make.
- ⚙️ **REPO** — already scripted or will be scripted by another agent; you run one command.

Nothing here touches `app/`, `tests/`, or `cloud/` — those trees are owned by other agents in
this session. This runbook only tells you what to click and type; it creates no application
code.

---

## Part A — Firebase project (Spark, free tier)

### A.1 Create the project 🖱️ YOU

1. Go to https://console.firebase.google.com and sign in with the Google account you want to
   own this project long-term (not a throwaway — you'll be minting service-account keys
   against it).
2. **Add project** → name it (e.g. `etoronightwatch`) → accept or decline Google Analytics
   (decline; nothing here needs it) → **Create project**.
3. You land on the **Spark plan** by default — no billing account attached, no prompt to add
   one. Confirm the plan badge in the console reads **Spark**.

**Cost check, verified, not assumed.** Firebase's own pricing page lists **Cloud Messaging
(FCM)** under Spark's "No-cost products" — it is free on Spark with no usage cap and no
Blaze/billing requirement. (https://firebase.google.com/pricing) Nothing in Part A or Part B
requires you to attach a credit card. If the console ever prompts you to "upgrade to Blaze," it
is asking for a feature you don't need (Cloud Functions, most likely) — decline it.

### A.2 Add an Android app to the project 🖱️ YOU

Do this now even though the APK doesn't exist yet (DESIGN §11 step 3 is deferred) — the
`google-services.json` and the package-name binding are needed before any Android work starts,
and getting the package name locked in early avoids a re-registration later.

1. Project Overview → click the **Android icon** ("Add app") → **Android**.
2. **Android package name** — this must exactly match the app's `applicationId` in
   `app/build.gradle.kts` when that file is written. **Recommended: `com.nightwatch.etoro`.**
   Pick your own if you prefer, but write it down now — see the box in §A.6.
3. App nickname: anything (e.g. "etoroNightwatch"). Debug SHA-1: leave blank for now — only
   needed for Google Sign-In, which this app doesn't use.
4. **Register app.**

**Changing the package name later is expensive.** It is the primary key Firebase uses to bind
`google-services.json` to your app; a rename means deleting and re-adding the Android app in
the Firebase console and re-downloading the config file. Decide it here, once.
(https://firebase.google.com/docs/android/setup)

### A.3 Download `google-services.json` 🖱️ YOU

1. Still in the "Add app" wizard: **Download google-services.json**.
2. Its eventual home in this repo is **`app/google-services.json`** (module root, next to
   `app/build.gradle.kts` — that file doesn't exist yet since Android tooling is deferred to
   DESIGN §11 step 3, so for now just keep the download somewhere safe).
3. **Gitignore it now anyway**, before it exists in the tree, so it can never land in a commit
   by accident:

   ⚙️ REPO — add this line to the repo's `.gitignore` (it currently has no `app/` entries at
   all):

   ```
   app/google-services.json
   ```

   **Why gitignore a file with "no private key"?** Correct — `google-services.json` is not a
   secret in the credential sense (it ships inside every public APK that uses Firebase). But it
   identifies *this specific project* — project id, project number, OAuth client ids, mobile SDK
   app id — and there's no reason to let a fork or a leaked repo silently target your Firebase
   project. Treat it as project-identifying, not secret-bearing, and keep it out of git anyway.
4. Skip the rest of the wizard (**Add Firebase SDK**, `google-services` Gradle plugin) — that's
   Android build-system work, out of scope for this runbook and for DESIGN §11 step 3.

### A.4 Create the sending service account 🖱️ YOU

This is the credential the Cloudflare Worker uses to call the FCM HTTP v1 API.

1. In the Firebase console: ⚙️ (gear icon, top left) → **Project settings** → **Service
   accounts** tab. Firebase auto-creates a default service account here
   (`firebase-adminsdk-*@<project-id>.iam.gserviceaccount.com`) with the Firebase Admin SDK's
   default permissions — that default account is sufficient for sending FCM; you do not need to
   hand-create a second one unless you want tighter scoping.
2. Click **Generate new private key** → confirm → a JSON file downloads. This is the file the
   Worker uses to mint OAuth access tokens for FCM HTTP v1 sends.
3. **If you want a scoped-down account instead of the default Admin SDK one:** Google Cloud
   Console → IAM & Admin → Service Accounts → Create service account on this same project, then
   grant it the role verified below.

**Verified role.** Google's own Firebase Cloud Messaging authentication doc
(https://firebase.google.com/docs/cloud-messaging/auth-server) documents authorizing HTTP v1
sends via a service account with the `https://www.googleapis.com/auth/firebase.messaging`
OAuth scope. The predefined IAM role that carries the permission this scope needs
(`cloudmessaging.messages.create`) is **`roles/firebasecloudmessaging.admin`**, labeled
**"Firebase Cloud Messaging API Admin"** in the console
(https://cloud.google.com/iam/docs/roles-permissions/firebasecloudmessaging). The
Admin-SDK default service account created in step 1 already carries equivalent permission
project-wide; a manually-created service account needs this role granted explicitly on the
project (IAM & Admin → IAM → grant → paste the service account email → role: **Firebase Cloud
Messaging API Admin**).

**Legacy key — does not apply here, confirmed.** The old "Cloud Messaging" tab server key
(`AAAA...` string, no expiry, no scoping) is not an option to fall back to: Google deprecated
sending via the legacy HTTP/XMPP APIs on **2023-06-20** and shut them down completely by
**2024-07-22** (multiple independent confirmations, including Microsoft's own migration notices
for Azure Notification Hubs). Only the HTTP v1 API + OAuth service-account flow works now — the
Worker (§B.4) is written against v1 from the start, so there is no migration debt here.

### A.5 The service-account JSON is a private key — state this plainly 🖱️ YOU (read, don't act yet)

Unlike `google-services.json`, this file **can mint an OAuth token that sends push notifications
to your app as you**. Treat it exactly like an API secret:

- Never commit it to the repo, in this project or any fork.
- Never put it in a Worker's *code* — only in a Worker *secret* (§B.4), which is encrypted at
  rest and never appears in `wrangler.toml` or in `git`.
- Never let it reach a browser — the web app only ever reads a snapshot through the separate,
  low-privilege read token (DESIGN §9.4), never this key.
- Never let it land inside the APK — the phone doesn't send FCM messages, it only *receives*
  them; sending is entirely the Worker's job, keeping this key off the device.

Delete the downloaded JSON from your Downloads folder once it's loaded into the Worker secret in
§B.4 — don't leave a long-lived copy sitting in plaintext.

### A.6 What to record 🖱️ YOU

Write these down now (Project settings → General tab has all four):

| Item | Where to find it | Used for |
|---|---|---|
| **Project ID** | Project settings → General | Worker config, OAuth token audience |
| **Project number** | Project settings → General | FCM HTTP v1 endpoint URL (`https://fcm.googleapis.com/v1/projects/{project-id}/messages:send` uses the *project id*, not the number, but both are commonly needed for troubleshooting) |
| **Android package name** | Project settings → General → Your apps, or what you typed in §A.2 | Must match `applicationId` when the Gradle module is written (DESIGN §11 step 3) |
| **Android app id** (`1:NNNN:android:xxxx`) | Project settings → General → Your apps | Identifies this specific app registration inside the project; needed if you ever re-download `google-services.json` |

---

## Part B — Cloudflare account and Worker

### B.1 Create a Cloudflare account 🖱️ YOU

1. https://dash.cloudflare.com/sign-up — free account, email + password (or SSO). No credit
   card required for the Workers Free plan.
2. You do **not** need to add a domain/zone to use Workers — a Worker gets a
   `*.<your-subdomain>.workers.dev` URL automatically. Skip any "add a site" prompt.

**Free-tier limits that matter here** — verified against Cloudflare's own limits pages, not
guessed:

| Resource | Free-tier limit | Source |
|---|---|---|
| Worker requests | 100,000 / day (hard cap; extra requests error, don't bill) | https://developers.cloudflare.com/workers/platform/limits/ |
| CPU time per invocation | 10 ms (HTTP request), 10 ms (Cron Trigger) | same |
| Cron Triggers | **5 per account** (not per Worker) | same; corroborated by https://github.com/cloudflare/cloudflare-docs/issues/29326 |
| Workers KV reads | 100,000 / day | https://developers.cloudflare.com/kv/platform/limits/ |
| Workers KV writes | 1,000 / day (to distinct keys); writes to the *same* key are additionally limited to 1/second | same |
| Workers KV storage | 1 GB per account, 1 GB per namespace | same |
| Workers KV value size | 25 MiB max per value | same |
| Cloudflare Pages builds | 500 / month, 1 concurrent build | https://developers.cloudflare.com/pages/platform/limits/ |
| Pages projects | 100 per account | same |

**Fit check for this app.** One daily session pushing one snapshot, plus occasional on-demand
refreshes and inbound commands, is on the order of tens of Worker invocations and KV writes a
day — several orders of magnitude under every ceiling above. The one limit worth watching
later is **Cron Triggers: 5/account** — the daily FCM-trigger cron uses one of the five; leave
headroom if you plan other scheduled Workers on this same account.

### B.2 Authenticate wrangler ⚙️ REPO / 🖱️ YOU

Wrangler 4.128.0 is already confirmed working on this machine via `npx` — no global install.
This step opens a browser for you to approve the OAuth grant (🖱️ YOU), the command itself is
already correct (⚙️ REPO):

```
npx wrangler login
```

It opens `dash.cloudflare.com` in your default browser, asks you to approve, and writes a token
to wrangler's local config (not into the repo). Confirm with:

```
npx wrangler whoami
```

### B.3 Create the KV namespace for snapshots 🖱️ YOU + ⚙️ REPO

1. 🖱️ YOU decide the namespace name — e.g. `NIGHTWATCH_SNAPSHOTS`.
2. Create it:

   ```
   npx wrangler kv namespace create NIGHTWATCH_SNAPSHOTS
   ```

   This prints an `id` (and, separately, a `preview_id` if you also create a preview namespace).
3. ⚙️ REPO: that `id` goes into `cloud/worker/wrangler.toml` under a `[[kv_namespaces]]` block —
   the other agent building `cloud/worker/` owns writing this file; your job is to hand them (or
   paste yourself, since you have write access) the printed id:

   ```toml
   [[kv_namespaces]]
   binding = "SNAPSHOTS"
   id = "<the id wrangler printed>"
   ```

   Do this after `cloud/worker/` exists — if it doesn't yet when you reach this step, hold the
   printed id and come back once the Worker scaffold lands.

### B.4 Set Worker secrets 🖱️ YOU

`wrangler secret put` reads the value from **stdin**, not a command-line argument — so the
secret never appears in shell history, `ps`, or your terminal scrollback the way a
`--flag=value` would. Run each of these from the repo root (or wherever `wrangler.toml` for the
Worker lives, once `cloud/worker/` exists) and paste the value when prompted:

```
npx wrangler secret put FIREBASE_SERVICE_ACCOUNT_JSON
```
Paste the **entire contents** of the service-account JSON from §A.4 (yes, the whole JSON blob,
as one secret value — the Worker parses it at runtime to mint OAuth tokens). This is the
highest-value secret in the whole system (§E).

```
npx wrangler secret put PHONE_PUSH_SECRET
```
A secret **you generate** — e.g. `openssl rand -hex 32` or PowerShell
`-join ((1..32) | ForEach-Object { '{0:x2}' -f (Get-Random -Max 256) })`. Authenticates the
phone's snapshot push to the Worker (DESIGN §9.4). The same value must later be configured
on-device (in `config/.secrets.env` or app-private storage — the phone side isn't built yet).

```
npx wrangler secret put WEB_READ_TOKEN
```
A second, **different** secret you generate the same way. Authenticates the static web app's
reads from the Worker. Deliberately a separate credential from the push secret — different
privilege, same separation principle DESIGN §6.F applies to eToro keys (read vs write).

```
npx wrangler secret put COMMAND_HMAC_SECRET
```
A third, different secret. This is the HMAC key the Worker (and, later, the phone) use to sign
and verify inbound commands over FCM and Telegram (DESIGN §8.2, §3 `commands` table).

**Enumerate what you just created** — four secrets, four distinct purposes, none reused:

| Secret name | Value origin | Consumed by |
|---|---|---|
| `FIREBASE_SERVICE_ACCOUNT_JSON` | Downloaded in §A.4 | Worker, to mint OAuth tokens and call FCM HTTP v1 |
| `PHONE_PUSH_SECRET` | You generate | Worker, to authenticate the phone's snapshot push |
| `WEB_READ_TOKEN` | You generate | Worker, to authenticate the web app's snapshot read |
| `COMMAND_HMAC_SECRET` | You generate | Worker + (later) phone, to verify inbound command HMACs |

Verify what's set (names only, never values) with:

```
npx wrangler secret list
```

### B.5 Deploy 🖱️ YOU / ⚙️ REPO

Once `cloud/worker/` has code (owned by another agent in this session):

```
npx wrangler deploy
```

Confirm the printed URL (`https://<worker-name>.<your-subdomain>.workers.dev`) responds.

**Verify the cron trigger registered:**

1. Cloudflare dashboard → Workers & Pages → your Worker → **Triggers** tab → **Cron Triggers**
   section should list the schedule from `wrangler.toml`'s `[triggers] crons = [...]`.
2. Or from the CLI: `npx wrangler triggers deploy` re-syncs triggers if the dashboard looks
   stale after an edit to `wrangler.toml`.
3. To confirm it actually *fires* without waiting for the real schedule, trigger it manually
   from the dashboard (**Triggers** tab → **Trigger Cron Event** button, if present for your
   wrangler/dashboard version) or temporarily set the cron to a near-future minute, deploy,
   watch it fire in `wrangler tail` (§B.6), then set it back.

### B.6 Tail logs 🖱️ YOU

```
npx wrangler tail
```

Streams live `console.log`/exception output from the deployed Worker. Run this in one terminal
while you exercise the Worker from another (curl, the dashboard's "Trigger Cron Event", or the
Telegram/webapp flows once they exist) — it's the fastest way to see what an FCM send actually
returned, or why a KV write failed, before any phone is involved.

---

## Part C — Telegram

### C.1 Create the bot 🖱️ YOU

1. Open Telegram, message **@BotFather**.
2. `/newbot` → give it a name and a unique username ending in `bot` (e.g.
   `etoronightwatch_bot`).
3. BotFather replies with a token shaped like `123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx`.
   That's `TELEGRAM_BOT_TOKEN`.

### C.2 Get the chat id 🖱️ YOU

No third-party bot needed — the bot's own API exposes this directly.

**For a private chat (you talking to your own bot):**

1. Open a chat with your new bot in Telegram and send it any message (e.g. `/start`).
2. In a browser, visit:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
   (replace `<TOKEN>` with the full bot token, colon included).
3. The JSON response contains `"message":{"chat":{"id": 123456789, ...}}` — that positive
   integer is your `TELEGRAM_CHAT_ID` for a private chat.

**For a channel (the `-100...` form):**

1. Create the channel (or use an existing one), add your bot as an **administrator** (it needs
   admin rights to post — a plain member won't receive/send via the Bot API the same way).
2. Post any message in the channel.
3. Hit the same `getUpdates` URL — the channel's message arrives with
   `"chat":{"id": -1001234567890, "type":"channel", ...}`. That negative, `-100`-prefixed
   integer is the channel's `TELEGRAM_CHAT_ID`.

**If `getUpdates` returns an empty `result: []`:** Telegram only queues updates the bot hasn't
already fetched (or that arrived after the bot last called `getUpdates` with a webhook unset).
Send a fresh message *after* loading the URL once (to clear any stale offset), then reload.

### C.3 Where these go ⚙️ REPO / 🖱️ YOU

`config/.secrets.env` (already gitignored — confirmed in the repo's `.gitignore`, and
`config/.secrets.env.example` already documents the two keys):

```
TELEGRAM_BOT_TOKEN=123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=123456789
```

or the negative form for a channel. This file is never committed and the codebase's log
redaction already recognizes the `NNNNNNNNN:AA...` bot-token shape and masks it before it can
reach a log line — no separate action needed for that.

### C.4 Smoke-test ⚙️ REPO (once implemented)

```
.venv\Scripts\python.exe -m nightwatch.cli telegram-test
```

(Windows path shown to match this machine; another agent is implementing the `telegram-test`
subcommand now — it doesn't exist yet.) Confirm the message actually lands in the chat/channel
you configured in §C.2 before moving on.

---

## Part D — Verification checklist

Tick these off in order. The point of this section: **draw the line honestly between what can
be verified with zero phone/Android involvement, and what genuinely has to wait.**

### Can verify now (no APK, no eToro credentials)

- [ ] Telegram bot created, token obtained (§C.1)
- [ ] Telegram chat id obtained for the intended destination (§C.2)
- [ ] `telegram-test` sends a message that actually arrives (§C.4)
- [ ] Firebase project created on Spark, no billing attached (§A.1)
- [ ] Android app registered in Firebase, package name recorded (§A.2, §A.6)
- [ ] `google-services.json` downloaded and gitignored (§A.3)
- [ ] Service-account JSON downloaded, understood as a private key (§A.4, §A.5)
- [ ] Cloudflare account created (§B.1)
- [ ] `npx wrangler login` / `whoami` succeed (§B.2)
- [ ] KV namespace created, id recorded (§B.3)
- [ ] All four Worker secrets set via `wrangler secret put` (stdin, not flags) (§B.4)
- [ ] `npx wrangler deploy` succeeds; Worker URL responds to a basic request
- [ ] Cron trigger visible in the dashboard's Triggers tab (§B.5)
- [ ] **Worker mints a Google OAuth access token from the service-account JSON** — exercise the
      Worker's own token-minting code path directly (e.g. a debug route, or a `wrangler tail`
      session while triggering the relevant handler) and confirm it returns a bearer token, not
      an auth error. This proves the JWT-signing + token-exchange flow works end-to-end against
      Google's OAuth endpoint, independent of FCM itself.
- [ ] **Worker sends an FCM message and gets a message-id back, or a documented error for "no
      such registration token."** Call the Worker's send path with a syntactically-valid but
      **fake** FCM registration token. Expect and accept an FCM-side error here —
      `UNREGISTERED` or `INVALID_ARGUMENT` — as the successful outcome of *this* check: it means
      the request reached FCM's HTTP v1 endpoint, was authenticated (the OAuth token from the
      previous check was accepted), and was rejected only for lacking a real device to deliver
      to. That is everything that can be proven without a phone.

### Cannot verify until the APK exists (DESIGN §11 steps 3–4)

- [ ] An actual FCM delivery reaching a real device, end to end
- [ ] `RemoteMessage.getPriority()` reporting `PRIORITY_HIGH` on receipt
- [ ] The phone successfully pushing a real snapshot to the Worker's KV store
- [ ] The web app rendering a real (non-stub) snapshot
- [ ] Any Doze/App-Standby network-availability behavior at all — that requires a phone and
      DESIGN §11 step 3's tooling (JDK/SDK), neither of which exists on this machine yet

**Why this split matters:** the user's original question was "does the free-tier stack work at
all" *before* spending time on Android tooling or handing over eToro keys. Everything in the
first list answers that question completely — a green "no such registration token" error from
FCM is a **pass**, not an inconclusive result, for the free-tier-viability gate (DESIGN §11 step
1). Do not wait for a phone to close out that gate.

---

## Part E — Security notes

### Secret inventory — what lives where, and why

| Secret | Lives in | Never in | Why |
|---|---|---|---|
| FCM service-account JSON | Cloudflare Worker secret (`FIREBASE_SERVICE_ACCOUNT_JSON`) | Repo, browser, APK, Kotlin/Python code | Can mint tokens that send push to your app as you — the single highest-value credential in this system (§A.5). Compromise = attacker can wake your phone, deliver arbitrary payloads to `NightwatchMessagingService`, at will. |
| `google-services.json` | `app/google-services.json`, gitignored | Public repo/fork | Not secret, but project-identifying; no reason to publish it (§A.3) |
| Phone push shared secret | Worker secret + on-device config (once it exists) | Repo, browser | Lets an attacker forge a fake snapshot push, poisoning what the web app displays |
| Web-app read token | Worker secret + the static Pages site's client code | — | Read-only exposure by design; still not something to hand out casually since it gates portfolio data (see below) |
| Command HMAC secret | Worker secret + on-device config | Repo, browser | Forging this lets an attacker issue fake inbound commands (`/halt`, etc.) — read/stop-only by DESIGN §8.2's own design, so worst case is a nuisance halt, not a fraudulent trade |
| eToro API keys | `config/.secrets.env` (already gitignored) — out of scope for this runbook, not created yet | Anywhere in `cloud/` or a browser | Covered by DESIGN §6.F, not this document |
| Telegram bot token | `config/.secrets.env` | Repo, logs (redaction already covers the shape) | Compromise lets an attacker read/send as the bot, but the bot only executes the read/halt-only command table (§8.2) |

**The service-account JSON is the highest-value secret in the entire system** — more so than
any individual eToro key, because eToro keys are pre-split by privilege (read/write/demo,
DESIGN §6.F) while a single leaked FCM service account gives full send capability with no
finer-grained scope available short of creating a narrower custom IAM role.

### Rotation guidance

- **FCM service-account key:** Google Cloud Console → IAM & Admin → Service Accounts → the
  account → Keys tab → **Add key** to create a new one, update the Worker secret
  (`wrangler secret put FIREBASE_SERVICE_ACCOUNT_JSON` again — it overwrites), confirm a send still
  works, *then* delete the old key from the same Keys tab. Never delete the old key before
  confirming the new one works — that's the fast way to lock yourself out of sending.
- **The three generated secrets** (push shared secret, read token, HMAC secret): rotate by
  generating a fresh random value and re-running `wrangler secret put`. Because these gate the
  phone's own push and command flow, rotating them requires updating the on-device config too —
  once the device side exists, treat these as a **coordinated pair change**, not an
  independent Worker-side rotation, or the phone will start failing pushes with a stale secret.
- **Telegram bot token:** BotFather → `/revoke` on the bot to issue a new token; update
  `config/.secrets.env`. Old token stops working immediately.
- Rotate on any suspected exposure (accidental commit, shared screen, leaked log) immediately,
  don't wait for a schedule. Absent a specific trigger, an annual rotation of the service
  account key is a reasonable default — it's the one credential expensive enough to compromise
  that "just in case" rotation is worth the (small) operational cost.

### The snapshot endpoint must never be publicly readable

The Worker's snapshot read route serves **portfolio data** — account and sleeve balances,
positions, P&L (DESIGN §3 `portfolio_snapshots`, §9.4). It must require the
`WEB_READ_TOKEN` on every request, with no anonymous fallback path, no debug bypass left in
from testing, and no accidental `GET` served straight from KV without going through the
Worker's auth check. When `cloud/worker/` code exists, this is worth a specific look during
review: confirm there is no route that reads the KV namespace without checking the token first.

### What never reaches the browser or the APK

Restated from DESIGN §9.4 because it bears repeating here: no Telegram bot token, no eToro API
key, and no FCM service-account key ever reaches the browser (the static web app only holds the
low-privilege read token) or the APK (the phone only *receives* FCM pushes; it never sends
them, so it has no need for the service-account credential at all).

---

## Part F — Cloudflare Pages web app

**Corrects a gap in the original DESIGN.md §9 wording.** §9 originally described the web app as
a "static page" reading the Worker directly, and separately claimed "Secrets exposed to a
browser: None." Read together those two statements don't hold: a purely static page calling
`GET /snapshot` would have to embed the Worker's `x-web-read-token` value in client-side
JavaScript, where anyone loading the page can read it — the same class of flaw §9.3 uses to
reject the Telegram-bot-as-API idea. The web app built in `cloud/pages/` is therefore **static
assets plus a server-side Cloudflare Pages Function proxy**: the Function holds the token as a
Pages environment secret and injects it server-side; the browser only ever calls same-origin
`/api/snapshot` and `/api/refresh`. DESIGN.md §9 has been updated to say this (see the diff
alongside this runbook change). This Part covers the operational steps; `cloud/pages/README.md`
covers local dev, tests, and the exact env vars/secrets — this Part doesn't repeat that.

### F.1 Create the Pages project 🖱️ YOU + ⚙️ REPO

Once `cloud/pages/` has code (already built as of this writing):

```
cd cloud/pages
npx wrangler pages deploy public --project-name etoro-nightwatch-pages
```

The first run of this command creates the Pages project if it doesn't exist yet (wrangler
prompts to confirm the project name) and uploads `public/` plus the `functions/` directory next
to it. Confirm the printed `*.pages.dev` URL loads and shows the dashboard shell (it will report
"not configured" on `/api/snapshot` until §F.2 is done — that's expected, not a bug).

### F.2 Set the Pages secret 🖱️ YOU

```
npx wrangler pages secret put WEB_READ_TOKEN --project-name etoro-nightwatch-pages
```

Paste the **same value** already set on the Worker as `WEB_READ_TOKEN` (Part B.4 above — note
that file currently documents this secret under the name `WEB_READ_TOKEN`; whichever name the
deployed Worker actually uses, the *value* pasted here must match it exactly, since this is the
credential sent as the `x-web-read-token` header on every proxied request). Re-run
`npx wrangler pages deploy public --project-name etoro-nightwatch-pages` after setting it — a
Pages secret takes effect on the next deployment, not instantly on `secret put` the way a Worker
secret does.

Verify with:

```
npx wrangler pages secret list --project-name etoro-nightwatch-pages
```

(names only, never values — same convention as the Worker's `wrangler secret list`).

`WORKER_BASE_URL` is **not** a secret — it's a plain Pages variable, already set to the deployed
Worker's URL as a default in `cloud/pages/wrangler.toml` `[vars]`. Only touch it if the Worker's
URL ever changes.

### F.3 Deploy 🖱️ YOU / ⚙️ REPO

```
npx wrangler pages deploy public --project-name etoro-nightwatch-pages
```

Confirm the printed `https://<project>.pages.dev` (or a custom domain, if one is later attached)
loads the dashboard and that `GET /api/snapshot` returns real (or stub) data, not a "not
configured" error.

### F.4 Enable Cloudflare Access — REQUIRED, not optional 🖱️ YOU

**Do this before telling anyone the URL, and ideally before the first real deploy is left
sitting reachable.** Without it, `https://<project>.pages.dev` is a public, unauthenticated
endpoint: `/api/snapshot` proxies to the Worker with the read token injected *for anyone who
loads the page*, which means anyone who has or guesses the URL sees portfolio data (account and
sleeve balances, positions), and anyone who finds it can also fire `/api/refresh` and wake the
phone. The Pages Function proxy solves the browser-secret-exposure problem (§9.4) — it does
**not** by itself solve "who is allowed to load this page at all." Cloudflare Access is the free
answer to that second question.

1. Cloudflare dashboard → **Zero Trust** (left nav — this may prompt you to "set up Zero Trust"
   the first time; accept the free plan, no credit card required for the tier this app needs).
2. **Access → Applications → Add an application → Self-hosted.**
3. **Application domain**: the Pages project's `*.pages.dev` domain (or custom domain if
   attached). Path: leave as `/` to protect the whole app, including every `/api/*` route —
   don't protect only `public/` and leave `/api/*` unprotected, since that's exactly the
   portfolio-data surface that must not be public.
4. **Policy**: Allow, with an Include rule of **Emails** → your own email address (or a small
   allowlist of specific emails if more than one person needs access). Do not use a broader rule
   like "Everyone" or an email-domain wildcard unless you specifically intend that.
5. Session duration: pick something reasonable for a personal dashboard (e.g. 24h or 1 week) —
   Access re-prompts for a login code via email (or another configured identity provider) when
   the session expires; there's no password to manage.
6. Save. Reload the Pages URL in an incognito/private window to confirm it now redirects to a
   Cloudflare Access login page (a one-time email code by default) before showing anything.

**Free-tier user limit, stated plainly.** Cloudflare Access's free tier covers **up to 50 users**
per account (verified against Cloudflare's own Zero Trust pricing page,
https://www.cloudflare.com/plans/zero-trust-services/) — a single-user personal dashboard like
this one is nowhere near that ceiling, but it's worth knowing before adding a family member,
co-founder, or anyone else to the allowlist in F.4 step 4: the 50-user count is per Cloudflare
account, shared across every Access-protected application on it, not per application.

### F.5 Verification checklist

- [ ] `npx wrangler pages deploy public --project-name etoro-nightwatch-pages` succeeds; URL
      loads the dashboard shell
- [ ] `WEB_READ_TOKEN` set via `wrangler pages secret put`, matching the Worker's value exactly
- [ ] Re-deployed after setting the secret (Pages secrets apply on next deploy, not instantly)
- [ ] `GET /api/snapshot` on the deployed URL returns real/stub data, not a "not configured" 500
- [ ] `POST /api/refresh` on the deployed URL reaches the Worker (a `409`/FCM error is expected
      and fine until a real phone is registered — a `500` "not configured" is not)
- [ ] **Cloudflare Access enabled on the whole app (`/`), not just static assets** (§F.4) —
      confirm an incognito window is challenged for login before it can reach `/api/snapshot`
- [ ] Confirmed, by viewing page source and the browser's Network tab, that `WEB_READ_TOKEN`
      never appears anywhere sent to the browser (see `cloud/pages/README.md` for how this was
      verified locally with `curl` + `grep`; repeat the same check against the deployed URL once
      Access is configured and you can authenticate through it)
