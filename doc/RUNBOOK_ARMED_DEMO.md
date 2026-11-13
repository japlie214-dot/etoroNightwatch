# Z10 armed DEMO session (T054)

This route places demo orders, never real-account orders. The existing probe and dry
home remains `files/nightwatch_config`; the armed home is `files/nightwatch_demo`.
No DB, lock, or configuration is copied from production. Migrations create a fresh
DB inside the demo home on its first eligible run.

## Build and install

Build from `C:/ClaudeHubFun/hq/repos/etoroNightwatch/android-probe` in PowerShell:

```powershell
$env:JAVA_HOME = 'C:\Users\10102721\Android\jdk-17'
$env:ANDROID_HOME = 'C:\Users\10102721\Android\Sdk'
.\gradlew.bat :app:testDebugUnitTest :app:assembleDebug
```

Online Gradle is approved. Do not open local Gradle/auth/proxy configuration.
The package is `com.nightwatch.etoro`; `.probe` is only the Kotlin namespace.
The debug APK is `android-probe/app/build/outputs/apk/debug/app-debug.apk`.

Only the PM/Boss performs installation, provisioning and device checks. Builders
must not run the following device commands. With USB debugging authorized:

```text
adb install -r android-probe/app/build/outputs/apk/debug/app-debug.apk
```

## Provision the dedicated home (PM/Boss only)

Use the Boss-provided existing demo config and secrets files. Never open, print,
log, recreate or echo their contents. Before transferring, the PM confirms by
variable names only that the secrets file is demo-only; real-account key names
mean stop and ask the Boss. Workers do not inspect or transfer these files.

The demo config must select `etoro.environment = "demo"`, `trading.mode = "live"`,
`strategy.exit_mode = "fixed"`, and configured risk limits
`max_notional_per_session_usd`, `max_daily_loss_usd`, `max_open_lots`, and
`max_orders_per_session`.
Demo credentials use `ETORO_PUBLIC_KEY` and `ETORO_DEMO_PRIVATE_KEY`; the existing
Telegram configuration/credentials must also be provisioned. This document
contains no secret values.

Run from the repository root, replacing the two local-file placeholders:

```text
adb shell run-as com.nightwatch.etoro mkdir -p files/nightwatch_demo/config
adb push <Boss-provided-demo-config-file> /data/local/tmp/nw_cfg.toml
adb push <Boss-provided-demo-secrets-file> /data/local/tmp/nw_sec.env
adb shell run-as com.nightwatch.etoro cp /data/local/tmp/nw_cfg.toml files/nightwatch_demo/config/config.toml
adb shell run-as com.nightwatch.etoro cp /data/local/tmp/nw_sec.env files/nightwatch_demo/config/.secrets.env
adb shell rm /data/local/tmp/nw_cfg.toml /data/local/tmp/nw_sec.env
adb shell run-as com.nightwatch.etoro ls -l files/nightwatch_demo/config/config.toml files/nightwatch_demo/config/.secrets.env
```

Verify existence and byte size only, never content. If run-as cannot read the
shell-staged secrets file, the approved binary-safe, non-echoed fallback is a
shell with input redirection (for example cmd.exe):

```text
adb shell "run-as com.nightwatch.etoro sh -c 'cat > files/nightwatch_demo/config/.secrets.env'" < <Boss-provided-demo-secrets-file>
```

Clean up the staged file afterwards. Do not use a text-reading PowerShell pipeline
for this transfer. Do not copy a production DB into the demo home.

## Open manual demo SCHG positions (T079, operator only)

With Boss approval, keep alarms stopped and recurring DCA off. In the app, enter
1–10 positions and USD 50–15000 per position, then tap **Open demo SCHG positions**.
Review the confirmation's count, USD each and total before accepting. The dedicated
`files/nightwatch_demo` config and gate-7 credential detection must both confirm demo;
real, unknown and mismatched environments refuse before any order submission.
The resolved home must be named `nightwatch_demo`; other homes refuse before config load.
Orders are submitted sequentially, stopping at the first error. The selectable report
shows accepted order IDs only when they are 1–20 ASCII digits; other values are shown
as unavailable, never echoed. Acceptance is not a confirmed fill. Check the demo portfolio
before any retry: an error can leave earlier orders (or the failing order) accepted.
The runner opens no database and writes no instruments, intents or lot annotations;
these positions remain unmanaged until T068 adoption. There is no session, alarm,
Telegram, FCM or intent-extra trigger. A busy session, adoption, probe or manual buy
refuses immediately rather than queueing another buy. A second confirmation dialog
is blocked while one is open or a manual buy is in flight; cancel and tap again only
when idle. Existing sessions/adoption retain their blocking serialization.

Device proof is PM/Boss-only: approve and confirm 5 × approximately USD 11000, observe
five SCHG positions in the demo portfolio, then Plan/review/Apply adoption and verify
gate 11 in the next eligible armed-demo session. Offline tests do not prove fills.

## Adopt existing demo positions (T068, operator only)

Before the first session, stop the broker's recurring SCHG DCA. Keep alarms stopped
and do not run another probe or session while adopting. Provision the dedicated
`files/nightwatch_demo` home above, including its existing Tiingo configuration.
No production DB is copied, and no real-account adoption button is provided.

1. Open the app and tap **Plan adoption (demo)**. The phone fetches its portfolio
   using the read role, resolves instruments, and fetches full SPY history from
   Tiingo. It does not place orders or write lot annotations during planning.
2. Read the on-screen report: symbol counts, tiers, exclusions and any date
   resolutions. A weekend/holiday open date uses the **preceding NYSE trading
   day**, requiring that exact SPY bar; the raw broker entry date is preserved.
   Missing history or any unresolved position refuses the entire adoption.
3. The home stores `adoption_plan.json`: plan ID, UTC creation time, resolved
   absolute home and DB paths, environment, non-secret CID account marker,
   position/database fingerprints, resolved rows and report. Copying a plan into
   another home does not authorize adoption there. All positions must carry the
   same CID. An empty portfolio says **nothing to adopt**, because it cannot
   establish that marker. Planning stages instrument metadata in memory.
4. Within 30 minutes, tap **Apply adoption (demo)** and accept the confirmation
   dialog. The runner validates the home/DB binding before opening the DB. A
   missing or outdated DB is refused without creating or migrating it; initialize
   or migrate separately and plan again. It re-reads the portfolio and SPY history
   and refuses account, environment, position, cache, annotation or resolved-plan
   changes. A refusal requires a new plan; it never silently applies different rows.
5. Confirm **applied** and the adopted count in the report. Only after all checks
   pass, apply takes one write transaction, rechecks the live snapshot, and writes
   missing local instruments plus `lot_annotations` together. An annotation-write
   failure rolls both back. No broker orders are placed. Re-plan and confirm again
   for a safe idempotent re-run: already
   annotated lots are skipped. After an app restart, plan again before applying.
6. PM/QA must observe the demo annotations on-device, then run the next eligible
   Monday-style session and verify gate 11 passes. Offline/JVM tests do not prove
   phone connectivity, adoption of actual demo holdings, or the session gate.

Adoption has no alarm, intent-extra or command trigger and never runs inside a
session. The bridge serializes it with sessions; per-home session locking refuses
an already-running invocation. A real config in the demo home is refused.

## Arm, verify connectivity, and schedule

1. Keep the Z10 charging with Wi-Fi on. Power permits a session only while charging
   or with battery at least 30 percent.
2. Run the existing in-app live probe before the alarm and verify the phone itself
   reaches eToro and Telegram. The PC proxy/DoH/VPN route does not prove the phone's
   route. Probes still use `nightwatch_config`; do not overlap probes with a session
   (concurrent process-global host/environment use is outside T054).
3. Create the device flag; its contents are ignored and must never hold secrets:

   ```text
   adb shell run-as com.nightwatch.etoro touch files/nightwatch_demo/ARM_DEMO
   ```

4. If HALTed, use the existing Telegram `/resume` flow first. It removes HALT from
   both homes and clears the persistent latch; it does not arm or schedule.
5. Press **Schedule market session** in the app. Opening the app alone does not
   schedule. Grant the exact-alarm permission if prompted.
6. Confirm the scheduled timestamp shown or logged is **2026-11-06 20:05 UTC**
   (**15:05 ET**, EST). Today's NYSE close is **21:00 UTC / 16:00 ET**, not 20:00 UTC.
   Future alarms follow the trading calendar, early closes and New York DST.

Entry and market-data work are gated to close-60 through close-25 (today 20:00
through 20:35 UTC). That is not an execution cutoff: before execution the engine
clears the network deadline, and executors submit until close-5 (20:55 UTC today).
T054 changes none of those deadlines or risk gates.

## Observe and stop

- The event log records `SESSION_MODE armed_demo` or `SESSION_MODE dry`, and a
  sanitized `SESSION_RESULT`. Armed-mode notification titles contain **DEMO ARMED**.
- Missing `ARM_DEMO` selects the original dry route. Wrong home, absent flag,
  real-account configuration, or non-live mode refuses without a network call
  from the Python entry. `armed=false` is expected for refusals and skips.
- A same-date row blocks another run: a finished armed row yields `already_ran`;
  every other existing same-date row yields `previous_unfinished`.
- Send Telegram `/halt` to latch HALT in both homes, cancel alarms and show the
  ongoing notification. A Python-created demo HALT is also detected after process
  death and suppresses future alarms. `/resume` clears both files and the latch.
- Remove the arm flag to make **future** invocations dry:

  ```text
  adb shell run-as com.nightwatch.etoro rm files/nightwatch_demo/ARM_DEMO
  ```

  Removing the flag does not stop an already-running session; use `/halt` for that.
- Do not delete session rows or DBs to retry. A crash leaving an unfinished session
  blocks gate 5 on the next session before gate-11 recovery. A human must handle
  it through a reviewed ticket. Gate 11 only recovers terminal-session orders.

Installation, connectivity, alarm delivery, and actual demo execution require
separate device/live-market proof. Offline/JVM success does not prove a trade.
The PC armed-demo acceptance (T039) has not yet passed; this phone run may be the
first full armed-demo session. Demo account only.
