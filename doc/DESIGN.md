# etoroNightwatch — Live eToro Trading & Portfolio App

**Status:** design, pending approval. Nothing built yet.
**Date:** 2026-09-02
**Host:** vivo iQOO Z10, Android 16 / OriginOS 6, Snapdragon 7 Gen 3 (arm64-v8a only), 12 GB RAM,
Jakarta (UTC+7). Native Android app with an
embedded CPython runtime via **Chaquopy**. Often on battery, not permanently charged. Doubles
as a small always-reachable personal server, contacted over Telegram.

etoroNightwatch wakes the phone in the Jakarta small hours, verifies it is safe to trade, runs
the Std Band Ladder Cash Recycle strategy against live US market prices **thirty minutes before
the close**, executes on eToro, reports to Telegram, and lets the device fall back into Doze.
The real eToro account holds other assets besides UPRO and SCHG; etoroNightwatch **displays
everything but manages only the managed set** (§1.4). A static web app (Cloudflare Pages,
reading a snapshot pushed by the phone to a Cloudflare Worker, §9) is the primary way to check
the portfolio from anywhere; an embedded FastAPI server remains as an optional LAN-only debug
view (§9.5).

The phone is woken by **three independent paths** — an on-device `setAlarmClock()`, a
cloud-scheduled FCM high-priority push, and a manual command — converging on **one idempotent
session** (§7.2).

---

## 1. Ground truth established before design

Three research passes produced the facts this design rests on. Where a fact contradicts the
original brief it is called out.

### 1.1 eToro API (verified against https://api-portal.etoro.com)

| Fact | Consequence |
|---|---|
| Auth is **two static headers**, `x-api-key` (Public API Key) + `x-user-key` (User Key), plus a per-request `x-request-id` UUID. No signing, no nonce, no JWT. | The "private key" in the brief is not a cryptographic key. Auth is trivial; secret handling is the only hard part. |
| **Demo vs Real is chosen by URL path**, not a flag (`/execution/orders` vs `/execution/demo/orders`). A key is bound to one environment at creation. | Environment and credentials must switch together, atomically. One config value drives both. |
| **There is no SELL order.** `action` supports only `open`; `transaction` only `buy`/`sellShort`. Reducing a holding = closing a specific `positionId`. | Maps cleanly onto the ladder: one lot = one eToro position. |
| Opens are **USD-denominated** (`amount`); closes are **unit-denominated** (`UnitsToDeduct`, `null` = full close). | Lot bookkeeping must bridge the asymmetry. We always full-close a lot, so `UnitsToDeduct: null`. |
| **HTTP 200 means accepted, not filled.** Must poll `GET /api/v2/trading/info/orders:lookup`. Status 3=Filled, 4=Rejected, 5=PartiallyFilled, **11=WaitingForMarket**. | Every order needs a poll-to-terminal loop and a persisted reference id for crash recovery. |
| Minimum trade size is **per-instrument**, from `POST /api/v2/trading/info/eligibility` (`minPositionExposure` top-level; `minPositionAmount` **per `leverageConfigs` entry**, keyed by `(settlementType, direction)` — this app reads the `real`/`long` one). Not a constant. **Verified live 2026-09-04, corrected 2026-09-05**: the 2026-09-04 read reported `minPositionExposure=50.0` for SCHG; a fuller 2026-09-05 live eligibility payload (see DESIGN_market_data.md §10.14, §0.1) shows this was wrong — **both UPRO and SCHG carry `minPositionExposure=10.0`**, real/long `minPositionAmount=10.0` for each, `allowOpenPosition: true` for both, `maxUnitsPerOrder` 28,321 SCHG / 13,421 UPRO. The response's top-level key is `eligibilities` (not `eligibility`, which the code wrongly used until 2026-09-04 and got zero rows back from every real call), and the partial-close flag is `allowPartialClosePosition` (not `allowPartialClose`). **`min_order_size` in `config/config.toml` stays `50` regardless of this correction — that value is a deliberate operator decision, not a broker-floor tracking figure (DESIGN_market_data.md §10.14); do not lower it to 10.** | Query at preflight. Never hardcode $10. |
| Rate limits per user key, 1-min window: reads 60/min, writes 20/min, market-data 120/min, eligibility 20/min dedicated. | Ample for a session. Throttle the webapp during the trading window. |
| Candles: `GET /api/v1/market-data/instruments/{id}/history/candles/{dir}/{interval}/{count}`, `OneDay`, up to 1000. **Verified live 2026-09-04**: this path-segment shape is correct (the query-parameter shape found in eToro's own docs, `GET /market-data/instruments/history/candles?instrumentId=…`, 404s live and does not exist) — but `{dir}` must be `asc`/`desc`, not `backward`/`forward` as this table previously said (that also 404s). `limit=1000` genuinely returns 1000 rows; the response nests candles as `{"candles":[{"instrumentId":…,"candles":[{...OHLCV rows...}]}]}`. | Our history source. Self-contained — no Snowflake dependency. |
| Live prices: `GET /api/v1/market-data/instruments/rates?instrumentIds=…` → `bid`/`ask`/`lastExecution`. **No market-open flag.** **Verified live 2026-09-04**: this path and these three field names were already correct. The instrument-id field in each row is `instrumentID` (lowercase `i`, capitalised `ID`) — a third casing distinct from `instrumentId`/`InstrumentID`, which the code checked for until this date and so silently matched zero rows despite the HTTP call succeeding. The quote timestamp field is `date`, not `timestamp`/`dateTime`. | Source of today's provisional close. Market-open comes from our own calendar. |
| Instrument ids are **immutable**. Resolve via `GET /api/v1/market-data/search?internalSymbolFull=UPRO` — **not** `market-data/instruments/search` as this table previously said (404s, `RouteNotFound`). **Verified live 2026-09-04**: UPRO→3244, SCHG→1084, SPY→3000. The response's top-level key is `items` (not `instruments`), and display fields are `internal`-prefixed (`internalInstrumentDisplayName`, `internalExchangeName`) rather than the unprefixed names this table previously assumed. A bare `internalSymbolFull=SPY` query is **not** an exact match — it also returns `SPY.RTH`, `SPY4.DE`, `SPY5.L`, `SPYD`, etc, so the exact-symbol row must be picked out of the page, not assumed to be first. No `currency`/`instrumentCurrency` field was returned under any name tried. | Resolve once via `market-data/search?internalSymbolFull=UPRO`, cache forever. |
| `GET /api/v1/market-data/instruments?instrumentIds=…` (bulk instrument metadata). **Verified live 2026-09-04**: the response's top-level key is `instrumentDisplayDatas`, not `instruments` as this table previously assumed — the wrong key silently returned an empty list on every real call. | Bulk metadata lookups must read the right key or get nothing, silently. |
| `POST /api/v2/trading/info/{demo/}costs` requires a `transaction` field in the body (`"buy"`/`"sell"`/`"sellShort"`/`"buyToCover"`, case-insensitive) — without it eToro 400s. **Verified live 2026-09-04.** | Always send `transaction: TRANSACTION` (the module's own hardcoded `"buy"` constant) alongside `instrumentId`/`amount`/`leverage`. |
| `GET /trading/info/{demo/}portfolio` and `GET /trading/info/{demo/}pnl` both return 200 for the demo credential and **nest their payload under a `clientPortfolio` object** (`{"clientPortfolio": {"positions": [...], "unrealizedPnL": ..., "credit": ..., ...}}`), not at the response's top level as previously coded. **Verified live 2026-09-04**, but only against a zero-position demo account, so the bug was invisible before this correction (both the old top-level read and the fix return `[]` when there are no positions) — not yet confirmed against a *non-empty* portfolio. | A "didn't error" empty-list result is not proof the parsing is right; re-verify once a demo position actually exists. |
| `GET /api/v1/balances` has **no** demo/real path segment at all — same URL for both environments; which account it reads is decided entirely by which credential pair authenticates. `POST /api/v2/trading/info/{demo/}eligibility` and `.../{demo/}costs`, and `GET /api/v2/trading/info/{demo/}orders:lookup`, all take the segment **between** `info/` and the final path component. `GET /api/v1/trading/info/trade/{demo/}history` takes it **between** `trade/` and `history`, not before `trade`. `POST /api/v2/trading/execution/{demo/}orders` and the market-close-orders path take it right after `execution/`. Market-data endpoints (`search`, `instruments`, `instruments/rates`, `instruments/{id}/history/candles/...`) take **no** demo/real segment at all — market data is account-agnostic. **All verified live 2026-09-04.** | Every trading/info and execution path needs its segment placement checked individually, per-family — there is no single consistent rule across the whole API surface. |

**Unverified — must be proven in demo before going live:**

1. No documented rule about rejecting orders near the close. Our T-30min timing is *probably* fine — and is now further from the close than the T-10min the design originally carried, so if anything this unknown is *less* pressing than it was. Still the **highest-risk unknown.**
2. Whether UPRO is tradable on your eToro entity (`allowOpenPosition` from eligibility is authoritative — leveraged ETFs are region-restricted).
3. Whether partial close is permitted for these instruments (we don't need it, but preflight checks it).

### 1.2 The host: why an Android app, and why not the alternatives

**The ThinkPad was evaluated and rejected.** Lenovo ThinkPad L13 Gen 5, Windows 11 Pro,
AD-joined: Modern-Standby-only (no S1/S2/S3 in firmware), `powercfg /lastwake` showed a **wake
history count of zero** — the machine has never woken itself on a timer — Microsoft documents
**~8 minutes of S4 resume latency**, and the only workable Task Scheduler logon mode on a
domain-joined box is a stored password that breaks on every ~90-day domain rotation. Too many
unproven links in the chain.

**Termux is also ruled out.** Termux is a terminal application. It cannot call
`setAlarmClock()` — that is an Android app API, not something a shell can reach. On battery,
Doze **defers standard `AlarmManager` alarms into maintenance windows, suspends network
access, and ignores wake locks**. A Termux cron job is therefore unreliable on battery, which
is precisely the condition this host runs in.
(https://developer.android.com/training/monitoring-device-state/doze-standby)

**The chosen host: a native Android app with Chaquopy.**

| Fact | Consequence |
|---|---|
| `setAlarmClock()` is the strongest scheduling guarantee Android offers. Verbatim: *"the system never adjusts their delivery time… The system identifies these alarms as the most critical ones and leaves low-power modes if necessary to deliver the alarms."* And in Doze: *"Alarms set with `setAlarmClock()` continue to fire normally. The system exits Doze shortly before those alarms fire."* (https://developer.android.com/training/scheduling/alarms) | This is the only Android mechanism with a documented on-time guarantee under Doze. It is the spine of the design. |
| `setAlarmClock()` surfaces a **visible alarm icon in the status bar**. | Cosmetic. Accepted. |
| **Chaquopy is free and open-source under MIT** (https://chaquo.com/chaquopy/, https://github.com/chaquo/chaquopy/blob/master/LICENSE.txt). It embeds a real CPython interpreter in the APK and ships prebuilt native wheels for NumPy, SciPy, pandas and scikit-learn. | The Python strategy, SQLite layer, eToro client and Telegram code port over largely unchanged. **We do not have to drop numpy/pandas.** |
| Cost: a Gradle / Android Studio build pipeline, and sideloading a new APK on every code change. | Slower iteration than editing a `.py` on a laptop. Accepted. |
| **Chaquopy 17.0.0** (2025-12-01) supports AGP 7.3–9.2, Python 3.10–3.14, min API 24. AGP 8.x–9.2 already supports `compileSdk 36` — nothing structurally blocks building against Android 16. But **no Chaquopy release note or changelog entry confirms Android 16 / API 36 has actually been tested.** *Unverified.* (https://chaquo.com/chaquopy/doc/current/versions.html, https://chaquo.com/chaquopy/doc/current/changelog.html) | **Decision:** `compileSdk 36`, `targetSdk 35`. Don't couple an unverified toolchain dependency (Chaquopy on API 36) to a target-SDK bump that buys nothing safety-wise — Android 16's JobScheduler quota changes (§1.2) apply to every app regardless of `targetSdk`, so staying on `targetSdk 35` sidesteps zero real restriction. A throwaway Chaquopy test build against `compileSdk`/`targetSdk 36` is an explicit prerequisite before ever committing to `targetSdk 36` (§11 step 4). |
| The host's ABI is known and singular: Snapdragon 7 Gen 3 → **arm64-v8a only**. Chaquopy ships per-ABI native wheels. | Build for `arm64-v8a` only (`ndk.abiFilters`, §2) rather than bundling every architecture Chaquopy supports — a material APK size reduction, at zero cost since the device can never use the other ABIs anyway. |
| `setAndAllowWhileIdle()` / `setExactAndAllowWhileIdle()` *"cannot fire alarms more than once per nine minutes, per app."* | Would have constrained a polling cadence. **Mostly moot** — inbound control is push-driven, not polled, in the normal case (§8.2). The daily safety-net alarm uses this family once a day; the escalation-only hourly Telegram poll (§8.2) also uses it, but only while escalated, at 1 hour — comfortably above the 9-minute floor. |
| Doze names **FCM high-priority messages** as a wake mechanism: *"FCM high priority messages let you wake your app to engage the user. In Doze or App Standby mode, the system delivers the message and gives the app temporary access to network services and partial wakelocks, then returns the device or app to the idle state."* (https://developer.android.com/training/monitoring-device-state/doze-standby) | The inbound control channel and the redundant daily trigger (§8.2, §7.2). Note what is granted: **network + partial wakelock, temporarily**, at delivery time. |
| AOSP names telephony as the *other* Doze-exempt wake path: *"MMS/SMS/telephony services function independently of Doze and always wake the client apps even while the device remains in Doze mode."* … *"Apps receiving SMS and MMS messages are put on a temporary whitelist so that they can complete their processing."* (https://source.android.com/docs/core/power/platform_mgmt) | Technically the strongest fallback — and **rejected on cost**, not on capability. Recorded with its reasoning in §8.2 so the trade-off is not silently re-litigated. |
| Android 12+ requires `SCHEDULE_EXACT_ALARM` (user-granted, Settings → Apps → Special app access → Alarms & reminders) or `USE_EXACT_ALARM` (auto-granted, but carries a Play Store policy that does not apply to a sideloaded personal app). | We use `SCHEDULE_EXACT_ALARM`, check `AlarmManager.canScheduleExactAlarms()` before every schedule, and register a receiver for `ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED` — because *"When the `SCHEDULE_EXACT_ALARMS` permission is revoked for your app, your app stops, and all future exact alarms are canceled."* |
| Doze *"ignores wake locks."* | A wake lock **does not escape Doze**. It only keeps the CPU alive once we are already running. Correct pattern: the alarm gets us running, a partial wake lock holds through the ~60s of API calls, released in a `finally` block. |
| Android 10+ ships **time zone rules as an APEX mainline module**, updated through Google Play system updates independently of the system image. (https://source.android.com/docs/core/permissions/timezone-rules) | tzdata stays current without an OS upgrade. `zoneinfo("America/New_York")` can be trusted. |

#### App Standby buckets — the trap this design is built around

(https://developer.android.com/topic/performance/appstandby,
https://developer.android.com/topic/performance/power/power-details)

An app the user never opens decays into the **Rare** or **Restricted** bucket:

| Bucket | Alarm budget | Network |
|---|---|---|
| Rare | 1 alarm / hour | **disabled** |
| Restricted | 1 alarm / day | **disabled** |

A once-daily trading job survives the *alarm* budget in either bucket. But **network being
disabled breaks everything** — no eToro, no Telegram, no trade. The documented escape is the
battery-optimization exemption requested via `ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`:
*"An app that is partially exempt can use the network and hold partial wake locks during Doze
and App Standby."*

**This exemption is mandatory for this design, not an optimisation.** Without it the app is
inert. It is therefore checked in preflight (gate 2) on every single run, and its loss is
treated as a hard failure.

#### Android 16 — what changed for this design (nothing, and that is the headline finding)

The host's real OS version was corrected during research from Android 15 to **Android 16**.
This mattered enough to check directly against Google's own migration pages, because the whole
design leans on `setAlarmClock()`, Doze, App Standby and the battery-optimization exemption
holding still. **They do.** Neither the "behavior changes: all apps" nor the "behavior changes:
apps targeting 16" page mentions alarms, `setAlarmClock()`, `SCHEDULE_EXACT_ALARM`/
`USE_EXACT_ALARM`, Doze, or App Standby at all.
(https://developer.android.com/about/versions/16/behavior-changes-all,
https://developer.android.com/about/versions/16/behavior-changes-16)

This is genuinely good news and the single most load-bearing finding of this research pass: the
mechanism this design leans on hardest — `setAlarmClock()` and the exemptions around it — is
**unaffected by the OS version bump**. Everything in §1.2 written against "Android 12+" still
holds verbatim on Android 16.

A handful of Android 16 changes are real but tangential (§7.6 cross-references the relevant
ones): JobScheduler quota enforcement now also applies to jobs started while the app was TOP or
alongside a foreground service (previously exempt) — irrelevant to `AlarmManager` directly, but
matters if any retry path is ever built on WorkManager/JobScheduler instead. Android 16 also
adds better background-work diagnostics (`JobParameters.getStopReason()`,
`JobScheduler#getPendingJobReasonsHistory()`, `STOP_REASON_TIMEOUT_ABANDONED`) that are useful
if a session is ever missed and needs debugging. The Android 15 `dataSync` foreground-service
6h/24h execution cap is unchanged in 16 and remains irrelevant at ~15 minutes per run.

#### App hibernation — a new risk this design must actively defeat

**This is not in the original design and is added here because it applies directly.** Android's
App Hibernation (API 30+, unchanged in Android 16) force-stops an app that has gone **unused for
several months** and **strips its runtime permissions** — including exactly the two permissions
gate 2 depends on: `SCHEDULE_EXACT_ALARM` and the battery-optimization exemption.
(https://developer.android.com/topic/performance/app-hibernation)

The trigger is **lack of user interaction**, not lack of background activity. This app is
*designed* to never be opened by the user after setup — that is the whole point of it running
unattended every night. That puts it squarely in hibernation's target population, regardless of
how reliably it has been running trades in the background.

If hibernation fires, it silently revokes the two permissions preflight gate 2 exists to guard,
and the entire system goes dark with no trade and, worse, no alert — because the code path that
would send the alert depends on the very permissions just stripped. OriginOS ships its own
per-app equivalent, a **"Pause app activity if unused"** toggle (§7.4), separate from and in
addition to the stock Android mechanism.

Mitigations:

1. **Disable both switches** — the OriginOS "Pause app activity if unused" toggle (§7.4) and the
   stock Android "remove permissions and free up space" / "unused apps" setting, at setup and
   re-verified on the same schedule as the rest of §7.4.
2. **Detection already exists, by construction.** Preflight gate 2 (§5) checks both
   `SCHEDULE_EXACT_ALARM` and the battery-optimization exemption on *every single session*. If
   hibernation revokes either, the very next scheduled wake — which, critically, does not itself
   require user interaction to fire, only the alarm — will catch it and alert loudly. **Detection
   is not prevention**, though: gate 2 tells you the day after it happened, not before, and if
   the alarm itself was one of the casualties there may be no next wake to catch it on the normal
   path (the daily safety-net alarm and boot receiver, §7.2, are the backstops for that specific
   failure).
3. **Unverified, both ways.** Whether a successful unattended background run (network activity,
   a foreground service, a fired alarm) counts as "usage" that resets the hibernation clock on
   stock AOSP is not documented either way — Google's own page is silent on what counts as
   qualifying interaction. It is certainly unverified for OriginOS's own toggle, which is a vivo
   mechanism with no public specification at all (§7.4). Do not assume either mechanism is reset
   by the nightly run; treat mitigation (1) as the one you can actually rely on.

Add to §12's risk table accordingly.

#### vivo / iQOO — stated plainly

This is the weakest link and the user chose it knowingly (§12).

- dontkillmyapp.com/vivo rates vivo **3/5** and states: *"System restrictions on Vivo phones have not been fully uncovered yet."*
- There is **no vivo developer documentation** on background execution restrictions. Behaviour is reverse-engineered by the community, not specified.
- Developers report alarms failing on vivo devices **even with autostart enabled, battery optimization disabled, and the app locked in Recents** (https://github.com/WrichikBasu/ShakeAlarmClock/discussions/61). Alarmy maintains a dedicated vivo troubleshooting article.
- Contrast, honestly: Samsung is **more aggressive by default** but **documents its off switch**. vivo is less aggressive on paper and documents nothing. Undocumented is worse than aggressive-but-known, because it cannot be verified — only observed.

Required OriginOS settings (all of them, §7.4) — the concrete steps moved to §7.4 since the
Funtouch paths this section used to name no longer apply on OriginOS 6.

**Support-life caveat.** The iQOO Z10 is early in its support window and **will receive major
OS upgrades**, each of which can silently reset power-management settings. This is the
opposite of the usual "run it on an EOL phone" advice, and it is a real recurring cost:
**verify every setting after every OS update** is a runbook item, not a footnote.

#### Battery health

vivo's Funtouch OS exposed a charge limit under **Settings → Battery → Battery health and
charging** (80% cap); whether OriginOS 6 keeps the same path is **unverified** — OriginOS
replaced Funtouch on this device and settings screens have been reorganized (§7.4). *Community-
sourced and unverified either way:* availability was uneven across Funtouch builds and some
users reported the toggle resetting after a restart. This is not vendor documentation and must
be confirmed on the actual device, on OriginOS 6.

The Z10's cell is physically large (7300 mAh). Because the phone is often on battery and
sometimes on charge continuously, adopt: use the 80% cap if present, inspect the phone
physically every few months for any swelling or back-cover separation, and plan a **battery
replacement at roughly three years**. A swollen lithium cell is a fire risk, not a performance
complaint.

### 1.3 DST — simpler here, and genuinely safer

`15:30 America/New_York` is **03:30 Jakarta in winter and 02:30 in summer**. Jakarta is UTC+7
with **no DST of its own**, so the local fire time legitimately alternates between 02:30 and
03:30 WIB. That is correct behaviour, not drift.

The Windows design had to fight Task Scheduler's UTC-pinned triggers. Here we simply **never
compute an offset**:

- Compute the next US session's close minus 30 minutes **in `zoneinfo("America/New_York")`**, honouring US market holidays and 13:00 ET half-days.
- Convert that instant to device local time and hand it to `setAlarmClock()`.
- **Reschedule at the end of every run.**
- Plus a **daily safety-net alarm** that repairs the schedule unconditionally.
- An **"already ran today"** idempotency flag in SQLite so the safety net can never cause a double session.

tzdata correctness rests on the Android APEX time zone module (§1.2), which updates via Google
Play system updates independently of the OS image.

### 1.4 The strategy (from `etoro_trader/harness/strategies/std_band_ladder_cash_recycle.py`)

Three instruments, three roles:

- **SPY** — signal only, never traded. Source of `cone_5_z`, the running all-time high, and the drawdown tier.
- **SCHG** — the 1x core book. Bought only on new-ATH bars from surplus cash; sold only to fund a ladder buy; never below `core_floor_pct × equity`. Its **prior-year mean market value sets next year's UPRO budget.**
- **UPRO** — the only traded risk asset, in two books: `catapult` (the normal ladder) and `reentry` (deferred tranches).

**The managed set.** The real eToro account is not exclusively ours to manage — it can hold,
and does hold, other assets besides these three. etoroNightwatch defines the **managed set** =
{UPRO, SCHG}, a hardcoded allowlist, as the only symbols it will ever open, close, or size a
position in. SPY is signal-only and is never held. Everything else in the account — any
position outside this set — is **displayed, never touched** (§3, §6.D, §9). This distinction is
load-bearing throughout the rest of this document: reconciliation (§5 gate 11), the executor's
close path (§6.D), and the dashboard (§9) all key off it.

The signal, exactly:

```
cone_5_z[t] = (C[t]/C[t-5] - 1) / stdev_pop( C[i]/C[i-5] - 1 , i = t-259..t )
```

on SPY daily closes. First valid value needs **265 bars** (260 lookback + 5 shift).

#### `WARMUP_BARS` — your question, answered from the code

It is **not** the rolling window. Its sole consumer is a guard in `decide`:

```python
if self._bars <= self.cfg.warmup_bars:
    return self._cash_management(ctx, [], "warmup: signal trades disabled")
```

`self._bars` counts bars *this harness instance has processed*. The 260-bar window lives
entirely inside the indicator, which returns `None` until it has enough data. So:

- **You were right** that a live app doesn't need a 280-bar "don't trade yet" countdown.
- **But** you still need **≥265 bars of SPY daily closes in the database** or the signal is `None` and nothing ever trades.
- **And there is a trap:** if we instantiate the strategy fresh each day and call `decide` once, `self._bars` is permanently 1 and the strategy is stuck in warmup forever.

**Decision:** persist `_bars` in SQLite as part of strategy state, and set `warmup_bars = 0`
in live config. Belt and braces — the indicator's own `None` already gates trading until
history is sufficient. Seeding loads 400 daily bars so the window is full from day one.

#### Bug fixes carried into etoroNightwatch (approved)

1. **`_buy_if_allowed` defined twice** (L99 dead, L143 wins). The dead copy is the one that stamps `entry_spy_ath`, so `new_ath_sale` is always `False`, **no re-entry bucket is ever created**, and `REENTRY_TRANCHES`/`BUCKET_EXPIRY_BARS` are inert. → **Merged**: apply the G-F budget ceiling, call the parent gate, then stamp `entry_spy_ath`/`entry_spy_close`/`entry_spy_drawdown` and decrement `_recyclable_cash`.
2. **`_process_reentry_buckets` duplicates buckets** on a fill bar (`active.extend(...)` re-adds already-appended buckets). → **Rewritten as a single pass**: drop expired, fill at most one, then filter.
3. **`_reconcile_lots` fabricates lots** for unknown broker positions, losing `book`/vintage/ATH metadata. Fine for a backtest, wrong for a live app that restarts. → **Replaced**: load persisted lot rows from SQLite and match by `position_id`. An unmatched broker position is a **preflight failure**, not something to paper over.
4. **`_ensure_budget` raises** on the first bar of a new calendar year with no prior-year SCHG history — a live app restarting on Jan 2 would crash. → Falls back to `core_fraction × current equity` with a loud warning to Telegram.

#### Dropped parameters (approved)

`CASH_BAND_FRACTION` (only read by a parent `_cash_management` this strategy fully overrides —
inert) and `MARTINGALE_RATIO` (baked into `tier_caps` at import; changing it at runtime does
nothing). The six tier caps are hardcoded as the constants they actually are, so config
cannot lie.

#### Live parameter set

| Param | Value | Note |
|---|---|---|
| `CORE_FRACTION` | 0.4 | |
| `CORE_FLOOR_PCT` | 0.5 | |
| `ORDER_DIVISOR` | 60 | |
| `MIN_ORDER_SIZE` | 10 | superseded by per-instrument minimum if higher |
| `BUY_THRESHOLD` | −2 | |
| `SELL_THRESHOLD` | 3 | |
| `MAX_LOTS` | 60 | |
| `WARMUP_BARS` | **0** | changed from 280 — see above |
| `CASH_TARGET_MULTIPLIER` | 8 | |
| `CONE_HORIZON` | 5 | |
| `CONE_CENTER_MODE` | close | |
| `EXIT_MODE` | fixed | |
| `TRAILING_STOP_PCT` | 0.05 | inert while `EXIT_MODE=fixed`; retained |
| `DWELL_BARS` | 1 | |
| `TIER_Z_THRESHOLDS` | null | flat threshold every tier |
| `REENTRY_TRANCHES` | 3 | now actually functional (fix #1) |
| `BUCKET_EXPIRY_BARS` | 20 | now actually functional (fix #1) |
| ~~`CASH_BAND_FRACTION`~~ | — | **dropped, inert** |
| ~~`MARTINGALE_RATIO`~~ | — | **dropped, inert**; tier caps hardcoded |

Tier caps (cumulative fraction of budget, by SPY drawdown from ATH):

| Tier | Drawdown | Cap |
|---|---|---|
| 0 | > −0.10 | 0.10070574586703619 |
| 1 | −0.10 … −0.15 | 0.22155264090747964 |
| 2 | −0.15 … −0.20 | 0.36656891495601180 |
| 3 | −0.20 … −0.25 | 0.54058844381425030 |
| 4 | −0.25 … −0.35 | 0.74941187844413660 |
| 5 | ≤ −0.35 | 1.0 |

---

## 2. Layout

A single Gradle project. The Kotlin module owns scheduling, permissions and lifecycle; the
Chaquopy Python source set owns everything else, with the same module names the Windows design
used, so the logic is portable and testable off-device.

```
etoroNightwatch/
  settings.gradle.kts
  build.gradle.kts
  app/
    build.gradle.kts             # Chaquopy plugin, python { pip { install(...) } };
                                 # ndk.abiFilters = ["arm64-v8a"] only (§1.2, §7.1) — Chaquopy
                                 # ships per-ABI native wheels and the host has a single,
                                 # known ABI, so bundling the others is pure APK bloat
    src/main/AndroidManifest.xml # SCHEDULE_EXACT_ALARM, RECEIVE_BOOT_COMPLETED,
                                 # WAKE_LOCK, INTERNET, ACCESS_NETWORK_STATE,
                                 # POST_NOTIFICATIONS,
                                 # FOREGROUND_SERVICE(+_DATA_SYNC),
                                 # REQUEST_IGNORE_BATTERY_OPTIMIZATIONS
    src/main/java/.../nightwatch/
      MainActivity.kt            # setup/status UI, permission funnel, OriginOS checklist
      AlarmScheduler.kt          # setAlarmClock() for the session; daily safety-net alarm;
                                 # escalated hourly Telegram poll alarm (failure case only)
      SessionAlarmReceiver.kt    # fires → starts SessionService
      SafetyNetReceiver.kt       # daily unconditional schedule repair
      BootReceiver.kt            # RECEIVE_BOOT_COMPLETED → re-register alarms
      ExactAlarmPermissionReceiver.kt  # ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED
      NightwatchMessagingService.kt    # FirebaseMessagingService: onMessageReceived does the
                                 # work inline; getPriority() demotion canary; HMAC verify;
                                 # posts a visible notification for every command (§8.2)
      TelegramPollReceiver.kt    # escalation-only hourly poll; normally NOT scheduled (§8.2)
      SessionService.kt          # foreground service; partial wake lock; calls Python
      PowerStatus.kt             # battery level/charging, exemption + canScheduleExactAlarms,
                                 # FCM token validity, last-seen message priority
    src/main/python/nightwatch/  # Chaquopy source root — pure Python, unchanged names
      config.py
      db/                        # schema.sql, repo.py, migrations
      etoro/                     # client.py, auth.py, models.py, errors.py, ratelimit.py
      market/                    # calendar.py (US holidays + early closes), bars.py
      strategy/                  # cone.py, ladder.py, state.py, params.py
      engine/                    # preflight.py, session.py, executor.py, reconcile.py
      notify/                    # telegram.py (outbound), commands.py (inbound allowlist)
      web/                       # app.py, routers/, templates/, static/ — optional LAN-only
                                 # debug view now, not the primary UI (§9.5)
      host.py                    # thin bridge: Kotlin ⇄ Python entry points
      cli.py                     # run | seed | webapp | status | halt  (desktop dev + tests)
  config/
    config.toml                  # strategy params, symbols, budget override, mode,
                                 # cash_reserve_usd (§4.5)
    .secrets.env                 # API keys — gitignored, never logged
                                 # on-device: app-private storage, not external
  data/nightwatch.db             # SQLite, WAL — app-private filesDir on device
  logs/log_2026-09-02.txt        # one file per day, app-private
  doc/DESIGN.md  doc/RUNBOOK.md
  forward_test/
    replay.py                    # time-travel harness over the LIVE engine
    fake_broker.py               # eToro semantics: async fills, positionIds, min size
  tests/
  cloud/                         # not part of the Gradle build; deployed separately (§9, §11)
    worker/                      # Cloudflare Worker: snapshot store, push relay, on-demand
                                 # FCM trigger — same Worker as the cloud-scheduled daily push
    pages/                       # Static web app (Cloudflare Pages) — reads the snapshot
                                 # from the Worker, no server of its own
```

Everything under `src/main/python/nightwatch/` runs unmodified on a desktop CPython for
development, unit tests and the forward test. Only `host.py` differs in what calls it.

---

## 3. Data model (SQLite, WAL)

> **Superseded in part by [`DESIGN_market_data.md`](DESIGN_market_data.md)** (2026-09-04): the `bars` table is **dropped**. The phone stores no price series; history is fetched fresh each session and discarded. `corporate_actions` and `price_window_fingerprints` are added. Every other table below is unaffected.

| Table | Purpose |
|---|---|
| `bars` | `(symbol, date, o,h,l,c,v, is_provisional, source)` PK `(symbol,date)`. **`is_provisional` is the key column** — see §4.2 |
| `instruments` | ticker → `instrument_id`, cached metadata, last eligibility snapshot |
| `sessions` | one row per trading date: state machine, preflight verdict, timings, outcome. **Unique on `trading_date`** — cannot double-run a day |
| `preflight_checks` | per-check result rows — full audit of why a run aborted |
| `decisions` | per-session `cone_z`, `spy`, `ath`, `drawdown`, `tier`, intents JSON, reason |
| `intents` | one row per `OrderIntent` **written before any HTTP call**, with its `x-request-id` UUID — the idempotency anchor |
| `orders` | eToro `orderId`, `referenceId`, status history, terminal status, fill price/units |
| `lots` | the ladder book, **managed-set symbols only**: units, entry price, `entry_reference_spy`, `cost_basis`, `position_id`, `book`, `entry_spy_ath/close/drawdown`, exit fields. The executor may only close a `position_id` present here (§6.D) |
| `buckets` | `bucket_id, sale_date, sale_spy_close, proceeds, tranche_amount, tranches_bought, last_reference_spy, created_bar` |
| `strategy_state` | singleton: `bars`, `ath`, `below_streak`, `recyclable_cash`, `budget_year`, `budget`, `order_size`, `cash_target`, `sale_batch_sequence`, `sale_batch_date` |
| `schg_daily_values` | `(date, value)` — feeds next year's budget |
| `budget_history` | `(year, budget, order_size, cash_target, source: auto\|override)` |
| `portfolio_snapshots` | per-session **account-level** totals (all-asset equity, total cash, `broker_available_cash`) **and sleeve** totals (`cash_reserve_usd`, `sleeve_cash`, `sleeve_equity`, sleeve unrealized/realized P&L, §4.5), plus per-position rows for every broker position with an `is_managed` flag — non-managed holdings are recorded for display only (§1.4, §9). This is also the row pushed to the Cloudflare Worker (§9) |
| `notifications` | Telegram send log with delivery status |
| `commands` | inbound control audit: command text, `channel` (`fcm`\|`telegram`), sender/chat id, HMAC counter (replay rejection), verdict (accepted/rejected + reason), timestamp — an inbound control channel must be auditable |

---

## 4. The daily session

### 4.1 Sequence

```
T-15min  setAlarmClock() fires → SessionAlarmReceiver → SessionService (foreground)
         Acquire PARTIAL_WAKE_LOCK (released in finally, §7.6)
         ↓
  [1]    PREFLIGHT — 12 gates, all must pass (§5)
         ↓  abort → Telegram with reason, hold the wake lock, no return to Doze
  [2]    SETTLE — overwrite yesterday's provisional bar with the official close (§4.2)
         ↓
  [3]    SLEEP until exactly T-30min (uses the real close, incl. 13:00 ET half-days)
         ↓
  [4]    FETCH live SPY/SCHG/UPRO rates → today's provisional bar
         ↓
  [5]    COMPUTE cone_5_z over 265+ bars ending on today's provisional close
         ↓
  [6]    DECIDE — strategy.decide() → OrderIntents  (persisted to `decisions`; every cash/
         equity input is the sleeve, never the account, §4.5)
         ↓
  [7]    ARM GATE — mode==live AND NIGHTWATCH_ARM set AND no HALT flag
         ↓  else → log + Telegram "would have done X", stop cleanly
  [8]    EXECUTE — per intent: persist request-id → POST → poll to terminal status
         ↓
  [9]    PERSIST fills, update lots/buckets/state, snapshot portfolio
         ↓
  [10]   TELEGRAM report (or the daily heartbeat if there was nothing to do)
         ↓
  [11]   RESCHEDULE next run (before releasing — a crash here must not orphan the alarm)
         ↓
  [12]   Release wake lock, stop the foreground service, let the device return to Doze
```

**On the margin.** The Windows design fired at **T-30min** solely to absorb ~8 minutes of S4
hibernate-resume latency. **That reason is gone** — `setAlarmClock()` fires on time and the
device is already running. We keep a **5-minute margin** at T-15min, and it buys exactly two
things: Wi-Fi re-association after Doze, and preflight. Nothing else. Step [3] sleeps out the
residual so the order still lands at T-30min.

### 4.2 The live-bar problem — and why `is_provisional` exists

> **§4.2a — Which T-30 decisions may trust a provisional close, and which may not.**
> **Decided 2026-09-05 with the operator, from measurement, not preference.** A decision taken at
> T-30 rests on a *provisional* close that the official close can contradict. The rule is that
> this is acceptable wherever the error is self-favouring, and unacceptable where it is adverse.
>
> - **The ladder entry signal (`cone_5_z`) keeps the provisional close.** Its errors cannot hurt
>   us. A false positive means we opened below the official close — we bought the dip cheaper
>   than the bar that closed above the threshold. A false negative means we simply skipped an
>   entry: opportunity cost, never a loss. The operator's framing: *"if we did open a position
>   < -2 std then it's a good thing for us even if the price goes back above -2 std at close."*
> - **The SCHG all-time-high test must use the PREVIOUS DAY'S SETTLED BAR.** Its false positive
>   is adverse and asymmetric: buying SCHG on an ATH that the close revokes means buying the core
>   book at a local high and consuming surplus cash for it. ATH is also *binary*, so an arbitrarily
>   small move flips it, where `cone_5_z` merely shifts.
>
> **Measured on 4,184 SPY daily bars, so the asymmetry is not assumed:**
>
> | | count | share |
> |---|---|---|
> | closes at a new ATH | 554 | 13.24% |
> | touched an ATH intraday but closed below | 372 | 8.89% |
> | closes within ±0.137% of the prior ATH (one typical 30-min move) | 347 | 8.30% |
>
> **40.2% of intraday ATH touches fail to close at an ATH.** That is an upper bound on the T-30
> error rate (T-30 is not the day's high), but the knife-edge row brackets it from below: bars
> within one 30-minute move of the prior ATH are 62.6% as numerous as genuine ATH closes.
>
> **The drawdown tier does NOT need the same treatment, and this was checked rather than
> assumed.** `TIER_CAPS` are cumulative ceilings, so a transient T-30 dip could in principle
> unlock a deeper tier than the settled close justifies and spend ladder capacity reserved for a
> real crash. It is negligible in practice because the tier boundaries (-10/-15/-20/-25/-35%) are
> levels SPY crosses rarely, whereas the ATH boundary (drawdown = 0) is one it sits on constantly:
> **1.27%** of bars fall within ±0.137% of any tier boundary versus **17.90%** for the ATH
> boundary — **14x** the exposure. Tiers keep the provisional close.
>
> **Implementation note.** This needs **no second daily schedule.** The existing T-30 session on
> day D already has day D-1's official close settled, so it evaluates the ATH test against that
> settled bar and executes the SCHG buy then. A second wake (proposed and rejected the same day)
> would have cost battery, added a failure surface for vivo's background killer, and — the real
> objection — created two independent sessions spending from one cash sleeve and both moving the
> `0.5 x sleeve_equity` core floor. Same correctness, none of that.
>
> **The one accepted deviation:** SCHG buys now land one session later than the backtest's. For a
> long-hold core book bought on ATHs this is expected to be noise, but it is a real difference and
> the forward test (§10) should report it rather than assume it away.


> **Superseded in part by [`DESIGN_market_data.md`](DESIGN_market_data.md)** (2026-09-04): the compounding-error problem described below is **removed, not reduced**. The provisional close is never persisted, so nothing survives to poison the 260-bar window. `is_provisional`, `settle_previous()` and session step [2] all go away.

Your requirement: *use today's live price, not yesterday's data.* Correct, and it has a
consequence the backtest never faced.

At 15:30 ET **today's official close does not exist yet.** We take the live last price as a
**provisional close** for bar `t`. The strategy consumes it and trades on it. Fine.

But `cone_5_z` uses a **260-bar rolling standard deviation**, so today's bar sits inside the
window for the next 260 sessions. If we leave the provisional value in place, every future
signal is computed against a slightly wrong history, and the error compounds silently.

**Honestly quantified, not waved away.** Trading at T-30 instead of T-10 means the provisional
close is taken **20 minutes further from the true official close** than the design originally
carried. That is a real cost to signal fidelity — the price can move meaningfully in twenty
extra minutes of trading — not a rounding error to be assumed away. It is not left as an
unmeasured assumption: the forward test (§10) replays the strategy using provisional closes
sampled at both T-10 and T-30 against the official close series and reports the divergence
directly, in `cone_5_z` and in the resulting trade decisions.

So **step [2] of every session settles the previous bar**: fetch the official `OneDay` candle
for the last trading date, overwrite the provisional row, flip `is_provisional = 0`. Any bar
still provisional after settlement is a preflight failure. This is invisible in a backtest
and would otherwise have been a slow, undetectable drift in production.

### 4.3 Order mapping

**Buy (UPRO or SCHG)** — `POST /api/v2/trading/execution/orders`:

```json
{ "action":"open", "transaction":"buy", "instrumentId": 0,
  "orderType":"mkt", "leverage":1, "amount": 0.0, "orderCurrency":"usd",
  "settlementType":"real" }
```

`leverage: 1` is correct and deliberate. The strategy's `leverage=3` is descriptive metadata
about UPRO being a 3x ETF; the backtest deducts full notional cash. We buy UPRO unleveraged
and let the ETF's own leverage do the work. Using `leverage > 1` would double-lever *and*
mandate a `stopLossRate`.

**Sell** — one lot = one position, so always a full close:
`POST /api/v1/trading/execution/market-close-orders/positions/{positionId}` with
`{"InstrumentId": id, "UnitsToDeduct": null}`.

**Minimum size guard.** `order_size = budget / 60` can fall below the instrument's
`minPositionExposure`. etoroNightwatch **blocks and alerts loudly** rather than silently
resizing — a silent bump would break the tier-cap arithmetic the whole strategy rests on.
**Verified live 2026-09-04, corrected 2026-09-05: SCHG's and UPRO's `minPositionExposure` are
both $10.00** — the 2026-09-04 read reported $50.00 for SCHG, which a fuller 2026-09-05 live
eligibility payload contradicts (DESIGN_market_data.md §10.14, §0.1); `minPositionAmount` is
nested per `leverageConfigs` entry keyed by `(settlementType, direction)`, not a top-level field;
this app reads the `real`/`long` entry. **`min_order_size = 50` in `config/config.toml` is
unaffected by this correction — it is the operator's own deliberate floor, not a broker-tracking
figure, and stays 50 regardless of what the broker's minimum is (DESIGN_market_data.md §10.14).**
This makes the sizing constraint below a config-driven one, not a differing-broker-minimums one:
SCHG's core buys are sized off `core_floor_pct × sleeve_equity` / the surplus-sweep calculation
(§4.5), not directly off `order_size`, so `sleeve_cash` must still be large enough that SCHG's
computed core fraction clears the operator's `$50` `min_order_size` floor (both instruments'
actual broker minimum is $10, so it is Nightwatch's own configured floor that binds here, not a
per-instrument broker asymmetry). Under the exclusion model, `sleeve_cash` is a runtime
quantity (`broker_available_cash − cash_reserve_usd`, §4.5), so this is now a live-cash
condition rather than a static config comparison — see the two-tier warning in
`nightwatch.strategy.min_allocation`.

See §6.A for why `leverage: 1` and `settlementType: real` are enforced structurally, not just
in this payload.

### 4.4 Extra deposits

**Revised again by the move to the exclusion sleeve model (§4.5, operator decision
2026-09-04).** Under the earlier inclusion model, a deposit sitting in the wider account did not
raise `sleeve_cash` — the sleeve was a fixed config figure, immune to deposits by construction.
That protection is **gone under exclusion, deliberately**: `sleeve_cash = max(0,
broker_available_cash − cash_reserve_usd)`, so any deposit that lands in `broker_available_cash`
above the reserve **immediately** raises `sleeve_cash`, and therefore the budget and order sizes,
with no explicit act required. This is the direct trade-off for a sleeve that grows with the
account instead of staying pinned — see §12 for this tracked as a standing risk, and for the
`max_sleeve_cash_usd` ceiling that was put to the operator on 2026-09-04 and **declined** in
favour of reserve-only. Not an open question; do not re-propose it.

To keep a deposit out of the strategy's reach, raise `cash_reserve_usd` by the deposited amount
**before** the deposit lands, or as soon as practical after — an explicit, auditable config edit,
same as before, just aimed at the opposite lever (withholding cash, not allocating it).

The **annual budget rule is unchanged**: `budget = floor(0.4 × prior_year_mean_SCHG_value / 20)
× 20`, struck once a year from the sleeve's own SCHG history, and unaffected either way by a
mid-year deposit.

### 4.5 The sleeve: what `cash` and `equity` mean

The user carves out a slice of the eToro account for this app to manage; the rest of the account
is the user's to use freely, entirely outside etoroNightwatch's view. So the strategy must never
reason about **whole-account** cash or equity — only about the slice it was given. Call that
slice the **sleeve**.

**Operator decision, 2026-09-04 — exclusion, not inclusion.** The sleeve was originally defined
by *inclusion*: `cash_allocation_usd`, a fixed absolute figure in config, was the app's entire
cash. The operator rejected that model:

> "I don't want inclusion because it has negative impacts to the strategy. I want exclusion to
> better manage my cash for things outside nightwatch."

The reasoning is concrete, not just a preference. A fixed inclusion figure pins the strategy to
whatever number was set once — it cannot grow as the account grows without an explicit,
manual edit every time. At the live config (`core_fraction=0.4`, `order_divisor=60`,
`min_order_size=50`, `max_lots=60`), `order_size` stays clamped to the `$50` floor until the
sleeve reaches `min_order_size × order_divisor / core_fraction` = **$7,500** (§4.3's
minimum-viable-allocation calculator, tier 2 — `nightwatch.strategy.min_allocation`). A $2,000
inclusion allocation funds only **16 of 60** ladder rungs, and the six `TIER_CAPS` — cumulative
fractions of budget across SPY drawdown tiers (§1.4) — can never reach their deep-drawdown tiers
on that budget. **Exclusion** fixes this: the sleeve is *whatever the account has, minus a
reserve the operator withholds*, so it grows with the account automatically, no manual edit
required.

| Term | Definition |
|---|---|
| `cash_reserve_usd` | An **absolute USD figure in config** (`nightwatch.config.StrategyConfig.cash_reserve_usd`) — the cash the operator withholds from the app for their own use, outside etoroNightwatch. `0` (also what `None`/absent collapses to) means "use all available cash"; the shipped `config.toml` default is `0.0`. Replaces `cash_allocation_usd` outright — the two models are not run side by side. |
| `sleeve_cash` | `max(0.0, broker_available_cash − cash_reserve_usd)`. A reserve larger than what is actually available **clamps to zero, never negative** — the app then simply has nothing to trade, which is a warning condition (§4.3's calculator, tier 1), never a crash. |
| `broker_available_cash` | Unchanged. Computed with eToro's documented formula (§1.1, §9), **not** `totalBalance` (which is portfolio value, not cash): `available = credit − (Σ ordersForOpen[i].amount where mirrorID = 0 + Σ orders[i].amount)`. |
| `sleeve_equity` | Unchanged in shape: `sleeve_cash + market value of our UPRO lots + market value of our SCHG lots`. Explicitly **not** account equity — non-managed holdings never enter this number, under either model. |

**Implementation line drawn here, deliberately.** This document specifies the runtime clamp
(`sleeve_cash = max(0.0, broker_available_cash − cash_reserve_usd)`); the config field
(`cash_reserve_usd`, with `>= 0` validation and `None → 0.0`) is implemented now in `config.py`
and `config/config.toml`. The runtime computation itself — reading `broker_available_cash` from
the broker and applying the formula — belongs to step 9's session engine, which is still a stub
(`engine/preflight.py`, `engine/session.py` not yet built). Building that arithmetic into engine
code that doesn't exist yet would be scaffolding with nothing to call it; the spec above is what
step 9 must implement, verbatim, when it exists.

**Every place the strategy reads cash or equity means the sleeve, not the account — unchanged by
the move to exclusion**, because `sleeve_cash` and `sleeve_equity` keep the same downstream shape
regardless of how `sleeve_cash` itself is computed:

- the G-B cash gate in `_buy_if_allowed`
- `cash_target = 8 × order_size`
- the SCHG core floor, `core_floor_pct × equity` — reads `0.5 × sleeve_equity`
- the first-year budget fallback, `core_fraction × equity` — reads `0.4 × sleeve_equity`
- the SCHG surplus sweep threshold
- the SCHG sell-to-fund calculation

**Why this matters, stated plainly.** If any of these read whole-account values while the app
controls only a slice, the arithmetic is wrong in both directions, and not symmetrically — the
worst case is the core floor computed against an account equity figure far larger than what the
app actually manages, which **permanently blocks buys** the sleeve could otherwise afford.
Getting this half-right — reading the sleeve in some places and the account in others — is
worse than not doing it at all, because the failure is silent arithmetic, not a crash. This risk
is unaffected by exclusion vs. inclusion; only the *size* of the sleeve changed, not what reads
it or how.

**A new risk exclusion introduces — tracked in §12.** Under inclusion, cash deposited for
purposes unrelated to the strategy was invisible to the app by construction. Under exclusion, any
deposit above the reserve is **immediately tradable** — it silently enlarges the sleeve, and
therefore the budget and order sizes, with no explicit act by the operator. See §12 for the full
entry, including the `max_sleeve_cash_usd` safety ceiling the operator considered and **declined**
on 2026-09-04 — reserve-only is the deliberate choice, not an unfinished one.

### 4.6 Budget: auto with override

Auto (the backtest rule): `budget = floor(0.4 × prior_year_mean_SCHG_value / 20) × 20`;
first year `0.4 × sleeve_equity` (§4.5). Then `order_size = max(budget/60, 10)`,
`cash_target = 8 × order_size`.

Override: `config.strategy.budget_override_usd`. Non-null wins immediately, `order_size` and
`cash_target` recompute from it, and the change is written to `budget_history` with
`source='override'`. Setting it back to `null` returns to auto at the next session. No
repercussions, no restart, no state surgery — exactly as specified.

---

## 5. Preflight — the twelve gates

All must pass. Any failure aborts before any order, sends Telegram with the specific gate and
reason, and **holds the wake lock** rather than returning to Doze, so the device stays
reachable for diagnosis in the morning.

| # | Gate | Fails when |
|---|---|---|
| 1 | **Halt switch** | `HALT` flag set (file in app storage, or set via the Telegram `/halt` command) |
| 2 | **Power & host permissions** | On battery and < 40%; **or the battery-optimization exemption is no longer granted**; **or `AlarmManager.canScheduleExactAlarms()` is false**; or the partial wake lock could not be acquired |
| 3 | **Network** | No DNS/HTTPS to `public-api.etoro.com` after 90s of polling (Wi-Fi re-associates lazily coming out of Doze) |
| 4 | **Clock** | Local clock differs from the API `Date` header by > 60s |
| 5 | **Database** | Cannot open, schema version mismatch, or an unfinished prior session exists |
| 6 | **Auth** | `GET /balances` not 200 |
| 7 | **Environment match** | Config says `real` but the key is demo-bound (or vice versa) — probed, not trusted |
| 8 | **Instruments** | UPRO/SCHG/SPY unresolvable; `allowOpenPosition` false; `minPositionExposure` > `order_size` |
| 9 | **Calendar** | Not a US trading day; also resolves early-close (13:00 ET) days so T-30min is correct |
| 10 | **Price data** | ⚠️ **STALE — superseded by DESIGN_market_data.md §6.** This row still describes the settled/provisional bar architecture, which that document deletes entirely (no `bars` table, no `is_provisional`, no settle step — §3.2). Gate 10 as actually built (`engine/preflight.py`, 2026-09-05) checks freshness of the provisional close and rate/candle agreement per symbol; window completeness, corporate actions and signal sanity are specified there but not yet built. Read §6 of that document, not this cell. |
| 11 | **Reconciliation** | An **unknown broker position in a managed symbol** (UPRO/SCHG we did not open) → hard failure. A **DB lot with no matching broker position** → hard failure (closed outside the app; needs reconciliation and a notification, never a silent re-buy). A position in **any non-managed symbol** is ignored for trading and recorded for display only — never a failure (§1.4, §6.D) |
| 12 | **Risk caps** | Session order count or notional exceeds configured caps; daily loss guard tripped |

**Gate 2 deserves emphasis.** The two host permissions it checks are the ones Android can take
away without telling us. Losing the battery-optimization exemption silently disables the
network in the Rare/Restricted buckets; losing `SCHEDULE_EXACT_ALARM` **stops the app and
cancels every future exact alarm**. Both are checked on every run and both fail **loudly** to
Telegram, naming the exact Settings path to restore them. A quiet degradation here would look
identical to "the market was closed."

**Gate 10 is partially built as of 2026-09-05 — see `engine/preflight.py`, not this cell, for
what actually exists.** Items 1 (freshness of the provisional close, plus the rate-vs-candle
cross-check) and 3 (sanity) are implemented and tested; items 2 (window completeness), 4
(corporate actions), and 5 (signal sanity) remain stubs, as do gates 1–9, 11, and 12 below. The
paragraph immediately below predates that build and describes the now-superseded `bars`-table
architecture; kept for its historical reasoning, not as a current status claim. "Stale/zero live
rate" as described there was unimplemented at the time: `Rate` carries the timestamp eToro
actually returns (the live field is `date`, mapped to `Rate.timestamp` in
`etoro/client.py::get_rates`), but `market/bars.py` never reads it. This was deferred twice on
the same reasoning both times — a staleness check needs a "now" reference and a configured
limit, and neither belongs in `market/bars.py` itself; they belong in preflight, which isn't
built yet (steps 6 vs. 9, §11). This is not hypothetical: a live probe on 2026-09-04 with the
market closed showed SCHG's `lastExecution` timestamped to the *prior* session's close while
SPY's was current pre-market — two managed-set instruments, same moment, different staleness,
proving a stale rate is a real state this app will see. Tracked as a risk in §12.

Gate 10 also has no answer yet for a **bar-history gap** (a missing trading day in the middle of
the 260-bar window, not just a stale tip) — see the bad-tick guard note in §6.G and the tracked
risk in §12; `market/bars.py::missing_trading_days()` already exists as the diagnostic a future
gate would call.

**Gate 9 has an unhandled failure mode above it, not within it.** `market/calendar.py` runs
`EASTERN = ZoneInfo("America/New_York")` at module import time, before gate 9 or any other gate
runs. See the tracked risk in §12.

Beyond the twelve gates above: an aborted run **never returns to Doze** — the wake lock is
held so the device stays reachable for diagnosis in the morning. Everything else that used to
live here — arming, credential separation, idempotency, session dedup — is consolidated into
§6, so it sits alongside the rest of the app's safety design rather than being split across two
sections.

---

## 6. Safety mechanisms

Every control below is either **structural** — impossible by construction, enforced in code
before any HTTP call is made — or **procedural** — a check that runs every session but could,
in principle, be bypassed by a bug elsewhere. Structural controls are preferred wherever the
API allows them; procedural controls exist where a structural one isn't possible (nothing stops
eToro's servers from misbehaving, so a poll-to-terminal check is procedural by necessity). Both
kinds are listed together below, grouped by what they protect against.

### A. Never margin, never short — structurally, not by convention

- Every open order intent sends `leverage: 1` exactly. An intent constructed with any other
  leverage value is **rejected before the HTTP call is made**, not merely defaulted.
- `max_leverage = 1` is a **hardcoded Python constant, not a `config.toml` value** — so no
  config edit, however wrong, can raise it.
- `transaction` is only ever `buy`. The code path that would send `sellShort` or `buyToCover`
  does not exist.
- `settlementType` is only ever `real`. Never `cfd`, `marginTrade`, or `realFutures` — those are
  eToro's leveraged/short settlement paths.
- Reductions happen **only** by closing a `positionId` we already own, via the market-close
  endpoint (§4.3). There is no code path that opens a short to reduce exposure.
- **Cross-check from §1.1:** eToro requires `stopLossRate` when `leverage > 1`. If the API ever
  demands a stop-loss on one of our orders, that is not something to satisfy — it is proof we
  are accidentally leveraged, and it is treated as an **abort condition**.
- Preflight gate 12 (risk caps) additionally checks: if any existing managed-symbol position
  reports `leverage > 1`, halt and notify. That should be structurally unreachable; the check
  exists anyway, because "should be unreachable" is not the same guarantee as "is checked."
- **Stated honestly:** UPRO is itself a 3x-leveraged ETF. We buy it **unleveraged** — our own
  order carries `leverage: 1` — and let the fund's internal leverage do the work. The
  strategy's `leverage=3` metadata (§1.4, §4.3) is descriptive only; the backtest deducted full
  notional cash, and so do we.

### B. Never exceed available cash

- `sleeve_cash` is clamped to `broker_available_cash`, computed with eToro's documented formula
  (§4.5), never `totalBalance`.
- The **sum of all intent amounts in one session** must not exceed `sleeve_cash` minus the
  tier-1 reserve.
- Each order **re-checks remaining sleeve cash immediately before submission**, not once at
  planning time — an earlier fill in the same session changes what is left.

### C. Blast-radius caps (config, with defaults)

| Config key | Purpose |
|---|---|
| `max_orders_per_session` | Independent backstop on how much can happen in one run. |
| `max_notional_per_session_usd` | Independent backstop in dollar terms. |
| `max_open_lots` | Independent backstop; the strategy's own `MAX_LOTS = 60` (§1.4) is a strategy-level bound — this is a second, separate one. |
| daily loss guard | If sleeve P&L for the day is worse than a configured threshold, halt and notify (feeds preflight gate 12 and auto-halt, §6.I). |

**Not yet in this table, but should be.** `MAX_TICK_DEVIATION_PCT`, `LEVERAGED_TICK_DEVIATION_PCT`,
`MIN_PLAUSIBLE_PRICE`, `LEVERAGED_SYMBOLS`, and `STALE_REFERENCE_TRADING_DAYS` (§6.G) are
currently module-level constants in `market/bars.py`, not `RiskConfig` fields — so tuning the
crash-guard today means a code change, not a config edit. They are risk knobs by every
definition this table uses for the others; two independent build sessions reached this same
conclusion and left them in place only because `config.py` was outside that step's scope. Move
them into `RiskConfig` (`config.py`) the next time either file is touched.

### D. Symbol allowlist

The **managed set** (§1.4) = {UPRO, SCHG}, hardcoded, plus SPY as signal-only. Two enforcement
points:

- **Opens.** An order intent for any symbol outside the managed set is rejected before the HTTP
  call — there is no config path to widen this set at runtime.
- **Closes.** The executor may only ever close a `positionId` that (a) exists in our own `lots`
  table **and** (b) whose symbol is in the managed set. This is the single most important
  blast-radius control in the app: it makes it **structurally impossible** to close a position
  we did not open ourselves, including any other holding in the same eToro account.

This is also what fixes preflight gate 11 (§5) for an account that holds more than UPRO and
SCHG: an unknown position in a **managed** symbol is a hard failure (something opened UPRO/SCHG
we don't know about — do not trade blind); a DB lot with no matching broker position is a hard
failure (closed outside the app, needs reconciliation and a notification, never a silent
re-buy); a position in **any non-managed symbol** is ignored for trading and recorded for
display only (§9) — never a failure. Before this fix, gate 11 would have aborted every session
against a real, multi-asset account.

### E. Arming

- Live execution needs **both** `mode = "live"` in config **and** the `NIGHTWATCH_ARM`
  environment variable, **and** no `HALT` flag set. Neither of the first two is sufficient
  alone.
- **The invariant:** every kill switch in §6.I can only ever **stop** trading. None of them,
  alone or combined with anything else, can **start** a trade.
- This invariant is what bounds the manual trade controls spec'd in §9.7: they are a UI on top
  of the same executor, not a second path to money. In `mode = "live"` a manually-placed order
  still requires `NIGHTWATCH_ARM` exactly as an automated order does, and the controls are
  reachable only from the on-device UI or the LAN-only embedded dashboard — never from the
  Worker or Telegram (§8.2, §8.3), where a compromised channel could otherwise use them to move
  money.

### F. Environment safety

- Preflight probes that the API key's environment actually matches the configured environment
  (`real`/`demo`) **before any write** — probed against the API, not trusted from config
  (§5 gate 7).
- If the two disagree, the session refuses to execute live.
- **Separate credentials by privilege:** a Real+**Read** key for the webapp/snapshot, a
  Real+**Write** key used only by the session service, a Demo pair for the forward test. eToro's
  IP-whitelist and expiry options are used on all of them.
- Every Telegram message and every dashboard view states **DEMO** or **REAL** unmistakably — no
  ambiguity about which environment a number belongs to.

### G. Bad-data safety

> **Superseded in part by [`DESIGN_market_data.md`](DESIGN_market_data.md)** (2026-09-04): layer 3 of the bad-tick guard (gap-aware compounding tolerance, the 10x cap, `STALE_REFERENCE_TRADING_DAYS`) is **deleted** — it existed only to survive gaps that can no longer occur. Layers 1 and 2 are retained, with a tight band against a now-trustworthy reference.

- Reject a live rate whose timestamp is older than a configured staleness limit. **Not yet
  implemented** — see the gate 10 note in §5 and the tracked risk in §12.
- **Bad-tick guard:** reject a live price that deviates from the previous settled close by more
  than a configured percentage. The entry signal fires when `cone_5_z < −2`, so a single bad
  tick on the downside could manufacture a spurious buy signal — this is a real failure mode for
  a system that trades on one live price point (§4.2), not a theoretical one. `market/bars.py`
  widens this tolerance multiplicatively with the number of elapsed NYSE sessions since the
  last settled bar, capped at 10x the base per-session tolerance (200% for SPY/SCHG, 600% for
  UPRO — see `compounded_deviation_pct()`), and logs at `ERROR` once the gap exceeds
  `STALE_REFERENCE_TRADING_DAYS = 10`. That widening is a deliberate tradeoff so a genuine
  multi-week drawdown doesn't get misread as a bad tick, but past roughly a week's gap only an
  order-of-magnitude error still trips it — a real gap should be caught as a gap, not squeezed
  through an ever-wider tolerance band. The structurally correct fix is a **preflight gate that
  detects the gap directly** (`missing_trading_days()` already exists as the diagnostic) and
  halts the session, alerts, and forces a re-seed, rather than trusting a stretched tolerance to
  keep catching real errors indefinitely. An `ERROR` log alone is not sufficient here — this
  runs on a phone that lives away from the user; it must alert over the Telegram path (§8) the
  same as every other preflight failure. Tracked as a risk in §12.
- Sanity-bound the signal itself: an implausible `|cone_5_z|` (say, > 10) **aborts** rather than
  trades on it.
- Refuse to trade with fewer than 265 settled SPY bars (§1.4), or with any unsettled provisional
  bar still in the window (§4.2, preflight gate 10).

### H. Order-verification safety

- HTTP 200 means accepted, not filled (§1.1). Every order is polled via `orders:lookup` to a
  terminal status.
- Status **11 (WaitingForMarket)** is explicitly "queued, not filled" — recorded as such, with a
  decision on whether to cancel; it is never booked as a fill.
- Status **5 (PartiallyFilled)** records the actual filled units; the requested quantity is
  never assumed.
- The `x-request-id` UUID is persisted to `intents` **before** the HTTP call and doubles as
  eToro's `referenceId`. A lost response (crash, network drop, process death) is recovered by
  looking the order up by `referenceId`, never by re-sending — the difference between a lost
  response and a double buy.
- The documented write rate limit (20/min, §1.1) is respected with a token bucket.

### I. Kill switches and auto-halt

- Manual: the `HALT` file, the Telegram `/halt` command (§8.2), and `mode = "dry_run"` in
  config.
- **Auto-halt**, triggered by: any reconciliation mismatch in a managed symbol (§6.D), N
  consecutive session failures, or the daily loss guard (§6.C) tripping. Auto-halt always
  notifies with the reason.
- **One session per trading date**, enforced by a unique constraint on `sessions.trading_date`
  and backed by the "already ran today" flag the safety-net alarm consults (§7.2) — the safety
  net can re-register freely without ever causing a second session.
- Restated: **every kill switch here can only stop trading, never start it** (§6.E).

---

## 7. Host integration — Android (iQOO Z10, Android 16 / OriginOS 6)

### 7.1 App architecture

| Component | Language | Responsibility |
|---|---|---|
| `MainActivity` | Kotlin | Setup and status UI. Permission funnel: `SCHEDULE_EXACT_ALARM`, battery-optimization exemption, notification permission. Renders the OriginOS settings checklist (§7.4) with a "verified on" date. |
| `AlarmScheduler` | Kotlin | Computes nothing itself — asks Python for the next fire instant, then calls `setAlarmClock()`. Owns the daily session alarm and the daily safety-net alarm unconditionally; registers the hourly Telegram poll alarm **only while the app is in the escalated state** (§8.2), and cancels it on de-escalation. |
| `SessionAlarmReceiver` | Kotlin | Fires on the session alarm, immediately starts `SessionService`. Does no work in the receiver. |
| `SessionService` | Kotlin | Foreground service (`dataSync`) with a persistent notification, so Android will not kill the ~15 minutes of run. Acquires the partial wake lock, invokes the Python entry point, releases in `finally`. |
| `BootReceiver` | Kotlin | `RECEIVE_BOOT_COMPLETED` → re-register the session alarm and the daily safety-net alarm; re-register the escalation poll only if persisted state says the app is currently escalated. |
| `ExactAlarmPermissionReceiver` | Kotlin | `ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED` → re-register alarms when the permission is granted back; alert to Telegram when it is lost. |
| `TelegramPollReceiver` | Kotlin | Escalation-only. When scheduled, hourly `setExactAndAllowWhileIdle()` → drains the Telegram update queue and checks the de-escalation condition (§8.2). Normally not scheduled at all. |
| `nightwatch/*` | Python (Chaquopy) | Everything else: config, SQLite, eToro client, calendar, strategy, engine, notify, web. Identical to the desktop tree. |

The division is deliberate: **Kotlin does only what Python cannot reach** — alarms,
permissions, wake locks, boot, service lifecycle. All trading logic, all state, all decisions
stay in Python where they can be forward-tested off-device.

### 7.2 Alarm scheduling and DST

```
end of session  → python: next_close_minus_30("America/New_York", holidays, half_days)
                → convert to device local instant
                → AlarmManager.setAlarmClock(instant, showIntent)
```

`setAlarmClock()` is chosen over every other `AlarmManager` variant because it is the only one
Android documents as never being deferred: *"the system never adjusts their delivery time…
leaves low-power modes if necessary to deliver the alarms,"* and, specifically under Doze,
*"Alarms set with `setAlarmClock()` continue to fire normally. The system exits Doze shortly
before those alarms fire."*

Three layers of redundancy:

1. **Session alarm** — rewritten at the end of every run (step [11], before the wake lock is released).
2. **Daily safety-net alarm** — fires once a day, unconditionally recomputes and re-registers the session alarm. Repairs a crash, a week powered off, a DST flip, or a reboot the boot receiver somehow missed.
3. **Boot receiver** — re-registers on every boot.

Because we always compute in `America/New_York` and convert, the local fire time correctly
alternates between **02:30 and 03:30 WIB** across the US DST boundaries, and 13:00 ET half-days
land at their own correct local time. No offset is ever computed by hand.

**Idempotency:** an `already_ran(trading_date)` flag in `strategy_state`/`sessions` means the
safety net can re-register freely and a duplicate fire is a no-op, not a second session.

Before every `setAlarmClock()` call: check `AlarmManager.canScheduleExactAlarms()`. If false,
do not silently fall back to an inexact alarm — that would be a silent downgrade to
unreliability. Alert to Telegram and surface it in the UI.

### 7.3 The mandatory exemptions

| Permission / exemption | How | Why it is not optional |
|---|---|---|
| `SCHEDULE_EXACT_ALARM` | User grant: Settings → Apps → Special app access → Alarms & reminders | Without it, no exact alarm at all. Revocation **stops the app and cancels all future exact alarms.** |
| Battery-optimization exemption (`ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`) | One-time system dialog from `MainActivity` | Grants network access and partial wake locks during Doze and App Standby. **Without it the app has no network in the Rare/Restricted buckets and cannot trade at all.** |
| `RECEIVE_BOOT_COMPLETED` | Manifest | Alarms do not survive a reboot on their own. |
| `WAKE_LOCK` | Manifest | Holds the CPU through the run once already awake. |
| Foreground service (`dataSync`) + notification permission | Manifest + runtime grant | Keeps the ~15-minute run from being killed mid-order. |

Both user-granted items are re-verified in **preflight gate 2** on every run.

### 7.4 OriginOS 6 settings checklist

**None of this is vivo documentation.** vivo publishes no developer-facing background-execution
policy for OriginOS, full stop. dontkillmyapp.com/vivo — the closest thing to a community
reference, rating vivo 3/5 — covers **Funtouch OS only** and has **not been updated for OriginOS
6**; its guidance does not transfer automatically. The checklist below is
**COMMUNITY-REPORTED, from a single blog source, not vivo documentation — it must be verified
on the physical device**, then re-verified after every OS update.
(https://cyberraiden.wordpress.com/2026/08/02/origin-os-6-apps-section-settings-explained/)

| # | Setting | Path | Status on the real device |
|---|---|---|---|
| 1 | Autostart / auto-launch **enabled** | Settings → Apps → [app] → Battery usage → enable auto-start. OriginOS folds this into the per-app "Battery usage" screen — unlike Funtouch, which had a separate standalone Autostart manager. | **Confirmed present** — `Pengaturan → Aplikasi → Mulai otomatis` exists as its own screen (not folded into Battery usage as the community source predicted). |
| 2 | Background power **"Izinkan penggunaan daya di latar belakang"** | Settings → Apps → [app] → Kontrol daya latar belakang → set to **Izinkan penggunaan daya di latar belakang** (tri-state: Kontrol pintar / Izinkan / Batasi — the default is Kontrol pintar, the vague vendor-managed mode most likely to silently kill a 3 AM alarm; Izinkan is the one that matters). | **Confirmed present and set** — screen exists exactly as predicted, with these three exact Indonesian labels. |
| 3 | Battery optimization exemption | Settings → Apps → Special app access → Alarm & pengingat (grants `SCHEDULE_EXACT_ALARM`), and the standard `REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` prompt. | **Confirmed present** — both `Alarm & pengingat` and the battery-optimization exemption prompt exist and were grantable; verified in-app via `canScheduleExactAlarms()==true` and `isIgnoringBatteryOptimizations()==true` on every wake. |
| 4 | App **locked in Recents** | **No confirmed OriginOS 6 equivalent found.** Verify manually: long-press the app's card in Recents and look for a lock/pin icon. | Still unverified — not exercised during probe testing so far. |
| 5 | **"Jeda aktivitas aplikasi jika tidak digunakan" disabled** | This is the OriginOS hibernation analogue (§1.2) — a vivo-specific mechanism layered on top of, and separate from, stock Android App Hibernation. Disable it for this app explicitly. | **Confirmed present and disabled** — toggle exists under app info exactly as predicted and was switched off. |

Items 1–3 and 5 are no longer community-sourced guesses — they were located and set on the
physical device (2026-09-03) using the exact Indonesian labels recorded above, which the reader
should use verbatim rather than the earlier speculative English paraphrase. Item 4 remains
unverified. **This is still not confirmed-safe** — vivo documents none of the *behaviour* behind
these switches, only their existence is now confirmed; no verified reports, positive or
negative, exist for whether OriginOS 6 actually *honours* them through a full night of deep
Doze (the closest prior evidence, alarms failing on vivo hardware even with every known
Funtouch-era setting applied — https://github.com/WrichikBasu/ShakeAlarmClock/discussions/61 —
predates OriginOS 6 and cannot be assumed to transfer either way). That is exactly why §11
step 4's empirical multi-night proof is what actually resolves this, not the checklist by
itself.

**After every OS update, re-verify all five and re-run a one-night proof before trusting the
next live session.** The Z10 is early in its support life and will take major upgrades.

### 7.4b Private DNS — required, discovered empirically, not anticipated in the original design

**Finding (2026-09-03, on the physical device):** `public-api.etoro.com` did not resolve to
eToro's real servers on either the corporate Wi-Fi *or* the phone's mobile data connection.
Both networks returned a TLS certificate for `stopmalware.moratelindo.io` /
`trustpositif.moratelindo.io` — Indonesia's Kominfo/Trust Positif internet-filtering system,
applied at the ISP/carrier level. This is DNS-based hijacking (the resolver returns the
block-page IP for a filtered hostname), not SNI/deep-packet inspection — confirmed by the fact
that switching DNS alone fixed it, with no other network change. It reproduced on two
independently-operated networks (a corporate uplink and a cellular carrier), which is strong
evidence this is nationwide filtering-mandate behaviour rather than one operator's policy, and
therefore very likely also present on the phone's home Wi-Fi ISP by default.

**Required fix — set once, device-wide:** Settings → Connections → More connection settings →
Private DNS → **Private DNS provider hostname** → `one.one.one.one` (Cloudflare, DNS-over-TLS).
This is deliberately the **device-wide** setting, not a per-network DNS override in a single
Wi-Fi's advanced settings — the device-wide form applies uniformly across every network the
phone joins, Wi-Fi and mobile data alike, with no need to reapply it after a network change.
Confirmed working: `public-api.etoro.com` resolved correctly and returned eToro's real
`Unauthorized` response (the expected pass condition with no credentials configured, §1.1) once
this was set.

**Add to preflight (§5) and to the OriginOS checklist above** as item 6: verify Private DNS is
still set to `one.one.one.one` (or equivalent) before trusting a session's network reachability
result, since an OS update or a factory-reset-adjacent event could plausibly clear it the same
way it can clear the permissions above. Add to §12 as a tracked risk: if this setting is ever
lost, the failure mode is silent and specific — DNS still resolves to *something*, TLS still
completes a handshake, and the failure surfaces only as a certificate-verification error on
eToro's hostname specifically, not a generic connectivity failure. A future preflight check
should distinguish this exact failure signature (`SSLPeerUnverifiedException` naming a
`moratelindo.io` certificate) from an ordinary network outage in its Telegram alert text, so a
recurrence is diagnosable from the phone at 3 AM without re-deriving this chain of reasoning.

### 7.5 Boot survival

`BootReceiver` on `RECEIVE_BOOT_COMPLETED` re-registers the session alarm and the daily
safety-net alarm unconditionally, and re-registers the escalation-only hourly Telegram poll
**only if persisted state says the app is currently escalated** (§8.2) — a reboot must not
itself turn on a poll that was not running before it. **This must be tested explicitly**:
reboot the phone, then confirm the job actually fires afterwards, before any real money is
involved. With autostart disabled on OriginOS, the receiver will silently fail to run at
all — which is exactly the failure mode that looks like nothing happened.

### 7.6 Battery strategy and returning to Doze

- The alarm gets us **running**; the partial wake lock only keeps us running. A wake lock does **not** escape Doze — Doze *"ignores wake locks."* Acquire after the service starts, release in a `finally` block, always.
- Total awake time per session is roughly 15 minutes, of which ~60 seconds is actual API traffic.
- **In the normal case, no Telegram polling alarm is scheduled at all** — inbound control rides FCM push, which costs no dedicated alarm or standing connection of our own (§8.2). Only on escalation (FCM demoted, token invalid, or no FCM message for N days) does `AlarmScheduler` register a repeating `setExactAndAllowWhileIdle()` hourly poll, cancelled again the moment de-escalation is detected. This keeps the battery cost of inbound control at zero in the common case and bounded to one poll/hour only in the failure case. At 1 hour the cadence is comfortably above the documented 9-minutes-per-app floor for `*AndAllowWhileIdle` alarms.
- On a clean finish: reschedule, release the wake lock, stop the foreground service, and **let the device return to Doze naturally**. Nothing is forced.
- On an abort: hold the wake lock and stay out of Doze so the device is reachable.
- Charging: use the 80% charge cap if OriginOS 6 exposes it (*unverified whether the Funtouch-era
  path carries over*, §1.2), inspect the cell periodically, plan replacement at ~3 years.
- FCM high-priority demotion after ~7 days of silent sends (§8.2) is unchanged on Android 16;
  the visible-notification-per-command rule already mandated there exists specifically to
  prevent it — not duplicated here.

---

## 8. Telegram — the primary interface

Telegram is the reliable away-channel. The web app (§9) is much more available now that it
reads a pushed snapshot rather than the phone's own LAN server, but it still depends on the
phone eventually waking to push — Telegram remains the channel with no such dependency.

### 8.1 Outbound

One message per event, all tagged with the trading date:

- **Session start** — fired at X, target Y, mode live/dry-run
- **Preflight** — pass, or the specific gate that failed and why
- **Decision** — `cone_z`, SPY, drawdown, tier, budget, cash, and the intents (or "hold" + reason)
- **Each fill** — symbol, side, USD/units, price, order id
- **Session end** — positions, equity, cash, P&L, next scheduled run
- **Any abort or exception** — full reason, and that the device is staying awake
- **Daily heartbeat, even on no-trade days** — "ran, nothing to do, next run at X"

The heartbeat is not decoration. Without it, a killed app and a quiet market look identical.
**With it, silence is unambiguously a failure.** Given §12's top risk, this is the single most
valuable monitoring feature in the design.

Sends are logged to `notifications` with delivery status, so a *failed* alert is itself visible.
Outbound sends **do not depend on any inbound polling at all** — an alert goes out the instant
it is generated, via a direct Telegram `sendMessage` call, with no `getUpdates` cycle involved
on the sending path. If Telegram is unreachable the session still completes; it does not become
a trading failure.

### 8.2 Inbound — read-only plus halt

Inbound is **push-driven, not polled**, in three tiers:

1. **Primary — FCM high-priority push.** Doze's own documentation names it as a wake path:
   *"FCM high priority messages let you wake your app to engage the user. In Doze or App
   Standby mode, the system delivers the message and gives the app temporary access to network
   services and partial wakelocks, then returns the device or app to the idle state."*
   (https://developer.android.com/training/monitoring-device-state/doze-standby) Latency is
   seconds. Battery cost is effectively zero — Google Play Services owns the one persistent
   socket the phone already maintains for every app; we add no connection of our own.
   `NightwatchMessagingService.onMessageReceived` does the work inline, inside the temporary
   network/wakelock grant, and: verifies the HMAC on the payload, posts a **visible
   notification for every command** (required — Google demotes an app's high-priority FCM
   privilege after 7 days of silent sends with no visible UI), and logs
   `RemoteMessage.getPriority()` as a demotion canary.
2. **Fallback — opportunistic Telegram check.** Not a standing poll. The app already wakes once
   a day for the trading run; while it is awake and on the network anyway, it makes one
   `getUpdates` call, piggybacked on the existing wake. Zero extra battery, zero extra alarm.
   Worst case for a command arriving only this way: it waits until the next daily run.
3. **Escalation only — hourly Telegram poll.** Scheduled by `AlarmScheduler` **only** when the
   FCM canary reports demotion, the FCM registration token is invalid, or no FCM message has
   been observed for N days. On escalating, the app sends an outbound Telegram alert explaining
   why and that battery use will rise. It **de-escalates automatically** the next time a
   `PRIORITY_HIGH` message is observed, and the poll alarm is cancelled. Battery is spent only
   in the failure case, never as a standing cost.

**Accepted coverage gap.** If the phone has no data connectivity at all, there is no inbound
command channel until it reconnects — none of the three tiers reaches a phone with no network.
SMS/telephony is the one Android wake path documented to function independently of Doze and of
data connectivity (§1.2), and it was rejected on cost, not capability, so this gap was accepted
knowingly rather than closed. The daily trading run itself is unaffected by this gap — the
on-device `setAlarmClock()` needs no network to fire — but a run that wakes with no
connectivity will abort safely at **preflight gate 3** rather than trade blind.

| Command | Effect |
|---|---|
| `/status` | mode, arm state, halt state, last session verdict, host permission state |
| `/positions` | open positions with entry, units, P&L |
| `/pnl` | equity, cash, unrealized and realized P&L |
| `/today` | today's decision: `cone_z`, tier, intents or hold reason |
| `/logs` | tail of the current day's log |
| `/next` | next scheduled run, in ET and WIB |
| `/halt` | set the halt flag — preflight gate 1 will abort every future session |
| `/resume` | clear the halt flag |
| `/help` | list the supported commands above |

**Explicitly not available: config changes, arming live mode, manual trades, budget changes.**

The rationale is one sentence: **a compromised Telegram account must never be able to move
money.** Every allowed command either reads state or *stops* trading. `HALT`/`RESUME` are safe
precisely because halt can only ever prevent a trade, and resume only restores the normal path —
which still requires both arming keys (§6.E) that Telegram cannot touch.

Security:

- **Allowlist on Telegram chat id.** Any message from any other chat id is ignored and logged, never answered. FCM messages are addressed to our own registration token, which is the equivalent binding for that channel.
- **HMAC-of-(command, counter), with replay rejection by counter.** Every inbound command, on either channel, carries an HMAC over the command and a monotonic counter; a counter at or below the last-accepted value is rejected as a replay. Every attempt — accepted or rejected, and why — is written to the `commands` audit table (§3): command, channel (`fcm`\|`telegram`), sender/chat id, counter, verdict, timestamp. An inbound control channel must be auditable.
- Commands are parsed as **data against a fixed table**. Nothing from a message is ever passed to a shell, a file path, or an eval.
- **No inbound listener, no open port to the internet, on either channel.** FCM delivery is a push from Google's infrastructure to Google Play Services, not a socket we open; Telegram is `getUpdates` long-polling, never a webhook.

### 8.3 Remote operability is a design requirement, not a convenience

**The phone lives away from the user most of the time.** It stays at home; the user does not.
So anything that can only be done by touching the phone is a real operational hurdle, not a
minor inconvenience — a check that "should be quick, just open the app" may not be possible for
days. The design principle, stated plainly: **every capability that does not move money must be
reachable through the Worker, with the phone untouched.**

This was proven, not just asserted, during the Android platform proof (§7, the multi-night
gate): `ping`, `status`, `logs`, `runchecks`, `schedule`, and `selftest` were all driven from a
laptop via `POST /command` → HMAC'd FCM push → phone executes → `POST /snapshot` → `GET
/snapshot` read back the result, with the phone sitting untouched throughout an entire session.
That loop is the template the real app's command surface should follow, not a probe-only
convenience to be dropped when the real app is built. Concretely, carry forward into the real
app everything already proven on the probe, beyond the read-only table in §8.2:
- **`schedule`** — set or move the next session's trigger time remotely, HMAC-signed over
  `command:counter:arg` so the argument itself cannot be tampered with independently of the
  command name (§8.2's HMAC already covers `command:counter`; the argument extension is a
  strict, backward-compatible superset — verified: a signature with no `arg` is unchanged, and
  tampering with `arg` alone invalidates the signature).
- **`runchecks`** — re-run preflight (§5) and the connectivity/tzdata/clock/standby-bucket
  checks on demand, without waiting for the next scheduled session, and push the result.
- **`selftest`** — the full check battery, for "is everything still healthy" at any time.
- **`ping`** — cheapest possible liveness probe, for "is the phone even reachable right now"
  before reaching for anything heavier.

**The boundary that does NOT move, and is deliberately in tension with "make everything
remote":** arming live trading, changing `cash_reserve_usd` or any other config, and
anything that could originate a trade remain **local-only** (§6.E) — set in `config.toml` and
the `NIGHTWATCH_ARM` environment variable on the device itself, never reachable from Telegram or
an FCM command. This is not an oversight; it is the same invariant §8.2 already states — *a
compromised control channel must never be able to move money* — extended to cover the Worker
and the laptop-side `/command` caller too, since those are now also remote and therefore also a
compromise surface. If read-only remote diagnostics, `schedule`, and `HALT`/`RESUME` do not
cover the real operational need, that is a decision to revisit explicitly with the user — not
one to silently expand.

**The one hurdle remote operability genuinely cannot remove: one-time, in-person device setup.**
Android does not expose `SCHEDULE_EXACT_ALARM`, the battery-optimization exemption, the
notification permission, or any OriginOS setting (autostart, background power, the hibernation
toggle) as remotely grantable — each requires a physical tap through a system permission dialog
or settings screen (§7.3, §7.4). Private DNS (§1.2, §7.6 — see the Kominfo finding below) is the
same: a device-wide setting with no remote API. **All of this must be done once, before the
phone is left unattended, and re-verified after any OS update that resets it** (§12 already
tracks the update-reset risk) — because after that point there is no remote path to fix a
revoked permission or a reset setting; the phone would need to come home, or the user would need
someone physically present with it.

---

## 9. Web app

The architecture changed from a phone-hosted LAN dashboard to a **push-snapshot, static
frontend** model, so the dashboard works from anywhere, not just on the same Wi-Fi as the
phone.

### 9.1 Architecture

- **The Z10 Android app is the server of record.** It holds the SQLite database (§3), runs the
  strategy, and is the only thing that ever talks to eToro. Nothing about that changes.
- **After each session (and on demand, §9.3), the phone pushes a portfolio snapshot** to the
  Cloudflare Worker already being built for the cloud-scheduled FCM trigger (§1.2, §7.2) — same
  infrastructure, one more job for it. The Worker stores the snapshot in Workers KV or D1 (both
  free tier at this scale).
- **The web app is static assets plus a server-side Cloudflare Pages Function proxy** (both free
  on Pages), not a purely static page — a purely static page would have to embed the Worker's
  read token in client-side JavaScript, which is a public secret the instant the page loads
  (the same class of flaw §9.3 rejects the Telegram-bot-as-API idea for). The Pages Function
  (`cloud/pages/functions/api/`) holds the read token as a Pages environment secret and injects
  it server-side on the way to the Worker; the browser only ever calls same-origin
  `/api/snapshot` and `/api/refresh`. There is still no server *of our own to run* — Pages hosts
  both the static assets and the Function, and the Worker remains the only thing holding state.
- **On-demand refresh.** The web app asks the Worker to fire a high-priority FCM push at the
  phone — the exact same push mechanism already designed for the redundant daily trigger. The
  phone wakes, pushes a fresh snapshot, the page updates. Latency: seconds, not "wait for the
  next scheduled session."

### 9.2 Why this over the phone-hosted dashboard

| | Phone-hosted FastAPI (original) | Worker + Pages (revised) |
|---|---|---|
| Reachable from | Same Wi-Fi/LAN only | Anywhere |
| While phone is in Doze | Unreachable | Snapshot is cached at the Worker — still reachable |
| Inbound ports on the phone | None required, but the server itself listens on-device | **None** — the phone stays outbound-only; no dynamic DNS, no NAT traversal |
| Secrets exposed to a browser | None (LAN only) | **Still none** — the Worker read token lives only in the Pages Function's server-side environment, never in client-side code; the browser calls same-origin `/api/...` and the Function injects the token on the way to the Worker. No Telegram bot token ever reaches a browser either (§9.4) |

### 9.3 Rejected alternative: the Telegram bot as the web app's API

The original idea — have the web app talk to the Telegram bot directly — was considered and
**rejected**, recorded here so it is not silently revisited:

1. A browser frontend calling the bot would need the **bot token in client-side code**.
   Unacceptable — a public, unauthenticated secret.
2. Telegram's `getUpdates` supports only **one consumer**. The web app polling it would conflict
   with the phone's own inbound command handling (§8.2) — they would race for the same update
   queue.
3. Telegram's **4096-character message limit** makes it a poor data transport for a portfolio
   snapshot with positions, history and chart data.

### 9.4 Security for the Worker path

- A **shared secret** authenticates the phone's push to the Worker.
- A **separate read token** authenticates the web app's read from the Worker — different
  credential, different privilege, same separation principle as §6.F.
- Everything over HTTPS.
- The snapshot contains portfolio data (account and sleeve balances, positions) and **must
  never be publicly readable** — the read token gates it.
- No Telegram bot token, no eToro API key, ever reaches the browser or the Worker's public
  surface.
- **What the remote app deliberately does not get: any way to place, size, or close a trade.**
  §8.3 makes broad remote operability a design principle and that tension is deliberate, not an
  oversight to fix later — but a manual trade is unambiguously a money-moving control, and §6.E /
  §8.2's invariant (a compromised remote channel must never be able to move money) applies to the
  Worker and the Pages app exactly as it applies to Telegram. The manual trade controls (§9.7)
  exist only on the on-device UI and the LAN-only embedded dashboard; they are never exposed
  through `/api/snapshot`, `/api/refresh`, or any future Worker command route.

### 9.5 The embedded FastAPI server — now optional

The FastAPI server embedded via Chaquopy (as originally designed) is **kept, but demoted**: a
**LAN-only local/debug view**, not the primary UI. This simplifies the phone app's operational
burden — no DHCP reservation, no "keep Wi-Fi alive when idle" requirement for normal use — while
still leaving a direct, no-network-dependency view available for on-device debugging.

Contents (server-rendered pages plus a small JSON API; TradingView lightweight-charts for the
chart) — the same shape is shown on **both** the LAN debug view and, in equivalent form, the
static web app, sourced from the pushed snapshot:

- **Account panel** — total cash, total equity, all broker positions (managed and non-managed),
  clearly labelled which is which.
- **Sleeve panel**, visually separated from the account panel — `cash_reserve_usd`,
  `sleeve_cash`, `sleeve_equity` (§4.5), sleeve unrealized and realized P&L, budget,
  `order_size`, `cash_target`, `recyclable_cash`, and today's `cone_z` / drawdown / tier.
- **Positions table** — every field the API exposes: entry price, units, invested, leverage,
  unrealized P&L, open date. Managed positions (UPRO/SCHG lots) are visually distinguished from
  other holdings, which are shown for information only and are never actionable from here.
- **Trade history table** — from `/trading/info/trade/history` joined to our lots, so each
  managed trade shows *why* it happened (`cone_z`, tier, book, bucket id). History lookback is
  capped at just under 1 year per request; we page and cache.
- **Chart** — SPY with the cone bands and z-score, **trade markers overlaid** for every buy and
  sell, click-through to the decision that produced it.
- **Strategy state** — open lots by book, live re-entry buckets with tranche progress and expiry
  countdown.
- **Sessions & logs** — every session, its preflight verdict, and the day's log file (LAN debug
  view only — not pushed to the Worker).
- **Host panel** — battery level, charging state, exemption status, `canScheduleExactAlarms()`,
  next alarm, last boot, date the OriginOS checklist was last verified (LAN debug view only).
- **Snapshot freshness** — every view built from a pushed snapshot shows an explicit "as of"
  timestamp, so staleness is visible rather than assumed away.

*Overlay note (carried from etoro_trader):* chart overlays are registered indicators, never
strategy-specific metadata.

### 9.6 Watching a forward test run live (new requirement, added post-Android-proof)

The forward test (§10) runs on desktop CPython during development, not on the phone — but
the user should be able to watch one run from the same web app that will later show the live
account, rather than only reading a static end-of-run report. This reuses the architecture
already proven working for the phone: the forward-test runner pushes progressive snapshots to
the same Worker `/snapshot` endpoint (authenticated the same way, with `PHONE_PUSH_SECRET`, or a
separate `SIM_PUSH_SECRET` if the two should be distinguishable in the audit trail — build
agent's call, document whichever is chosen), tagged with a `mode: "simulation"` field the
snapshot schema does not otherwise carry, and the existing Pages UI renders a visibly distinct
"simulation running" view when that field is present — same components (positions table, trade
history, chart with markers, sleeve panel), unmistakably labelled as historical/simulated so it
is never confused with the live account view. Push cadence should be roughly one snapshot per
simulated trading day, not per tick — fast enough to watch progress, not so fast it floods the
Worker's free-tier write budget over a multi-year run.

### 9.7 Manual trade controls (spec only — not built until steps 8–10 land)

The operator wants buttons to place a trade by hand, primarily so a real, non-empty position
exists on the demo account to verify `get_portfolio()`'s envelope handling against (§1.1, §12)
without waiting for the strategy's own signal to fire — see the §11 step 11 acceptance
criterion. This is a spec for steps 8–10 to build against, not code to write now.

**Where this lives, and where it explicitly does not.** A manual trade button is unambiguously a
money-moving control, so the placement follows §6.E's arming invariant and §8.2's stated
rationale (*"a compromised Telegram account must never be able to move money"*) all the way
through:

- **Allowed:** the on-device Android UI (`MainActivity`), reachable only with physical access to
  the unlocked phone; and the embedded FastAPI dashboard (§9.5), which is LAN-only.
- **Forbidden, explicitly:** the Cloudflare Pages remote web app (§9.1–§9.4), the Worker's
  `/command` route (§8.3), and Telegram (§8.2). None of these gets a manual-trade capability,
  ever. §6.E and §8.2 rule 4 (`doc/HANDOFF.md` §7) are the reason: §8.3 makes broad remote
  operability a design principle precisely because the phone lives away from the user most of the
  time, and that is in deliberate tension with "arming, config writes, and anything that can move
  money stay local-only" — the design calls that tension out rather than silently resolving it,
  and a manual trade is squarely on the money-moving side of that line. Widening either remote
  surface to include it would be exactly the silent expansion §8.3 and HANDOFF §7 rule 4 warn
  against.

**Arming still applies, unmodified.** In `mode = "live"` a manually-placed order requires
`NIGHTWATCH_ARM` exactly as an automated order does (§6.E) — there is no "manual" exemption from
the two-key gate. The controls exist for demo verification and are **demo-only until step 12**;
they are never a bypass around arming, the symbol allowlist (§6.D — UPRO/SCHG only), `max_leverage
= 1` (§6.A), or the risk caps in preflight gate 12 (§5).

**Same executor, same bookkeeping — never a side channel.** An order placed by hand must flow
through the identical executor, idempotency (`x-request-id`/`referenceId`, §6.H), lot bookkeeping,
and reconciliation path as an automated order. It must never write a position the `lots` table
doesn't know about — that would manufacture exactly the unmatched-managed-symbol position that
§5 gate 11 treats as a hard preflight failure, on the very account the controls exist to verify
against. Concretely: a manually-created lot still needs its book/vintage/`entry_spy_ath` metadata
(§1.4, HANDOFF §5 fix 3) written at the same time the position is opened, or it will break
reconciliation on the very next scheduled session.

**Minimum viable control surface**, shaped by §4.3's USD/units asymmetry:

- **Symbol picker: UPRO or SCHG only.** SPY is signal-only and is never offered as a tradable
  symbol here, same as everywhere else in the app (§1.4, §6.D).
- **Open a position** — operator enters a USD amount (opens are USD-denominated, §4.3); the
  control enforces the same minimum-size guard as automated buys (§4.3's per-instrument
  `minPositionExposure`) rather than silently resizing.
- **Close a position** — operator picks one existing lot from the `lots` table and closes it in
  full; per §4.3 there is no partial close in this app, so the control is a full-close action
  only (`UnitsToDeduct: null`), not a units-entry field. Opens and closes are deliberately
  different shapes in the UI because they are different shapes in the API.
- **A confirmation step** before submission — this is an irreversible, money-moving action.
- **The resulting order's status surfaced back to the operator** — accepted/filled/rejected
  (§1.1's poll-to-terminal semantics), not just a fire-and-forget "sent."

---

## 10. Forward test — the acceptance gate

Not a backtest. The forward test drives the **real live engine** — the same session state
machine, preflight, executor, and persistence — over historical data with a time-travel clock
and a fake broker. It runs on desktop CPython against the identical Python source set.

`forward_test/fake_broker.py` reproduces eToro's actual semantics, not a convenient
abstraction: async accept-then-fill, `orderId`/`referenceId`/`positionId` lifecycle, status
`11 WaitingForMarket`, USD-denominated opens, unit-denominated closes, per-instrument
minimums, and injectable failures (timeout after submit, rejection, partial fill).

**Acceptance criteria — all must pass before the app is reported finished:**

1. A multi-year run over real SPY/SCHG/UPRO daily history completes with no unhandled exception.
2. Strategy decisions match `etoro_trader`'s replay for the same inputs, **allowing for the four approved bug fixes** — every divergence traced to a specific fix, none unexplained.
3. Deferred re-entry actually fires: buckets created, tranches filled 1/3→3/3, expiries logged. This is the proof that fix #1 works.
4. The provisional→settled bar transition is exercised across a full year and the settled series matches the official candles exactly.
5. Crash recovery: kill the process between "order submitted" and "response received", restart, and confirm exactly one order exists — recovered by `referenceId`, not re-sent.
6. Every preflight gate is triggered at least once by fault injection and aborts cleanly.
7. Budget rolls across a calendar year boundary, including the empty-prior-year fallback.
8. A mid-run deposit is absorbed correctly with no special handling.
9. **T-10 vs T-30 provisional-close divergence.** Replay the strategy using provisional closes sampled at T-10 and at T-30 against the official close series, and report the divergence in `cone_5_z` and in the resulting trade decisions — the measurement backing the §4.2 signal-fidelity cost.
10. Then a **live demo-account run** against real eToro demo endpoints — the only way to settle the "does the API accept orders 30 minutes before close" unknown.

---

## 11. Build order

**No eToro credential is required until step 5, and no real-money credential until step 12.**
The order below is deliberately re-sequenced from a strict dependency order to front-load the
questions with the cheapest, most reversible answers: prove the free infrastructure works with
nothing but a laptop and a bot token, *then* decide whether the Android platform risk (§1.2,
§12) and the eToro integration are worth building against. See `doc/RUNBOOK_INFRA.md` for the
step-by-step console/CLI instructions behind steps 0–2.

| # | Step | Gate |
|---|---|---|
| 0 | **Telegram smoke test.** Create the bot, obtain a chat id, send one message end to end (`RUNBOOK_INFRA.md` Part C). No credential beyond a bot token — proves the one outbound channel that has no dependency on the phone, the Worker, or eToro (§8.1). | |
| 1 | **Firebase project + Cloudflare Worker.** Mint a Google OAuth token from the service-account JSON, then call FCM HTTP v1 and get back either a message id or a "no such registration token" error — the latter is a **pass**, since no device is registered yet (`RUNBOOK_INFRA.md` Parts A, B, D). **Proves free-tier viability with no phone and no eToro keys.** | **GATE — the question asked first** |
| 2 | Cloudflare Pages web app (static assets + server-side Pages Function proxy, §9.1) reading a **stub** snapshot from the Worker (hand-written JSON, not a real portfolio). Proves the read path — Pages Function → Worker → KV → browser, with the read token enforced server-side and never exposed to the browser — end to end before any real data exists. Requires Cloudflare Access in front of the deployment (`RUNBOOK_INFRA.md` Part F) since the proxy is otherwise a public, unauthenticated endpoint. | |
| 3 | **Android tooling install** — JDK + Android SDK + `adb`. Currently absent from this machine (§12) and explicitly a prerequisite, not assumed; deferred to here so it is only installed once step 1 has proven the cloud side is worth building against. | |
| 4 | Minimal APK: FCM receive + `setAlarmClock()`, nothing else — the same shape as the old gate-zero `WakeProof.ps1` from the Windows design, now proving the Android platform instead of Task Scheduler. Grant `SCHEDULE_EXACT_ALARM` and the battery-optimization exemption, apply the OriginOS checklist (§7.4), disable the OriginOS "Pause app activity if unused" toggle and the stock unused-app permission removal (§1.2). Before this step is called done, also run a **throwaway Chaquopy test build against `compileSdk`/`targetSdk 36`** (§1.2) — a prerequisite check, separate from the alarm proof, that the toolchain itself works on this Android 16 device before any real app code is built on top of it. | **GATE — the multi-night on-battery proof.** Fires on time *and* has network, unplugged, screen off, untouched, for **3+ hours**, on **several consecutive nights**, including once across a reboot. Additionally confirm `RemoteMessage.getPriority()` still reports `PRIORITY_HIGH` after a week of pushes (§8.2 demotion canary) — this is the check that catches Google's 7-day silent-send demotion before it ever costs a live session. |
| 5 | eToro client: auth, rate limiting, retries, all read endpoints, typed models. **First step that needs any eToro key at all**, and only the **demo** pair. | Against **demo** |
| 6 | Market calendar + bar store + seeding (400 daily bars) + provisional/settle | |
| 7 | Strategy port with the four approved bug fixes (§1.4); unit tests against `etoro_trader` fixtures | |
| 8 | Forward-test harness + fake broker; acceptance criteria 1–8 (§10, desktop CPython) | **Gate** |
| 9 | Session engine: preflight (§5), executor, idempotency, reconciliation, sleeve accounting (§4.5) | |
| 10 | Full app integration: Telegram inbound with escalation-only hourly poll and the allowlist (§8.2), snapshot push to the Worker, the web app now reading live data instead of the step-2 stub; embedded FastAPI demoted to LAN-only debug view (§9.5) | |
| 11 | Demo end-to-end **on the phone, on battery**: scheduled wake → real demo orders → back to Doze, several consecutive sessions. **Acceptance criterion, not just a smoke test:** at least one session in this run must occur while the demo account genuinely holds a non-empty position, and `get_portfolio()`'s `clientPortfolio` envelope handling (§1.1, §12) must be confirmed correct against it — a run where every session sees 0 positions does not close this step, per the "check that passes because nothing happened" rule (`doc/HANDOFF.md` §7 rule 1). The manual trade controls (§9.7) are the intended way to get a real position onto the demo account for this without waiting on the strategy's own signal to fire. | **Gate** |
| 12 | Go live: real keys, `NIGHTWATCH_ARM`, a generous `cash_reserve_usd` so only a small sleeve is initially exposed, watch closely | Your call |

**Do not build steps 3–12 on an unproven platform.** If the minimal APK (step 4) cannot reliably
fire with network on battery on this specific handset, no amount of application code fixes it —
that gate is unchanged from the original design, only its number and its position relative to
the cloud-infrastructure proof have moved.

---

## 12. Open risks

The host was chosen **knowing it is the highest-risk combination of the options considered**:
an undocumented vendor OS, running on battery rather than mains, with money at stake and a
once-a-day window that cannot be retried. That was an explicit, informed decision. It is
recorded here as a standing risk, not re-argued.

> **Superseded in part by [`DESIGN_market_data.md`](DESIGN_market_data.md)** (2026-09-04): the two **High** risks below concerning live-rate staleness and undetected bar-history gaps are addressed there (gate 10, §6). The gap risk is **dissolved** rather than mitigated — re-derivation makes a gap inexpressible — so its 're-seed from official candles' mitigation no longer applies as written.

| Risk | Severity | Mitigation |
|---|---|---|
| **Chaquopy / API 36 compatibility is unverified.** Chaquopy 17.0.0 supports AGP 7.3-9.2 (which structurally covers `compileSdk 36`), but no release note or changelog entry confirms Android 16 / API 36 has been tested. An embedded-CPython failure would invalidate the core premise that the Python strategy ports over largely unchanged. | Medium | Ship `compileSdk 36` / `targetSdk 35` so an unverified toolchain dependency is not coupled to a target bump. A throwaway Chaquopy test build against `compileSdk`/`targetSdk 36` is an explicit prerequisite in §11 step 4. Fallback if it fails: stay on `compileSdk 35`. |
| **vivo's undocumented background killing on battery** — dontkillmyapp rates vivo 3/5 and states restrictions *"have not been fully uncovered yet"*, and that coverage is Funtouch-only, not updated for the device's actual OriginOS 6; developers report alarms failing on vivo hardware even with every known Funtouch-era setting applied, and no verified reports exist either way for OriginOS 6 | **High** | §11 step 4 multi-night proof on battery before anything else; all five OriginOS settings (§7.4); foreground service for the run; daily Telegram heartbeat so a silent kill is detected the same morning. **There is no way to make this risk go away — only to detect it fast.** |
| **Battery-optimization exemption revoked** (by an OS update, a "battery saver" sweep, App Hibernation, or the user) — silently removes network in the Rare/Restricted buckets | **High** | Preflight gate 2 checks it every run and fails loudly with the exact Settings path. Detected within one session. |
| **App Hibernation force-stops the app and strips its permissions** after months of the user never opening it — the trigger is lack of *user interaction*, not lack of background activity, and this app is deliberately never opened by design (§1.2). OriginOS layers its own "Pause app activity if unused" toggle on top. Whether a successful unattended run counts as qualifying "usage" on stock AOSP is undocumented, and is certainly undocumented for the OriginOS toggle | **High** | Disable both the OriginOS toggle and the stock unused-app permission removal at setup (§7.4). Preflight gate 2 already checks both affected permissions every session and will detect revocation — **detection, not prevention**; the daily heartbeat surfaces it the same morning. |
| **OS upgrade resets power-management settings** — the Z10 is early in its support life and *will* take major upgrades | **High** | Runbook: re-verify all five OriginOS settings **and re-run a one-night proof** after every OS update. Host panel shows the last-verified date. |
| **`SCHEDULE_EXACT_ALARM` revoked** — *"your app stops, and all future exact alarms are canceled"* | **High** | `ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED` receiver + `canScheduleExactAlarms()` in preflight gate 2. Never silently downgrade to an inexact alarm. |
| **Orders near the close may behave differently than documented** | **High** | Step 11 on demo settles it; status 11 handled either way. |
| **Live-rate staleness check is unimplemented** — `Rate.timestamp` (the live `date` field, §1.1) exists but `market/bars.py` never reads it, deferred twice pending preflight (§5 gate 10 note, §6.G). A live probe with the market closed caught SCHG timestamped to the prior session's close while SPY's was current — the same moment, two managed-set instruments, different staleness: a real condition, not a hypothetical one. | **High** | **Concrete gate spec.** Preflight gate 10 rejects a rate whose `date` timestamp is older than a configured limit while the market is open, using the same "now" reference the calendar gate (§5 gate 9) already establishes. On failure it takes the standard gate-failure path (§5): abort before any order, hold the wake lock, alert over Telegram (§8) naming the stale symbol and its age — never a silent skip or a fallback to a cached price. Additionally, persist the rate's own `date` on the bar row (`is_provisional` bars, §4.2) alongside the value it produced, so staleness is auditable from the DB after the fact rather than only inferable from logs at the moment it happened. Build before step 9 is considered done. |
| **A bar-history gap is currently caught only by a stretched tolerance, not detected as a gap** — `market/bars.py`'s bad-tick guard (§6.G) widens multiplicatively with elapsed sessions, capped at 10x base (200% SPY/SCHG, 600% UPRO), past which only an order-of-magnitude error still trips it. `missing_trading_days()` exists as the diagnostic but nothing calls it to halt+reseed. | **High** | **Concrete gate spec.** Preflight gate 10 calls the existing `missing_trading_days()` over the required-history window (§1.4's ≥265-bar SPY requirement); any gap halts the session, alerts over Telegram (§8) the same as every other gate failure (an `ERROR` log alone is not sufficient — the phone lives away from the user), and **re-seeds from official candles** before trading resumes — the candles endpoint serves up to 1000 daily bars per request (§1.1, live-verified 2026-09-04), so a full re-seed is cheap and authoritative, not a partial patch. **Follow-up tied to this gate, not immediate:** once gap detection and re-seed exist, the bad-tick guard's multiplicative tolerance stretching (§6.G) exists only to survive gaps that this gate will now catch directly — its 10x cap should be revisited and tightened at that point. A widened band that no longer needs to be wide is just a weaker guard; do not tighten it before the gate lands, or gaps go undetected in the interim. |
| **`get_portfolio()`'s `clientPortfolio` envelope handling is unverified against a genuinely non-empty portfolio** — every live probe so far ran against a demo account with 0 positions, which passes regardless of whether the envelope is unwrapped correctly. Step 9's reconciliation (§5 gate 11) treats an unmatched managed-symbol position as a hard preflight failure, so a wrong envelope read there is not a cosmetic bug. | Medium | **Scheduled, not floating — operator decision 2026-09-04.** Verification is deliberately postponed rather than manufactured: no synthetic position is opened early just to exercise the parser. It is now a named acceptance criterion of §11 step 11 ("demo end-to-end on the phone") — see the step 11 row below — because step 11 is the first point in the build order where the demo app is deployed on the phone and can hold a real, non-empty position under normal operation. This does **not** block step 9 from being *built*: the reconciliation code (gate 11) can and should be written and unit-tested against fixtures in step 9. It does block step 9's reconciliation from being *trusted* — a 0-position pass is exactly the "check that passes because nothing happened" pattern (`doc/HANDOFF.md` §7 rule 1) — until step 11 closes this row. |
| **A missing tz database makes `market/calendar.py` unimportable** — `EASTERN = ZoneInfo("America/New_York")` runs at module scope, so a missing tz database raises before `main()` and before any preflight gate runs, producing a bare traceback instead of the abort-hold-wakelock-notify behavior §5 promises for every other failure. Preflight gate 9 covers "not a trading day," not "the calendar module failed to import." | Medium | `tzdata` is pinned in `requirements.txt` for desktop CPython (Windows ships no system tz database; Android resolves `ZoneInfo` via the APEX mainline module, so this is a desktop-only exposure). Wrap the import at the app's entry point so an import failure still produces the graceful abort path, not a bare traceback. |
| **No Android build tooling on the development machine** — no JDK, no Android SDK, no `adb` installed as of this writing | Medium | Recorded as an explicit prerequisite gap, not assumed away. §11 sequences the Firebase/Cloudflare infrastructure proof (steps 0–2) *before* this install (step 3) specifically so the tooling install is deferred until it's known to be worth doing. Installing it is mechanical (Android Studio or SDK Studio command-line tools + a JDK) but is real setup time, not yet spent. |
| **Running on battery rather than mains** — a flat phone is a missed session with no alert | Medium | Preflight gate 2 aborts below 40% and says so; daily heartbeat makes a dead phone obvious; charge cap and periodic inspection for the opposite failure. |
| **Battery swelling** from long periods on charge; 7300 mAh cell | Medium | 80% charge cap if available on OriginOS 6 (*unverified — whether the Funtouch-era path carries over is unconfirmed, community-sourced, confirm on device*); periodic physical inspection; replace at ~3 years. A swollen cell is a fire risk. |
| UPRO may not be tradable on your eToro entity | Medium | Preflight gate 8 — `allowOpenPosition` is authoritative. |
| Strategy is **unstudied** — no study results exist for this variant in `etoro_trader` | Medium | etoroNightwatch executes it faithfully; it does not validate the edge. Start with a small budget override. |
| Inbound commands lag if FCM is demoted before the hourly escalation poll kicks in | Low | FCM push makes the common case near-instant; escalation (§8.2) bounds worst-case lag to an hour and de-escalates automatically. Outbound alerts are unaffected and instant. |
| Live signal uses a provisional close; backtest used the official one | Low–Medium | Inherent to trading before the close. Quantified by the forward test. |
| **Cloudflare Worker outage, or the phone's push failing to reach it** | Low–Medium | The on-device `setAlarmClock()` trigger and the daily session (§7.2) are independent of the Worker — trading is unaffected. Telegram remains the reliable away-channel (§8). Only the on-demand web view is degraded; the LAN debug FastAPI view (§9.5) is a fallback if on the same Wi-Fi. |
| **Stale snapshot on the web app** — the phone was unreachable when a refresh was requested | Low | The web app shows an explicit "as of" timestamp (§9.5) so staleness is visible, never assumed away; on-demand refresh (§9.1) retries via the same FCM path already relied on for inbound commands. |
| **A deposit above the reserve silently enlarges the sleeve** — the mirror-image risk to the one exclusion (§4.5, operator decision 2026-09-04) just fixed. Under the old inclusion model, cash deposited for purposes unrelated to the strategy was invisible to the app by construction. Under exclusion, `sleeve_cash = max(0, broker_available_cash − cash_reserve_usd)`, so **any deposit above the reserve is immediately tradable** — it enlarges the sleeve, and therefore the budget and order sizes, with no explicit act by the operator. Real for an app that trades unattended on a phone, once a day, with no human in the loop to notice before the next session acts on it. | Medium–High | **Accepted, deliberately unmitigated in code.** A `max_sleeve_cash_usd` ceiling (`sleeve_cash = min(max_sleeve_cash_usd, max(0, broker_available_cash − cash_reserve_usd))`) was put to the operator on 2026-09-04 and **declined**: reserve-only is the chosen model, because a ceiling has a failure mode of its own — it must be raised by hand as the account grows, and if it isn't, the strategy silently stops scaling, which is the same class of quiet degradation exclusion was adopted to escape. Do not re-propose it unless the operator reopens the question. The mitigation is therefore operational, not structural: raise `cash_reserve_usd` by a deposit's amount before or immediately after it lands, same as any other config edit (§4.4), and treat every unexpected snapshot/status jump in `sleeve_cash` as a signal to check the account for an unplanned deposit. The sleeve figure and its reserve are on the status/snapshot panel (§9) precisely so this is visible without asking. |
