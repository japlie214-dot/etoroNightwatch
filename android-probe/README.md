# android-probe — etoroNightwatch minimal proof APK

**Status: build order step 4 (DESIGN.md §11).** This is the gate-zero artifact whose only job is
to prove the Android platform behaves the way DESIGN.md assumes, on the real device, on battery,
overnight. **It is not the real app** — no eToro client, no strategy, no trading. If this gate
fails, DESIGN.md's own words apply: "Do not build steps 3–12 on an unproven platform."

Built with the exact known-good toolchain combination verified in
`../doc/RUNBOOK_INFRA.md` Part G.7: **Chaquopy 17.0.0, AGP 8.9.0, Gradle 8.11.1, Kotlin 2.0.21,
JDK 17, compileSdk 36 / targetSdk 36 / minSdk 24, arm64-v8a only.**

Deliberately built under the **real app's package name**, `com.nightwatch.etoro` — not a
throwaway id — because the whole point of this probe is to get exactly the OS treatment (App
Standby bucket, battery policy, hibernation clock) the real app will get later (DESIGN §1.2,
§7.4). Do not install any other app under this package name on the test phone at the same time.

---

## What it proves (and does not)

| In scope | Out of scope |
|---|---|
| `setAlarmClock()` fires on time, on battery, in Doze | The trading strategy, eToro client, Telegram bot |
| FCM high-priority delivery + the 7-day demotion canary | Real portfolio data of any kind |
| Network reachability after a cold wake (Doze Wi-Fi lazy re-assoc) | Production error handling, retries beyond a bounded backoff |
| Boot survival | Anything requiring `NIGHTWATCH_ARM` / live trading |
| Chaquopy embedded CPython actually running after a cold wake | numpy/pandas (proven separately, §RUNBOOK_INFRA Part G.7 — 30 MB is pointless here) |
| `America/New_York` tzdata resolution, both Kotlin/`java.time` and Python/`zoneinfo` | — |

---

## Build

From this directory (`android-probe/`), with the environment set explicitly — **this session's
processes hold a stale environment, so set these every time, do not rely on inherited state**:

```bash
export JAVA_HOME="C:\Users\10102721\Android\jdk-17"
export ANDROID_HOME="C:\Users\10102721\Android\Sdk"
export PATH="$JAVA_HOME/bin:$PATH"

./gradlew assembleDebug --stacktrace
```

**Before the first build**, copy `local.properties.example` to `local.properties` and fill in:
- `sdk.dir` — your Android SDK path.
- `PHONE_PUSH_SECRET` — the value in `../secrets/phone_push_secret.txt`. This is injected into
  `BuildConfig.PHONE_PUSH_SECRET` at build time (see `app/build.gradle.kts`); it is never
  hardcoded into a committed `.kt` file, and `local.properties` is gitignored.

`app/google-services.json` must be present (already copied here from
`../app/google-services.json`, project `etoronightwatch`, app id
`1:932131099981:android:2b2f984544e74163b81304`, package `com.nightwatch.etoro`) — also
gitignored, never committed.

### Actual build result (this run)

```
BUILD SUCCESSFUL in 2m 19s
49 actionable tasks: 49 executed
```

No `compileall`/path-length failure occurred — the repo path
(`C:\Users\10102721\Documents\ClaudeFiles\etoroNightwatch\android-probe`, 70 characters) stayed
well under the Windows `MAX_PATH` issue that hit the throwaway `chaquopy-probe` build in a
deeply-nested temp scratchpad (RUNBOOK_INFRA §G.7). If this ever recurs on a different machine or
a longer path, `subst X: <path>` and build from the substituted drive, then remove the mapping —
no admin rights required.

`aapt2 dump badging app/build/outputs/apk/debug/app-debug.apk` confirms:

```
package: name='com.nightwatch.etoro' versionCode='1' versionName='0.1-probe'
compileSdkVersion='36' compileSdkVersionCodename='16'
minSdkVersion:'24'
targetSdkVersion:'36'
uses-permission: name='android.permission.SCHEDULE_EXACT_ALARM'
uses-permission: name='android.permission.RECEIVE_BOOT_COMPLETED'
uses-permission: name='android.permission.WAKE_LOCK'
uses-permission: name='android.permission.INTERNET'
uses-permission: name='android.permission.ACCESS_NETWORK_STATE'
uses-permission: name='android.permission.POST_NOTIFICATIONS'
uses-permission: name='android.permission.FOREGROUND_SERVICE'
uses-permission: name='android.permission.FOREGROUND_SERVICE_DATA_SYNC'
uses-permission: name='android.permission.REQUEST_IGNORE_BATTERY_OPTIMIZATIONS'
```

Unzipping the APK confirms native libraries are packaged for **arm64-v8a only** — no other ABI
present.

```
ls -la app/build/outputs/apk/debug/app-debug.apk
-rw-r--r-- 1 <you> <group> 26750398 <date> app-debug.apk    # ≈ 25.5 MiB / 26.75 MB
```

This is larger than the baseline `chaquopy-probe` (≈22.4 MB) mainly because of Firebase
Messaging + the `tzdata` pip package + Kotlin/AndroidX/OkHttp dependencies this probe adds on top
of a bare Chaquopy app — still nowhere near the +30 MB numpy/pandas would have added.

**Not verified, stated plainly:** no device is attached to this build machine as of this build.
Everything below is unverified until run on the physical phone — this README cannot and does not
claim otherwise.

---

## Install

```bash
adb devices          # confirm the iQOO Z10 shows up, USB debugging authorized
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

---

## OriginOS 6 settings — apply BEFORE any overnight test

**None of this is vivo documentation** (DESIGN §7.4) — community-reported, verify on-device, and
re-verify after every OS update. Using the exact Indonesian labels you reported seeing on this
device:

1. **Settings → Apps → Nightwatch Probe → Battery usage → Izinkan penggunaan daya di latar
   belakang** ("Allow background power usage") — set this ON. This is the OriginOS background
   power tri-state; without it the app has no network once it decays into the Rare/Restricted App
   Standby bucket (DESIGN §1.2).
2. **Settings → Apps → Nightwatch Probe → Battery usage → Jeda aktivitas aplikasi jika tidak
   digunakan** ("Pause app activity if unused") — set this **OFF**. This is OriginOS's own
   hibernation analogue, layered on top of stock Android App Hibernation, and it strips exactly
   the two permissions this probe (and the real app) depend on.
3. **Settings → Apps → Nightwatch Probe → Battery usage → Mulai otomatis** ("Autostart") — set
   this **ON**. Without it, `BootReceiver` will silently fail to run at all after a reboot — the
   failure mode that looks exactly like "nothing happened."
4. **Settings → Apps → Special app access → Alarm & pengingat** (Alarms & reminders) →
   Nightwatch Probe → **Allow**. Also grantable from inside the app via the "Grant: Schedule
   exact alarms" button, which opens this same settings screen.
5. In the app itself, tap **"Grant: Ignore battery optimizations"** and accept the system dialog.
   This is the exemption DESIGN §1.2 calls "mandatory... not an optimisation" — without it the
   Rare/Restricted App Standby buckets disable network entirely.
6. Tap **"Grant: Notifications"** (Android 13+ runtime permission) — required for the visible
   notification that both surfaces events to you and prevents FCM's 7-day silent-send demotion.
7. Long-press the app's card in Recents and look for a lock/pin icon (no confirmed OriginOS 6
   settings-menu equivalent was found — DESIGN §7.4 item 4 — verify manually on this device).

The in-app status line (top of the screen) shows live `canScheduleExactAlarms()` and
battery-optimization-exemption state, refreshed on every resume — use it to confirm each toggle
actually took effect rather than trusting the Settings UI alone.

---

## Using the app

- **"Schedule test alarm"** — fires `setAlarmClock()` N minutes from now (default 2). Use this
  first, with the screen on, to confirm the basic wiring works before trying anything on battery
  overnight.
- **"Schedule nightly alarm"** — schedules the next occurrence of a device-local `HH:mm` (e.g.
  `02:30`), matching DESIGN §1.3's Jakarta 02:30/03:30 WIB alternation. Reschedules itself after
  every fire and from `BootReceiver`.
- **"Run wake checks now"** — manually triggers the same network + Chaquopy + tzdata checks an
  alarm or FCM wake would run, without waiting.
- **FCM token** — shown once fetched, and POSTed to the Worker's `/device-token` endpoint
  automatically (`https://etoro-nightwatch-worker.japlie214.workers.dev/device-token`, header
  `x-phone-push-secret`). `cloud/worker/src/index.js` already implements this route
  (`POST /device-token`, authenticated by `PHONE_PUSH_SECRET`) — the exact same secret value in
  `../secrets/phone_push_secret.txt` / `local.properties` must match what's set as the Worker's
  `PHONE_PUSH_SECRET` via `wrangler secret put` (RUNBOOK_INFRA §B.4), or this POST will fail
  auth. This was confirmed by reading the Worker source, not by an actual on-device POST — no
  phone has run this probe yet.
- **Event log** — every event (alarm scheduled/fired, permission changes, FCM received with
  priority, network check latency, boot re-register, tzdata results) is persisted to a local
  SQLite database (`nightwatch_probe_events.db`, app-private storage) and shown in the scrollable
  list. **"Share / export log"** writes the full log to a text file and opens the Android share
  sheet — send it to yourself however is convenient (email, Telegram, etc.) the next morning.

---

## The multi-night test protocol — be concrete about pass/fail

This is the actual gate (DESIGN §11 step 4). A build succeeding proves nothing about the vivo
background-killing risk (DESIGN §12's top risk) — only the phone, unplugged, overnight, proves
that.

### Setup (once)

1. Install the APK, apply all 6 settings above, confirm the in-app status line shows
   `exactAlarm=true batteryExempt=true`.
2. Confirm the FCM token appears in the UI (proves Google Play Services delivered a registration
   token at all).
3. Run "Schedule test alarm" for 2 minutes with the screen on and confirm: the alarm fires, a
   notification appears, the event log shows `WAKE_START` → `NETWORK_CHECK` →
   `CHAQUOPY_SMOKE` → `TZ_CHECK_KOTLIN` → `TZ_CHECK_PYTHON` → `WAKE_END`, in that order, with no
   `_FAILED` entries other than the expected `FCM_TOKEN_POST` (endpoint not built yet, see
   above).

### Each night (repeat for 3+ consecutive nights)

1. Schedule the nightly alarm for a time at least 3 hours away.
2. **Unplug the phone.** Turn the screen off. Do not touch the phone until after the alarm should
   have fired.
3. The next morning, open the app (or pull the exported log) and check the event log for that
   night's `WAKE_START`/`WAKE_END` pair.

**Pass, for one night:**
- `ALARM_FIRED` appears within a minute or two of the scheduled time (the alarm-clock guarantee,
  not "eventually, in a maintenance window").
- `NETWORK_CHECK` shows `success=true` with a recorded `latency_ms` — **write this number down
  every night**; DESIGN §4.1 budgets 5 minutes of slack partly on the assumption that Wi-Fi
  re-association after Doze is fast, and this is the only way to check that assumption against
  reality on this specific phone.
- `CHAQUOPY_SMOKE` shows a result string with no `_FAILED` suffix.
- `TZ_CHECK_KOTLIN` and `TZ_CHECK_PYTHON` both show `PASS` (see tzdata section below).
- `WAKE_RESCHEDULE` shows a next-alarm timestamp — proves the app can re-arm itself unattended.

**Fail, for one night:** no `WAKE_START` at all (alarm silently swallowed — the vivo risk
DESIGN §12 names explicitly), or `WAKE_START` present but the process was killed mid-run
(`WAKE_END` missing), or `NETWORK_CHECK success=false` after the full 90s budget (Doze network
denial actually happening despite the exemption).

### The reboot night (once, in addition to the 3+ consecutive nights)

1. With the nightly alarm scheduled for several hours out, reboot the phone (not just
   screen-off — an actual restart).
2. Do not open the app after reboot.
3. Confirm the event log shows `BOOT_COMPLETED` → `BOOT_RESCHEDULE_RESULT` (rescheduled) shortly
   after boot, **without you having opened the app** — and that the nightly alarm still fires
   afterwards. If autostart (`Mulai otomatis`) was left off, expect this to fail silently; that is
   exactly the failure DESIGN §7.5 warns about.

### The week-long FCM demotion check

Google demotes an app's high-priority FCM privilege after ~7 days of silent sends with no visible
notification (DESIGN §8.2). This probe already posts a visible notification on every FCM receive,
which is the mitigation — but confirm it actually worked:

1. Send at least one FCM message a day to the device's registered token for 7+ days (via the
   Firebase console's "Compose notification" test tool, or once the Worker's send path exists).
2. Each day, check the event log's `FCM_RECEIVED` entries.
3. **Pass:** `priority=PRIORITY_HIGH` still appears on day 7+ messages.
4. **Fail:** `priority=PRIORITY_NORMAL` appears on a message sent as high-priority — this is the
   demotion happening, and it means the visible-notification mitigation needs re-examination
   before the real app is built.

### Overall pass/fail for step 4

**Pass** requires all of: 3+ consecutive unattended overnight cycles with `WAKE_START`/`WAKE_END`
pairs and successful network checks; the reboot night working; and `PRIORITY_HIGH` still observed
after a week. **Any single silent night (`ALARM_FIRED` missing) is grounds to treat vivo's
background-killing risk as materialized**, per DESIGN §12 — "there is no way to make this risk go
away, only to detect it fast," and detecting it fast is exactly what this protocol does.

---

## tzdata — what this probe checks and why

DESIGN §1.2/§1.3: Android 10+ ships tz rules as a Play-updatable APEX module, and the test
device's Google Play system update is reported ~10 months stale. Two independent runtimes are
checked, because they source tzdata differently:

- **Kotlin/`java.time`** (`TzCheck.kt`) — resolves `ZoneId.of("America/New_York")` against
  Android's own system/APEX tzdata, computing the UTC offset for a known EST date
  (2026-01-15 → expect `-05:00`) and a known EDT date (2026-07-15 → expect `-04:00`).
- **Python/`zoneinfo`** (`app/src/main/python/probe/checks.py`, called via `PyBridge.kt`) — same
  two dates, same expected offsets, but through Chaquopy's embedded CPython. Because Python's
  `zoneinfo` may not find Android's system tzdata through Chaquopy's embedded interpreter the way
  it does on desktop Linux, **this probe bundles the pure-Python `tzdata` PyPI package
  unconditionally** (`chaquopy { defaultConfig { pip { install("tzdata") } } }` in
  `app/build.gradle.kts`) as the safety net the task brief called for, rather than waiting to
  discover the failure on-device and patch it in later. The build log confirms it resolved and
  installed cleanly:

  ```
  Collecting tzdata
    Using cached tzdata-2026.3-py2.py3-none-any.whl (348 kB)
  Successfully installed tzdata-2026.3
  ```

  `checks.py`'s `tz_check()` also reports whether the `tzdata` package was importable at
  call time, so the on-device result distinguishes "resolved via bundled tzdata" from "resolved
  via Android's own APEX tzdata" if that distinction ever matters for debugging.

**What could not be verified at build time:** whether either path actually needed the fallback on
this specific device, and whether both report `PASS` at runtime. That can only be seen in the
app's UI (`txtTz`) and the `TZ_CHECK_KOTLIN`/`TZ_CHECK_PYTHON` event log entries once the APK
actually runs on the phone. If either ever reports `FAIL` or `MISMATCH` there, treat it exactly
as DESIGN's task brief warns: "this is exactly the kind of thing that silently produces a wrong
trade time" — do not proceed to build order step 5+ until it's resolved.

---

## What was stubbed / could not be verified

- **No device was attached during this build.** Every runtime claim above (alarm firing,
  Doze network behavior, FCM delivery, boot survival, tzdata resolution) is **unverified** until
  run on the physical iQOO Z10. This README describes the protocol to verify them; it does not
  claim they already have been.
- **The Worker's `/device-token` and `/health` routes exist in `cloud/worker/src/index.js`** —
  confirmed by reading the source, not by an on-device call (no phone attached). Whether the
  deployed Worker's `PHONE_PUSH_SECRET` matches the value baked into this build's
  `local.properties` has not been checked live.
- **The 5th OriginOS checklist item (lock in Recents)** has no confirmed settings-menu path for
  OriginOS 6 (DESIGN §7.4 item 4) — must be located manually via long-press in Recents.
- **numpy/pandas are deliberately not included** — proven separately in the throwaway
  `chaquopy-probe` build (RUNBOOK_INFRA §G.7); including them here would add ~30 MB for no reason
  this probe needs.
- **Signing is debug-only.** This is a sideloaded proof artifact, not a release build; no keystore
  was created or is needed for this step.
