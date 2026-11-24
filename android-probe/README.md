# android-probe — etoroNightwatch minimal proof APK

## T083 credential-free build slice — NOT update-ready

The build no longer imports the four authentication inputs into `BuildConfig`.
`RuntimeCredentials` (in `CommandVerifier.kt`) defines a runtime provider boundary;
no provider is installed. Missing, blank or failed provision reads reject signed
commands, snapshot upload, token registration and Telegram sends before transmission.
The existing HMAC/replay checks and FCM refresh/cron dispatch are unchanged.
There is no fallback to local files, environment variables, embedded credentials or
unsigned-command acceptance. No readiness is implied by compilation or an FCM token.

**Release blocker:** a trusted, restart-safe provisioning adapter must be designed,
wired and verified before this can replace the installed app. A null provider is
not a way to ship an update with disabled unattended demo operation. Do not install
this slice. It does not prove upgrade signing, runtime delivery or device parity.

The v1/v2 sections below are historical evidence, not verification of this slice.

**Status: v2, build order step 4 (DESIGN.md §11), upgrading v1 already installed on the phone.**
This is still the gate-zero artifact whose only job is to prove the Android platform behaves the
way DESIGN.md assumes, on the real device, on battery, overnight — **it is not the real app**, no
eToro client, no strategy, no live trading. v1 already proved the core platform mechanics on the
real device (see "What v1 already proved" below); v2 fixes v1's UX/visibility gaps and adds the
remote-operation and check-battery features the task brief called for next, still without any
trading logic.

Built with the exact known-good toolchain combination verified in
`../doc/RUNBOOK_INFRA.md` Part G.7: **Chaquopy 17.0.0, AGP 8.9.0, Gradle 8.11.1, Kotlin 2.0.21,
JDK 17, compileSdk 36 / targetSdk 36 / minSdk 24, arm64-v8a only.**

Deliberately built under the **real app's package name**, `com.nightwatch.etoro` — not a
throwaway id — because the whole point of this probe is to get exactly the OS treatment (App
Standby bucket, battery policy, hibernation clock) the real app will get later (DESIGN §1.2,
§7.4). Do not install any other app under this package name on the test phone at the same time.

---

## What v1 already proved on the real device (do not regress)

From a real exported log, on the physical iQOO Z10, on battery: the alarm fired **14 ms** after
its scheduled time; network after wake was 316 ms, 1 attempt, HTTP 200, on battery; Chaquopy
cold-wake worked (Python 3.13.9, counter persisted across wakes); tzdata **PASS** in both
Kotlin `java.time` and Python `zoneinfo` (`tzdata_pip_package_importable=True`);
`exactAlarm=true`, `batteryExempt=true`; the FCM token registered to the Worker (HTTP 200); a real
high-priority push was delivered successfully via the Worker. **None of this is re-verified by
this document** — no device was attached while v2 was built (see "What could not be verified" at
the end) — it is recorded here so v2's changes can be judged as additive, not as replacing a
working baseline.

---

## v2 — what changed and why

| Problem (from the task brief) | v1 behaviour | v2 fix |
|---|---|---|
| **1. Silent success** | An alarm firing posted no notification — the user saw nothing and reasonably concluded it failed. | Every significant event now posts a notification: alarm fired, FCM received, command executed, snapshot pushed, and any check FAILING. Two channels: `nightwatch_probe_events` (low importance, routine) and `nightwatch_probe_failures` (default importance, so failures actually surface) — see `NotificationHelper.kt`. |
| **2. UI** | Content collided with the status/nav bars; a raw `HH:mm` text field; no countdown; log was unfiltered, oldest-first, proportional font. | Edge-to-edge insets applied via `ViewCompat.setOnApplyWindowInsetsListener` (top+bottom, `systemBars()+displayCutout()`, `MainActivity.applyEdgeToEdgeInsets()`); `MaterialTimePicker` clock-mode picker; Mon–Sun day toggles for a recurring weekday schedule; a live "fires in Xh Ym Zs" countdown updating every second plus the absolute local next-fire timestamp; event log is newest-first (unchanged — `EventStore.recent()` already ordered `DESC`), monospace (`item_event_log.xml`), and filterable by event type via a `Spinner`; the share/export button is unchanged. |
| **3. Remote operation (the headline feature)** | No inbound command loop; FCM messages just triggered the same fixed wake-check battery. | Full command loop: HMAC verification + replay-counter rejection (`CommandVerifier.kt`, mirrors `cloud/worker/src/commands.js` exactly), command dispatch (`CommandHandler.kt`: `ping`, `status`, `logs`, `runchecks`/`selftest`, `schedule <iso8601>`, plus `refresh`/`run_session`/`halt`/`resume`/`help`), and outbound snapshot push to the Worker's `/snapshot` (`SnapshotPusher.kt`) — automatic on every alarm wake, on boot, on demand, and after every remote command. |
| **4. "All other tests before we connect eToro"** | Only network/Chaquopy/tzdata were checked. | `CheckBattery.kt` runs all twelve checks (eToro reachability, Telegram send from the phone, SQLite+WAL, Python numpy/pandas, Python sqlite3, daily log file, clock skew vs the Worker's `Date` header, App Standby bucket, FCM priority canary made loud in the UI, wake-lock held/released duration, battery delta across wakes, Doze state) on every wake and on demand, logging PASS/FAIL + detail and pushing the results in the snapshot. |

### Design constraint added mid-build: data-only FCM messages — do not reintroduce a `notification` block

While this build was in progress, the coordinator redeployed `cloud/worker/src/fcm.js` after
finding, empirically on the real device, that the Worker's FCM sends carried **both** a
`notification` block and a `data` payload. Per Firebase's documented behaviour, a message
carrying both is rendered by the FCM SDK itself while the app is backgrounded, and
**`onMessageReceived()` is never invoked** in that case. Three pushes were delivered and shown as
system-tray notifications, and v1's event log recorded **zero** `FCM_RECEIVED` entries — the
handler never ran. `cloud/worker/src/fcm.js` now sends **DATA-ONLY** messages (`title`/`body`
folded into `data`), and this app was written/updated to match:

- `NightwatchProbeMessagingService` never reads `remoteMessage.notification` (always null now).
- `title`/`body` are read out of `RemoteMessage.getData()`, defaulting sensibly if absent.
- The app builds and posts its own visible notification from `data` on every message, foreground
  or background — required both for remote commands to work at all and to avoid the 7-day
  silent-send FCM demotion.
- An explicit `FCM_MESSAGE_RECEIVED` event is logged unconditionally, first thing in
  `onMessageReceived`, with priority, the command name, and the full `data` key set — this is the
  exact event whose absence exposed the original bug, so its presence is now unmistakable.

**Do not add a `notification` block back to any FCM send this app is meant to react to — it will
silently break unattended remote operation exactly as it did here.**

### Files added

- `CheckBattery.kt` — the twelve checks (Kotlin-side: 1, 2, 3, 6, 7, 8, 9\*, 10\*, 11, 12; calls into Python for 4 and 5).
- `TelegramClient.kt` — direct `sendMessage` from the phone using `BuildConfig.TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID`.
- `CommandVerifier.kt` — HMAC-SHA256(`"<command>:<counter>"`) verification + persisted replay-counter rejection, mirroring `cloud/worker/src/commands.js`'s `hmacCommand()`.
- `CommandHandler.kt` — dispatches every supported command; includes a minimal `HaltFlag` stand-in for DESIGN §6.I/§5 gate 1.
- `SnapshotPusher.kt` — builds and POSTs the JSON snapshot to `/snapshot`.
- `item_event_log.xml` — monospace log row layout.

### Files changed

- `MainActivity.kt` — insets, time picker, day toggles, countdown, log filter, new buttons (full check battery, Telegram test, push snapshot).
- `NightwatchProbeMessagingService.kt` — routes `data.command` to `CommandVerifier`/`CommandHandler`; tracks the FCM priority canary in `FcmPriorityHolder` for the UI.
- `WakeService.kt` — runs the full check battery and pushes a snapshot on every wake (alarm/FCM/boot); logs wake-lock held duration.
- `BootReceiver.kt` — also triggers a post-boot check battery + snapshot push.
- `PowerStatus.kt` — adds `standbyBucket()` and `isDeviceIdleMode()`.
- `PyBridge.kt` / `probe/checks.py` — adds `numpy_pandas_check()` (260-bar rolling population stdev on synthetic data — the actual operation `cone_5_z` performs, DESIGN §1.4) and `python_sqlite_check()`.
- `NotificationHelper.kt` — two channels (routine/failure) instead of one.
- `AlarmScheduler.kt` — day-of-week-aware nightly scheduling, `scheduleAbsolute()` for the remote `schedule` command, and `currentNextFireEpochMs()` for the UI countdown.
- `app/build.gradle.kts` — `versionCode 1→2`, `versionName "0.1-probe"→"0.2-probe"`; adds `pip { install("numpy"); install("pandas") }`; adds `BuildConfig` fields for `COMMAND_HMAC_SECRET`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`. **Package name, `minSdk`/`compileSdk`/`targetSdk`, ABI filter, and signing config (debug-only, same as v1) are all unchanged**, so v2 installs as an upgrade over v1.
- `local.properties.example` — documents the three new keys.

\* Checks 9 (FCM priority canary) and 10 (wake-lock timing) already existed as log lines in v1; v2 makes 9 loud in the UI (a red warning line + a FAILURE-channel notification on demotion) and formalizes 10 as a numbered `CheckBattery`-style event, though the acquire/release timing itself is logged directly from `WakeService` since it wraps the whole wake, not a single check.

---

## Known gap: the Worker's `/command` allowlist

`cloud/worker/src/commands.js` currently only allows `status`, `positions`, `pnl`, `today`,
`logs`, `next`, `halt`, `resume`, `help` — the DESIGN §8.2 read-only/halt set for the *real* app.
It does **not** yet include `ping`, `runchecks`, `schedule`, or `selftest`, which this task brief
asked for. **`cloud/` was explicitly out of scope for this build** ("Do NOT touch cloud/"), so:

- The phone-side handler (`CommandHandler.kt`) fully implements all six requested commands
  (`ping`, `status`, `logs`, `runchecks`, `schedule <iso8601>`, `selftest`) plus the DESIGN set,
  and is ready the moment the Worker's allowlist is extended.
- Today, sending `ping`, `runchecks`, `schedule`, or `selftest` via `curl … /command` against the
  **live** Worker will be rejected by the Worker itself (`400 command not allowed`) before it ever
  reaches the phone.
- `status`, `logs`, `halt`, `resume`, and `help` work end to end today through the existing
  `/command` endpoint (examples below). `refresh` (a different endpoint, `/refresh`) also works
  today and is a good stand-in for exercising the wake+snapshot path remotely.
- To exercise `ping`/`runchecks`/`schedule`/`selftest` before that Worker change lands, send a raw
  FCM v1 message directly (Firebase console "Compose notification", or a hand-built HTTP v1 call)
  with a `data` payload of `{"command":"runchecks"}` etc. — no `hmac`/`counter` needed for that
  path since `CommandVerifier` is only invoked for messages that already carry both; a command
  arriving with no `hmac` field takes the same "unauthenticated by design" path as `refresh` in
  `NightwatchProbeMessagingService.dispatchCommand()`. This is fine for probe testing; it is not
  how the real app should ship (see DESIGN §8.2's auditability requirement).

---

## Build

Never invoke Gradle in this workspace for a credential-free build: Gradle and its
plugins can automatically read private local/user properties, init scripts,
signing state and caches even when this build script does not request them.

`../tools/credential_free_stage.py` provides the source staging step only. It does
not run Gradle, fetch tooling, infer approvals, scan directories or read runtime
values. Supply an individually reviewed **public-source-only** approval JSON,
containing exact repository-relative paths and SHA-256 hashes of approved bytes:

```json
[{"path":"android-probe/settings.gradle.kts","sha256":"<64 lowercase hex digits of reviewed bytes>"}]
```

Run with an isolated Python interpreter (`-I -B`), `--root` pointing at this
repository, `--approvals` at that approved manifest, and `--destination` at a new
directory inside the session scratchpad, outside the repository. The tool allows
only the three build scripts, explicitly named Kotlin/XML/Python sources, and
`app/src/main/python/nightwatch/db/schema.sql` plus numbered SQL migrations in
that package's `db/migrations/` directory. SQL support preserves package inputs;
it does not approve their contents or permit arbitrary database/runtime files.
It rejects private/configuration/generated paths, path escapes, duplicate names,
links/junctions/hardlinks and hash changes. A hash alone is not a public-data
classification: approval of every input's content remains mandatory.

No real-source approval manifest was supplied or staged in T083. The synthetic
checks run without Gradle or pytest's project fixtures:

```text
python -I -B tests/test_credential_free_stage.py --scratch <absolute existing session scratchpad> -v
```

### Metadata-only preflight — no approvals or contents

```text
python -I -B C:/ClaudeHubFun/hq/repos/etoroNightwatch/tools/credential_free_stage.py --root C:/ClaudeHubFun/hq/repos/etoroNightwatch --inventory
```

This command runs only `git ls-files -z -- android-probe app/src/main/python`
for path discovery. It does not open those paths, inspect user configuration,
calculate hashes, read approvals, stage contents or execute a build. Inventory
mode rejects `--approvals` and `--destination`. Its JSON includes each exact
candidate source path, missing required groups/paths, staging-rule rejections,
required tool versions and separate unsigned/signing/runtime gates. Tracked
presence is not disk verification, a dependency-closure proof or public-content
approval; untracked files and newly added untracked tests are not discovered.

The T083 dry-run found these indexed candidate groups, with **no missing group,
required anchor path or staging-rule rejection** after the SQL-path fix:

| Group | Indexed paths |
|---|---:|
| Build definitions | 3 |
| Android manifest | 1 |
| Android Kotlin | 18 |
| Android XML resources | 6 |
| Probe Python | 14 |
| Nightwatch Python | 49 |
| Nightwatch SQL schema | 1 |
| Nightwatch SQL migrations | 11 |
| Optional indexed Kotlin tests | 8 |

Before unsigned compilation, resolve these exact outstanding inputs:

- Review all listed source contents individually; only then supply an approval
  manifest with hashes. The inventory supplies neither approvals nor hashes.
- Supply approved isolated tools: Gradle **8.11.1**, JDK **17**, Android platform
  **36**, host Python **3.13**, AGP **8.9.0**, Kotlin plugin **2.0.21**, Chaquopy
  **17.0.0**, Google Services plugin **4.4.2**, Firebase BOM **33.5.1**. These are
  required versions, not installed-version attestations. Android build-tools
  are not pinned in the reviewed definitions; approve the AGP-selected version.
- Approve the resolved public Maven and pip dependency inputs from the build
  scripts. `httpx==0.27.2` and `exchange_calendars==4.11.1` are pinned;
  `tzdata`, `numpy` and `pandas` are not. Do not substitute existing caches.
- Independently supply reviewed public Firebase build configuration. The existing
  `android-probe/app/google-services.json` remains unread and excluded. The
  staging tool deliberately does not provision this JSON.
- Approve minimal clean-room AndroidX project configuration; the tracked
  `android-probe/gradle.properties` remains unread and excluded. Do not assume
  its contents or copy it. The staging tool does not provision properties.
- Preserve the `android-probe/` and `app/` sibling layout for the extra Chaquopy
  source set. Approve selection of the isolated Python interpreter instead of
  using the historical absolute `buildPython` path unchanged.

PATH lookup found Java at the documented JDK location but did not run its version;
`kotlinc` and `gradle` were unavailable on PATH. No cache or installation search
was performed, so the Kotlin provider tests were not executed. The isolated
synthetic preflight/staging/source-wiring suite passed **16 tests**.

Unsigned compilation needs no signing credential. Compatible external signing is
a separate authorized operation that must preserve the installed certificate,
package and upgrade version without exporting or inspecting private signing
material. Restart-safe runtime provisioning and Firebase delivery are another
separate release gate. Neither an inventory, an unsigned APK nor a renamed
credential establishes protection or a usable replacement.

**Unsigned APK build remains blocked.** A separately approved isolated Gradle
8.11.1/JDK 17/Android SDK 36/Python 3.13 toolchain and public dependency inputs are
required, with a fresh home, Gradle user home and project cache, no inherited
credential/proxy/pip configuration, and no access to private source/user paths.
The build script still names the historical host `buildPython` path; select an
approved interpreter in the isolated build before execution. Do not reuse the
workspace wrapper, generated files, credential-bearing caches or signing stores.
The Google Services plugin is deliberately retained: an explicitly approved
public Firebase configuration is also required; do not copy or read the existing
`google-services.json`. Removing Firebase just to compile would not preserve
unattended delivery. Build only an unsigned release in that approved isolation;
never use debug signing or claim install compatibility from an unsigned artifact.
No Gradle execution, APK, toolchain isolation or Firebase provisioning is verified
by the staging tests.

### Actual v2 build result (this run)

```
BUILD SUCCESSFUL in 1m 46s
49 actionable tasks: 26 executed, 23 up-to-date
```

`aapt2 dump badging app/build/outputs/apk/debug/app-debug.apk` confirms:

```
package: name='com.nightwatch.etoro' versionCode='2' versionName='0.2-probe'
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

Same package, same `minSdk`/`compileSdk`/`targetSdk`, same permission set, same (debug) signing
config as v1 — **only `versionCode` (1→2) and `versionName` (0.1-probe→0.2-probe) changed**, so
`adb install -r` upgrades cleanly over v1 without a data wipe.

```
-rw-r--r-- 1 10102721 1049089 56660409 <date> app-debug.apk    # ≈ 54.0 MiB / 56.66 MB
```

Confirmed **arm64-v8a only**: unzipping the APK shows a single `lib/arm64-v8a/` directory (no
other ABI). numpy/pandas are packaged inside Chaquopy's `assets/chaquopy/requirements-common.imy`
asset (Chaquopy bundles pip packages there rather than under `lib/`, regardless of the per-ABI
filename) — confirmed by listing that archive's contents: **63 `.so` files** matching
`numpy`/`pandas` (e.g. `numpy/core/_multiarray_umath.so`, `numpy/linalg/lapack_lite.so`,
`pandas/_libs/*.so`), not just "pip resolved them without erroring". The requirements asset itself
is ≈30 MB (`requirements-common.imy`, 30,160,201 bytes), matching the expected size growth from
the v1 baseline (≈26.75 MB) to v2 (≈56.6 MB) almost exactly.

**Not verified, stated plainly:** no device was attached to this build machine while v2 was built.
Everything runtime-related below is unverified on the physical phone until this APK is actually
installed and run — this README does not claim otherwise.

---

## Install (upgrade over v1)

```bash
adb devices          # confirm the iQOO Z10 shows up, USB debugging authorized
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

`-r` reinstalls, keeping app data (the event log DB, SharedPreferences schedule state, etc.) —
this only works because v2 keeps the same package name, same signing config (debug keystore,
unchanged from v1), and a higher `versionCode`. If `adb install -r` ever reports
`INSTALL_FAILED_UPDATE_INCOMPATIBLE` or a signature mismatch, it means the debug keystore used to
sign this build differs from the one that signed the installed v1 — check that the same machine
and `~/.android/debug.keystore` were used for both builds.

---

## OriginOS 6 settings — apply BEFORE any overnight test (unchanged from v1)

**None of this is vivo documentation** (DESIGN §7.4) — community-reported, verify on-device, and
re-verify after every OS update. Using the exact Indonesian labels reported on this device:

1. **Settings → Apps → Nightwatch Probe → Battery usage → Izinkan penggunaan daya di latar
   belakang** ("Allow background power usage") — **ON**. Without it the app has no network once
   it decays into the Rare/Restricted App Standby bucket (DESIGN §1.2) — this is now directly
   observable in-app via the "Standby bucket" line and check `STANDBY_BUCKET`.
2. **Settings → Apps → Nightwatch Probe → Battery usage → Jeda aktivitas aplikasi jika tidak
   digunakan** ("Pause app activity if unused") — **OFF**. OriginOS's own hibernation analogue;
   strips exactly the two permissions this probe depends on.
3. **Settings → Apps → Nightwatch Probe → Battery usage → Mulai otomatis** ("Autostart") — **ON**.
   Without it, `BootReceiver` silently fails to run at all after a reboot.
4. **Settings → Apps → Special app access → Alarm & pengingat** (Alarms & reminders) →
   Nightwatch Probe → **Allow**. Also grantable from inside the app via "Grant: Schedule exact
   alarms".
5. Tap **"Grant: Ignore battery optimizations"** in-app and accept the dialog — mandatory, not
   optional (DESIGN §1.2).
6. Tap **"Grant: Notifications"** — required for every visible notification v2 now posts, and
   for preventing FCM's 7-day silent-send demotion.
7. Long-press the app's card in Recents and look for a lock/pin icon (no confirmed OriginOS 6
   settings-menu equivalent — verify manually).

The in-app status line shows live `canScheduleExactAlarms()`, battery-optimization-exemption
state, App Standby bucket, and Doze state, refreshed on every resume.

---

## Using the app (v2)

- **Permission buttons** — unchanged from v1.
- **"Schedule test alarm"** — fires `setAlarmClock()` N minutes from now (default 2). Use first,
  screen on, before anything overnight.
- **"Pick time (analog clock)"** — opens a `MaterialTimePicker` in clock mode; sets the picked
  `HH:mm` shown above the day toggles.
- **Mon–Sun day toggles** — choose which weekdays the nightly alarm recurs on (defaults to
  Mon–Fri, matching the real app's weekday-only trading schedule).
- **"Schedule nightly alarm (picked time + checked days)"** — schedules the next matching
  occurrence; reschedules itself after every fire and from `BootReceiver`, always re-resolving
  against the current picked time/days.
- **"Next fire" / "Fires in ..."** — an absolute local timestamp and a live countdown, updating
  every second while the screen is on.
- **"Run FULL check battery now (selftest)"** — runs all twelve checks immediately, logs each
  with PASS/FAIL, posts a failure notification for anything that failed, and pushes a snapshot.
- **"Send Telegram test message now"** — sends one real message from the phone's own connection.
- **"Push snapshot to Worker now"** — POSTs the current device status + recent events + latest
  check results to `/snapshot` on demand.
- **FCM token** — shown once fetched, POSTed to the Worker's `/device-token`.
- **FCM priority canary** — shown in red if the last received message was not `PRIORITY_HIGH`.
- **Event log** — newest-first, monospace, filterable by event type via the dropdown above the
  list. **"Share / export log"** unchanged from v1.

---

## Remote-command reference (Problem 3 — the mini-server proof)

All commands go through the Worker already deployed at
`https://etoro-nightwatch-worker.japlie214.workers.dev` (`cloud/worker/src/index.js`, not
modified by this build). Replace `<WEB_READ_TOKEN>` with the value in
`../secrets/web_read_token.txt`.

### Read the phone's last-pushed snapshot (no phone interaction needed — reads the Worker's cache)

```bash
curl -s https://etoro-nightwatch-worker.japlie214.workers.dev/snapshot \
  -H "x-web-read-token: <WEB_READ_TOKEN>" | jq .
```

Returns `{"snapshot": {...device status, recent_events, latest_checks...}, "updated_at": "..."}`
— `snapshot: null` if the phone has never pushed one yet.

### Ask the phone for a fresh snapshot right now (works today, unmodified Worker)

```bash
curl -s -X POST https://etoro-nightwatch-worker.japlie214.workers.dev/refresh \
  -H "x-web-read-token: <WEB_READ_TOKEN>"
```

Wakes the phone via high-priority FCM; `CommandHandler.handle(..., "refresh", ...)` pushes a
fresh snapshot within seconds. Poll `GET /snapshot` again afterwards to see it land.

### Commands that work today through the Worker's existing allowlist

```bash
# status: posts a notification with power/host state and pushes a snapshot
curl -s -X POST https://etoro-nightwatch-worker.japlie214.workers.dev/command \
  -H "x-web-read-token: <WEB_READ_TOKEN>" -H "content-type: application/json" \
  -d '{"command":"status"}'

# logs: pushes the recent event log to the Worker's snapshot
curl -s -X POST https://etoro-nightwatch-worker.japlie214.workers.dev/command \
  -H "x-web-read-token: <WEB_READ_TOKEN>" -H "content-type: application/json" \
  -d '{"command":"logs"}'

# halt / resume: sets/clears the local HaltFlag stand-in
curl -s -X POST https://etoro-nightwatch-worker.japlie214.workers.dev/command \
  -H "x-web-read-token: <WEB_READ_TOKEN>" -H "content-type: application/json" \
  -d '{"command":"halt"}'

curl -s -X POST https://etoro-nightwatch-worker.japlie214.workers.dev/command \
  -H "x-web-read-token: <WEB_READ_TOKEN>" -H "content-type: application/json" \
  -d '{"command":"resume"}'

# help
curl -s -X POST https://etoro-nightwatch-worker.japlie214.workers.dev/command \
  -H "x-web-read-token: <WEB_READ_TOKEN>" -H "content-type: application/json" \
  -d '{"command":"help"}'
```

Each of these gets an HMAC + monotonic counter attached by the Worker
(`cloud/worker/src/commands.js`'s `hmacCommand()`), verified on the phone by
`CommandVerifier.verify()` before `CommandHandler` runs it; every accepted or rejected attempt is
logged as `COMMAND_RECEIVED`/`COMMAND_REJECTED` in the event log (and pushed in the next
snapshot).

### Commands implemented on the phone but currently blocked by the Worker's allowlist

See "Known gap" above. `ping`, `runchecks`, `schedule <iso8601>`, and `selftest` are fully wired
in `CommandHandler.kt` and will work the moment `cloud/worker/src/commands.js`'s
`ALLOWED_COMMANDS` includes them — this was not done because `cloud/` was out of scope for this
build:

```bash
# NOT YET WORKING against the live Worker (400 command not allowed) — shown for when it is extended:
curl -s -X POST https://etoro-nightwatch-worker.japlie214.workers.dev/command \
  -H "x-web-read-token: <WEB_READ_TOKEN>" -H "content-type: application/json" \
  -d '{"command":"selftest"}'

curl -s -X POST https://etoro-nightwatch-worker.japlie214.workers.dev/command \
  -H "x-web-read-token: <WEB_READ_TOKEN>" -H "content-type: application/json" \
  -d '{"command":"runchecks"}'

curl -s -X POST https://etoro-nightwatch-worker.japlie214.workers.dev/command \
  -H "x-web-read-token: <WEB_READ_TOKEN>" -H "content-type: application/json" \
  -d '{"command":"ping"}'

# schedule expects an ISO-8601 instant carried through as data.arg on the Worker side —
# also blocked by the same allowlist gap today.
curl -s -X POST https://etoro-nightwatch-worker.japlie214.workers.dev/command \
  -H "x-web-read-token: <WEB_READ_TOKEN>" -H "content-type: application/json" \
  -d '{"command":"schedule","arg":"2026-09-10T19:00:00Z"}'
```

### Redundant daily cron trigger (already deployed, unmodified)

`cloud/worker/src/index.js`'s `scheduled()` handler fires `run_session` automatically around the
US-close-minus-30-minutes window; `CommandHandler` runs the full check battery and pushes a
snapshot in response (this probe has no trading session to run, so it stands in with the check
battery so the trigger is still visibly proven end to end).

---

## The multi-night test protocol — be concrete about pass/fail (updated for v2)

This is still the actual gate (DESIGN §11 step 4). v1 already passed a subset of this on the real
device (see "What v1 already proved"); the protocol below should be re-run in full after
installing v2, since v2 changes the wake path (full check battery + snapshot push on every wake)
and this has never run on the phone.

### Setup (once)

1. Install v2 with `adb install -r` (above), re-apply/confirm all 6 OriginOS settings, confirm
   the in-app status line shows `exactAlarm=true batteryExempt=true`.
2. Confirm the FCM token appears in the UI.
3. Run **"Run FULL check battery now (selftest)"** with the screen on. Expect all twelve checks to
   log a result; `ETORO_REACHABILITY` should PASS even without credentials (401/403 counts as
   pass — see the check's own detail text); `TELEGRAM_SEND` should actually deliver a message to
   the configured chat; `SQLITE_WAL`, `NUMPY_PANDAS`, `PYTHON_SQLITE`, `DAILY_LOG_FILE` should all
   PASS; `CLOCK_ACCURACY` should show a skew comfortably under the 60,000 ms gate threshold;
   `STANDBY_BUCKET` should show `ACTIVE` (the app was just opened).
4. Confirm **"Push snapshot to Worker now"** logs `SNAPSHOT_PUSHED`, then confirm
   `GET /snapshot` (curl example above) returns that data back.

### Each night (repeat for 3+ consecutive nights)

1. Pick a time via the clock picker at least 3 hours away, confirm Mon–Fri (or your chosen days)
   are checked, tap "Schedule nightly alarm".
2. **Unplug the phone.** Screen off. Do not touch it until after the scheduled time.
3. Next morning: open the app (or `GET /snapshot`, or "Share / export log") and check for that
   night's `WAKE_START` → ... → `WAKE_END` pair, all twelve `CHECK_*` entries, and
   `SNAPSHOT_PUSHED`.

**Pass, for one night:** `ALARM_FIRED` within a minute or two of the scheduled time;
`NETWORK_CHECK success=true` with a recorded `latency_ms` (write this down — it is the empirical
basis for DESIGN §4.1's 5-minute margin); all twelve `CHECK_*` entries present with no unexpected
`pass=false`; `WAKE_LOCK_ACQUIRED`/`WAKE_LOCK_RELEASED` both present with a sane `held_ms`;
`SNAPSHOT_PUSHED` present; `WAKE_RESCHEDULE` shows a next-alarm timestamp.

**Fail, for one night:** no `WAKE_START` at all (the vivo background-killing risk, DESIGN §12);
`WAKE_START` present but `WAKE_END` missing (killed mid-run); `NETWORK_CHECK success=false` after
the full 90s budget; any check that should structurally always pass (`SQLITE_WAL`,
`PYTHON_SQLITE`, `DAILY_LOG_FILE`) reporting `pass=false`.

### The reboot night, and the week-long FCM demotion check

Unchanged from v1's protocol (below) — reboot once, confirm `BOOT_COMPLETED` →
`BOOT_RESCHEDULE_RESULT` → a boot-triggered check battery + snapshot push, all without opening the
app; and confirm `priority=PRIORITY_HIGH` still appears after 7+ days of daily pushes. v2 adds:
the FCM priority canary is now also visible in red in the UI and triggers a FAILURE-channel
notification immediately on demotion, rather than only being discoverable in the log.

### Overall pass/fail for step 4

Unchanged: **Pass** requires 3+ consecutive unattended overnight cycles with `WAKE_START`/
`WAKE_END` pairs, successful network checks, and all twelve checks passing structurally; the
reboot night working; `PRIORITY_HIGH` still observed after a week. Any single silent night is
grounds to treat vivo's background-killing risk as materialized (DESIGN §12).

---

## tzdata — unchanged from v1

See the v1 section retained below for the full Kotlin/`java.time` vs Python/`zoneinfo` rationale.
v2 did not touch tzdata handling; `TzCheck.kt` and `probe/checks.py`'s `tz_check()` are unmodified.

---

## What was stubbed / could not be verified (v2)

- **No device was attached during this v2 build.** Every runtime claim in this README beyond the
  build/packaging verification above (alarm firing, Doze network behavior, FCM command handling,
  Telegram delivery, numpy/pandas actually running, clock skew, standby bucket transitions, boot
  survival with the new snapshot push) is **unverified** until this APK is installed and run on
  the physical iQOO Z10.
- **The Worker's `/command` allowlist gap** (above) — `ping`/`runchecks`/`schedule`/`selftest`
  are phone-ready but not reachable through the live Worker's `/command` endpoint until
  `cloud/worker/src/commands.js` is extended, which was out of scope for this build.
- **`git check-ignore` was not run**, per this task's explicit instruction not to run git
  commands. Instead, `../.gitignore` was inspected directly and confirmed to contain
  `android-probe/local.properties`, `android-probe/*/local.properties`, and (top-level, so it
  matches at any path) `google-services.json`. A script-based scan of every file under
  `android-probe/` (excluding `local.properties` itself and build output) confirmed none of the
  four real secret values (`PHONE_PUSH_SECRET`, `COMMAND_HMAC_SECRET`, `TELEGRAM_BOT_TOKEN`,
  `TELEGRAM_CHAT_ID`) appear anywhere else.
- **Signing is still debug-only**, unchanged from v1 — this remains a sideloaded proof artifact,
  not a release build.

## Demo control (Refresh / Pause) device bridge

FCM data message: `command=demo_control`, `envelope` (compact JSON: `version:1, requestId, action,
deviceId, environment:"demo", issuedAt, deadlineAt = issuedAt + 120000`), `hmac`. The `hmac` is
lowercase-hex HMAC-SHA256, keyed with the runtime `COMMAND_HMAC_SECRET`, over these UTF-8 lines, each
ending in LF:

```
nightwatch.demo-control.dispatch.v1
version=1
requestId=<uuid>
action=refresh_demo_broker|pause_demo_orders
deviceId=<id>
environment=demo
issuedAt=<ms>
deadlineAt=<ms>
```

Only after that check does `PyBridge.applyDemoControl` pass write authority to Python. A signed
receipt (`POST /demo-control/ack`, RS256 bytes per `cloud/worker/README.md`) is produced only when
Python returns `status: "applied"` with complete matching evidence. **Refresh is never receipted
while Python returns `partial`** (open-order coverage unproved, `observedAt` null), so the cloud keeps
showing Requested. No device identity/signing key is provisioned yet: until
`DemoControlIdentity.identity` and the runtime secret exist, nothing is applied or acknowledged.
Not installed or exercised on Z10.
