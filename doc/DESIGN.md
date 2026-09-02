# etoroNightwatch — Live eToro Trading & Portfolio App

**Status:** design, pending approval. Nothing built yet.
**Date:** 2026-09-02
**Host:** vivo iQOO Z10, Android 15 / Funtouch OS, Jakarta (UTC+7). Native Android app with an
embedded CPython runtime via **Chaquopy**. Often on battery, not permanently charged. Doubles
as a small always-reachable personal server, contacted over Telegram.

etoroNightwatch wakes the phone in the Jakarta small hours, verifies it is safe to trade, runs
the Std Band Ladder Cash Recycle strategy against live US market prices **thirty minutes before
the close**, executes on eToro, reports to Telegram, and lets the device fall back into Doze. A
FastAPI webapp embedded in the app shows the whole portfolio from any device on the same LAN.

The phone is woken by **three independent paths** — an on-device `setAlarmClock()`, a
cloud-scheduled FCM high-priority push, and a manual command — converging on **one idempotent
session** (§6.2).

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
| Minimum trade size is **per-instrument**, from `POST /api/v2/trading/info/eligibility` (`minPositionExposure`, `minPositionAmount`). Not a constant. | Query at preflight. Never hardcode $10. |
| Rate limits per user key, 1-min window: reads 60/min, writes 20/min, market-data 120/min, eligibility 20/min dedicated. | Ample for a session. Throttle the webapp during the trading window. |
| Candles: `GET /api/v1/market-data/instruments/{id}/history/candles/{dir}/{interval}/{count}`, `OneDay`, up to 1000. | Our history source. Self-contained — no Snowflake dependency. |
| Live prices: `GET /api/v1/market-data/instruments/rates?instrumentIds=…` → `bid`/`ask`/`lastExecution`. **No market-open flag.** | Source of today's provisional close. Market-open comes from our own calendar. |
| Instrument ids are **immutable**. | Resolve once via `market-data/search?internalSymbolFull=UPRO`, cache forever. |

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
| `setAndAllowWhileIdle()` / `setExactAndAllowWhileIdle()` *"cannot fire alarms more than once per nine minutes, per app."* | Would have constrained a polling cadence. **Mostly moot** — inbound control is push-driven, not polled, in the normal case (§7.2). The daily safety-net alarm uses this family once a day; the escalation-only hourly Telegram poll (§7.2) also uses it, but only while escalated, at 1 hour — comfortably above the 9-minute floor. |
| Doze names **FCM high-priority messages** as a wake mechanism: *"FCM high priority messages let you wake your app to engage the user. In Doze or App Standby mode, the system delivers the message and gives the app temporary access to network services and partial wakelocks, then returns the device or app to the idle state."* (https://developer.android.com/training/monitoring-device-state/doze-standby) | The inbound control channel and the redundant daily trigger (§7.2, §6.2). Note what is granted: **network + partial wakelock, temporarily**, at delivery time. |
| AOSP names telephony as the *other* Doze-exempt wake path: *"MMS/SMS/telephony services function independently of Doze and always wake the client apps even while the device remains in Doze mode."* … *"Apps receiving SMS and MMS messages are put on a temporary whitelist so that they can complete their processing."* (https://source.android.com/docs/core/power/platform_mgmt) | Technically the strongest fallback — and **rejected on cost**, not on capability. Recorded with its reasoning in §7.2 so the trade-off is not silently re-litigated. |
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

#### vivo / iQOO — stated plainly

This is the weakest link and the user chose it knowingly (§11).

- dontkillmyapp.com/vivo rates vivo **3/5** and states: *"System restrictions on Vivo phones have not been fully uncovered yet."*
- There is **no vivo developer documentation** on background execution restrictions. Behaviour is reverse-engineered by the community, not specified.
- Developers report alarms failing on vivo devices **even with autostart enabled, battery optimization disabled, and the app locked in Recents** (https://github.com/WrichikBasu/ShakeAlarmClock/discussions/61). Alarmy maintains a dedicated vivo troubleshooting article.
- Contrast, honestly: Samsung is **more aggressive by default** but **documents its off switch**. vivo is less aggressive on paper and documents nothing. Undocumented is worse than aggressive-but-known, because it cannot be verified — only observed.

Required vivo settings (all of them, §6.4):

1. Autostart enabled — Settings → More settings → Applications → Autostart, or i Manager → App manager → Autostart manager
2. Battery usage → **Unrestricted**
3. Allow high background power consumption
4. Disable battery optimization (the `ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS` exemption)
5. Lock the app in Recents

**Support-life caveat.** The iQOO Z10 is early in its support window and **will receive major
OS upgrades**, each of which can silently reset power-management settings. This is the
opposite of the usual "run it on an EOL phone" advice, and it is a real recurring cost:
**verify every setting after every OS update** is a runbook item, not a footnote.

#### Battery health

Funtouch OS exposes a charge limit under **Settings → Battery → Battery health and charging**
(80% cap). *Community-sourced and unverified:* availability is uneven across Funtouch builds
and some users report the toggle resetting after a restart. This is not vendor documentation
and must be confirmed on the actual device.

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
    build.gradle.kts             # Chaquopy plugin, python { pip { install(...) } }
    src/main/AndroidManifest.xml # SCHEDULE_EXACT_ALARM, RECEIVE_BOOT_COMPLETED,
                                 # WAKE_LOCK, INTERNET, ACCESS_NETWORK_STATE,
                                 # POST_NOTIFICATIONS,
                                 # FOREGROUND_SERVICE(+_DATA_SYNC),
                                 # REQUEST_IGNORE_BATTERY_OPTIMIZATIONS
    src/main/java/.../nightwatch/
      MainActivity.kt            # setup/status UI, permission funnel, vivo checklist
      AlarmScheduler.kt          # setAlarmClock() for the session; daily safety-net alarm;
                                 # escalated hourly Telegram poll alarm (failure case only)
      SessionAlarmReceiver.kt    # fires → starts SessionService
      SafetyNetReceiver.kt       # daily unconditional schedule repair
      BootReceiver.kt            # RECEIVE_BOOT_COMPLETED → re-register alarms
      ExactAlarmPermissionReceiver.kt  # ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED
      NightwatchMessagingService.kt    # FirebaseMessagingService: onMessageReceived does the
                                 # work inline; getPriority() demotion canary; HMAC verify;
                                 # posts a visible notification for every command (§7.2)
      TelegramPollReceiver.kt    # escalation-only hourly poll; normally NOT scheduled (§7.2)
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
      web/                       # app.py, routers/, templates/, static/
      host.py                    # thin bridge: Kotlin ⇄ Python entry points
      cli.py                     # run | seed | webapp | status | halt  (desktop dev + tests)
  config/
    config.toml                  # strategy params, symbols, budget override, mode
    .secrets.env                 # API keys — gitignored, never logged
                                 # on-device: app-private storage, not external
  data/nightwatch.db             # SQLite, WAL — app-private filesDir on device
  logs/log_2026-09-02.txt        # one file per day, app-private
  doc/DESIGN.md  doc/RUNBOOK.md
  forward_test/
    replay.py                    # time-travel harness over the LIVE engine
    fake_broker.py               # eToro semantics: async fills, positionIds, min size
  tests/
```

Everything under `src/main/python/nightwatch/` runs unmodified on a desktop CPython for
development, unit tests and the forward test. Only `host.py` differs in what calls it.

---

## 3. Data model (SQLite, WAL)

| Table | Purpose |
|---|---|
| `bars` | `(symbol, date, o,h,l,c,v, is_provisional, source)` PK `(symbol,date)`. **`is_provisional` is the key column** — see §4.2 |
| `instruments` | ticker → `instrument_id`, cached metadata, last eligibility snapshot |
| `sessions` | one row per trading date: state machine, preflight verdict, timings, outcome. **Unique on `trading_date`** — cannot double-run a day |
| `preflight_checks` | per-check result rows — full audit of why a run aborted |
| `decisions` | per-session `cone_z`, `spy`, `ath`, `drawdown`, `tier`, intents JSON, reason |
| `intents` | one row per `OrderIntent` **written before any HTTP call**, with its `x-request-id` UUID — the idempotency anchor |
| `orders` | eToro `orderId`, `referenceId`, status history, terminal status, fill price/units |
| `lots` | the ladder book: units, entry price, `entry_reference_spy`, `cost_basis`, `position_id`, `book`, `entry_spy_ath/close/drawdown`, exit fields |
| `buckets` | `bucket_id, sale_date, sale_spy_close, proceeds, tranche_amount, tranches_bought, last_reference_spy, created_bar` |
| `strategy_state` | singleton: `bars`, `ath`, `below_streak`, `recyclable_cash`, `budget_year`, `budget`, `order_size`, `cash_target`, `sale_batch_sequence`, `sale_batch_date` |
| `schg_daily_values` | `(date, value)` — feeds next year's budget |
| `budget_history` | `(year, budget, order_size, cash_target, source: auto\|override)` |
| `portfolio_snapshots` | per-session equity, cash, available cash, unrealized P&L, per-position rows |
| `notifications` | Telegram send log with delivery status |
| `commands` | inbound control audit: command text, `channel` (`fcm`\|`telegram`), sender/chat id, HMAC counter (replay rejection), verdict (accepted/rejected + reason), timestamp — an inbound control channel must be auditable |

---

## 4. The daily session

### 4.1 Sequence

```
T-15min  setAlarmClock() fires → SessionAlarmReceiver → SessionService (foreground)
         Acquire PARTIAL_WAKE_LOCK (released in finally, §6.6)
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
  [6]    DECIDE — strategy.decide() → OrderIntents  (persisted to `decisions`)
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
unmeasured assumption: the forward test (§9) replays the strategy using provisional closes
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

### 4.4 Extra deposits

Your assumption is right, and here is precisely why. Equity and cash are read live from the
API each session. The budget is struck **annually** from the prior year's mean SCHG value, so
a mid-year deposit does not move it — matching backtest semantics exactly. The new cash sits
in the account, raises `equity`, and is swept into SCHG on the next new-ATH bar by the
existing surplus rule. No special service needed.

**One consequence worth knowing:** a large deposit will be moved into SCHG in a single sweep
on the next new-ATH bar. If you would rather stage that, deposit in tranches or set a
temporary budget override. Flagging, not changing — this is existing behaviour.

### 4.5 Budget: auto with override

Auto (the backtest rule): `budget = floor(0.4 × prior_year_mean_SCHG_value / 20) × 20`;
first year `0.4 × equity`. Then `order_size = max(budget/60, 10)`, `cash_target = 8 × order_size`.

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
| 10 | **Price data** | < 265 settled SPY bars; any unsettled provisional bar; stale/zero live rate |
| 11 | **Reconciliation** | Broker positions and DB lots disagree (unknown position, or a lot with no position) |
| 12 | **Risk caps** | Session order count or notional exceeds configured caps; daily loss guard tripped |

**Gate 2 deserves emphasis.** The two host permissions it checks are the ones Android can take
away without telling us. Losing the battery-optimization exemption silently disables the
network in the Rare/Restricted buckets; losing `SCHEDULE_EXACT_ALARM` **stops the app and
cancels every future exact alarm**. Both are checked on every run and both fail **loudly** to
Telegram, naming the exact Settings path to restore them. A quiet degradation here would look
identical to "the market was closed."

### Safety beyond preflight

- **Two-key arming.** Live execution needs `mode = "live"` in config **and** the `NIGHTWATCH_ARM` flag. Neither alone is sufficient.
- **Separate credentials by privilege.** A Real+**Read** key for the webapp; a Real+**Write** key used only by the session service; a Demo pair for the forward test. Use eToro's IP-whitelist and expiry options on all of them.
- **Idempotency.** The `x-request-id` UUID is persisted to `intents` **before** the HTTP call and doubles as eToro's `referenceId`. If the response is lost (crash, network drop, process death), recovery looks the order up by `referenceId` rather than re-sending. This is the difference between a lost response and a double buy.
- **One session per date**, enforced by a unique constraint, backed by the "already ran today" flag the safety-net alarm consults.
- **Never return to Doze on failure** — an aborted run holds the wake lock so the device stays reachable.

---

## 6. Host integration — Android (iQOO Z10)

### 6.1 App architecture

| Component | Language | Responsibility |
|---|---|---|
| `MainActivity` | Kotlin | Setup and status UI. Permission funnel: `SCHEDULE_EXACT_ALARM`, battery-optimization exemption, notification permission. Renders the vivo settings checklist (§6.4) with a "verified on" date. |
| `AlarmScheduler` | Kotlin | Computes nothing itself — asks Python for the next fire instant, then calls `setAlarmClock()`. Owns the daily session alarm and the daily safety-net alarm unconditionally; registers the hourly Telegram poll alarm **only while the app is in the escalated state** (§7.2), and cancels it on de-escalation. |
| `SessionAlarmReceiver` | Kotlin | Fires on the session alarm, immediately starts `SessionService`. Does no work in the receiver. |
| `SessionService` | Kotlin | Foreground service (`dataSync`) with a persistent notification, so Android will not kill the ~15 minutes of run. Acquires the partial wake lock, invokes the Python entry point, releases in `finally`. |
| `BootReceiver` | Kotlin | `RECEIVE_BOOT_COMPLETED` → re-register the session alarm and the daily safety-net alarm; re-register the escalation poll only if persisted state says the app is currently escalated. |
| `ExactAlarmPermissionReceiver` | Kotlin | `ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED` → re-register alarms when the permission is granted back; alert to Telegram when it is lost. |
| `TelegramPollReceiver` | Kotlin | Escalation-only. When scheduled, hourly `setExactAndAllowWhileIdle()` → drains the Telegram update queue and checks the de-escalation condition (§7.2). Normally not scheduled at all. |
| `nightwatch/*` | Python (Chaquopy) | Everything else: config, SQLite, eToro client, calendar, strategy, engine, notify, web. Identical to the desktop tree. |

The division is deliberate: **Kotlin does only what Python cannot reach** — alarms,
permissions, wake locks, boot, service lifecycle. All trading logic, all state, all decisions
stay in Python where they can be forward-tested off-device.

### 6.2 Alarm scheduling and DST

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

### 6.3 The mandatory exemptions

| Permission / exemption | How | Why it is not optional |
|---|---|---|
| `SCHEDULE_EXACT_ALARM` | User grant: Settings → Apps → Special app access → Alarms & reminders | Without it, no exact alarm at all. Revocation **stops the app and cancels all future exact alarms.** |
| Battery-optimization exemption (`ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS`) | One-time system dialog from `MainActivity` | Grants network access and partial wake locks during Doze and App Standby. **Without it the app has no network in the Rare/Restricted buckets and cannot trade at all.** |
| `RECEIVE_BOOT_COMPLETED` | Manifest | Alarms do not survive a reboot on their own. |
| `WAKE_LOCK` | Manifest | Holds the CPU through the run once already awake. |
| Foreground service (`dataSync`) + notification permission | Manifest + runtime grant | Keeps the ~15-minute run from being killed mid-order. |

Both user-granted items are re-verified in **preflight gate 2** on every run.

### 6.4 vivo settings checklist

None of this is documented by vivo. It is community-derived and must be **verified by
observation**, then re-verified after every OS update.

| # | Setting | Path |
|---|---|---|
| 1 | Autostart **enabled** | Settings → More settings → Applications → Autostart, or i Manager → App manager → Autostart manager |
| 2 | Battery usage **Unrestricted** | Settings → Battery → App battery usage |
| 3 | **Allow high background power consumption** | same screen |
| 4 | Battery optimization **disabled** for the app | the exemption dialog in §6.3 |
| 5 | App **locked in Recents** | Recents → app card → lock |

Reports exist of alarms failing on vivo hardware **even with all five applied**
(https://github.com/WrichikBasu/ShakeAlarmClock/discussions/61). That is why §10 step 0 is an
empirical multi-night proof and not a paperwork exercise.

**After every OS update, re-verify all five and re-run a one-night proof before trusting the
next live session.** The Z10 is early in its support life and will take major upgrades.

### 6.5 Boot survival

`BootReceiver` on `RECEIVE_BOOT_COMPLETED` re-registers the session alarm and the daily
safety-net alarm unconditionally, and re-registers the escalation-only hourly Telegram poll
**only if persisted state says the app is currently escalated** (§7.2) — a reboot must not
itself turn on a poll that was not running before it. **This must be tested explicitly**:
reboot the phone, then confirm the job actually fires afterwards, before any real money is
involved. On vivo, autostart being disabled will silently prevent the receiver from running at
all — which is exactly the failure mode that looks like nothing happened.

### 6.6 Battery strategy and returning to Doze

- The alarm gets us **running**; the partial wake lock only keeps us running. A wake lock does **not** escape Doze — Doze *"ignores wake locks."* Acquire after the service starts, release in a `finally` block, always.
- Total awake time per session is roughly 15 minutes, of which ~60 seconds is actual API traffic.
- **In the normal case, no Telegram polling alarm is scheduled at all** — inbound control rides FCM push, which costs no dedicated alarm or standing connection of our own (§7.2). Only on escalation (FCM demoted, token invalid, or no FCM message for N days) does `AlarmScheduler` register a repeating `setExactAndAllowWhileIdle()` hourly poll, cancelled again the moment de-escalation is detected. This keeps the battery cost of inbound control at zero in the common case and bounded to one poll/hour only in the failure case. At 1 hour the cadence is comfortably above the documented 9-minutes-per-app floor for `*AndAllowWhileIdle` alarms.
- On a clean finish: reschedule, release the wake lock, stop the foreground service, and **let the device return to Doze naturally**. Nothing is forced.
- On an abort: hold the wake lock and stay out of Doze so the device is reachable.
- Charging: use the Funtouch 80% charge cap if the build exposes it (*unverified*, §1.2), inspect the cell periodically, plan replacement at ~3 years.

---

## 7. Telegram — the primary interface

Telegram is the reliable away-channel. The dashboard (§8) is not; it depends on the phone being
on Wi-Fi and allowed to run.

### 7.1 Outbound

One message per event, all tagged with the trading date:

- **Session start** — fired at X, target Y, mode live/dry-run
- **Preflight** — pass, or the specific gate that failed and why
- **Decision** — `cone_z`, SPY, drawdown, tier, budget, cash, and the intents (or "hold" + reason)
- **Each fill** — symbol, side, USD/units, price, order id
- **Session end** — positions, equity, cash, P&L, next scheduled run
- **Any abort or exception** — full reason, and that the device is staying awake
- **Daily heartbeat, even on no-trade days** — "ran, nothing to do, next run at X"

The heartbeat is not decoration. Without it, a killed app and a quiet market look identical.
**With it, silence is unambiguously a failure.** Given §11's top risk, this is the single most
valuable monitoring feature in the design.

Sends are logged to `notifications` with delivery status, so a *failed* alert is itself visible.
Outbound sends **do not depend on any inbound polling at all** — an alert goes out the instant
it is generated, via a direct Telegram `sendMessage` call, with no `getUpdates` cycle involved
on the sending path. If Telegram is unreachable the session still completes; it does not become
a trading failure.

### 7.2 Inbound — read-only plus halt

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

**Explicitly not available: config changes, arming live mode, manual trades, budget changes.**

The rationale is one sentence: **a compromised Telegram account must never be able to move
money.** Every allowed command either reads state or *stops* trading. `HALT`/`RESUME` are safe
precisely because halt can only ever prevent a trade, and resume only restores the normal path —
which still requires both arming keys (§5) that Telegram cannot touch.

Security:

- **Allowlist on Telegram chat id.** Any message from any other chat id is ignored and logged, never answered. FCM messages are addressed to our own registration token, which is the equivalent binding for that channel.
- **HMAC-of-(command, counter), with replay rejection by counter.** Every inbound command, on either channel, carries an HMAC over the command and a monotonic counter; a counter at or below the last-accepted value is rejected as a replay. Every attempt — accepted or rejected, and why — is written to the `commands` audit table (§3): command, channel (`fcm`\|`telegram`), sender/chat id, counter, verdict, timestamp. An inbound control channel must be auditable.
- Commands are parsed as **data against a fixed table**. Nothing from a message is ever passed to a shell, a file path, or an eval.
- **No inbound listener, no open port to the internet, on either channel.** FCM delivery is a push from Google's infrastructure to Google Play Services, not a socket we open; Telegram is `getUpdates` long-polling, never a webhook.

---

## 8. Webapp (FastAPI, embedded via Chaquopy)

The same FastAPI app runs **inside the Android process** on the Chaquopy interpreter, bound to
`0.0.0.0` on a port **above 1024** (8000 or 8080 — binding below 1024 requires root). It is
reachable from any device on the same LAN at `http://<phone-ip>:8080`.

Practical requirements:

- A **DHCP reservation or static IP** for the phone, so the URL is stable.
- **Wi-Fi set to stay connected when idle** (Settings → Wi-Fi → advanced), or the server is unreachable the moment the screen sleeps.
- Read-only eToro key. Throttled during the trading window so it cannot eat the API budget.

**Honest limitation.** On battery, the server is only reachable while the app is allowed to
run. Doze, App Standby and vivo's own killer can all take it away without warning. Treat the
dashboard as a **"when on Wi-Fi / on charge" convenience**; **Telegram remains the reliable
away-channel.** Do not design any operational dependency on the dashboard being up.

Contents (server-rendered pages plus a small JSON API; TradingView lightweight-charts for the
chart):

- **Dashboard** — equity, cash, available cash (computed with eToro's documented formula: `credit − Σ pending order amounts`, since `totalBalance` is portfolio value, not cash), unrealized P&L, budget, `order_size`, `cash_target`, `recyclable_cash`, and today's `cone_z` / drawdown / tier.
- **Positions table** — every field the API exposes: entry price, units, invested, leverage, unrealized P&L, open date.
- **Trade history table** — from `/trading/info/trade/history` joined to our lots, so each trade shows *why* it happened (`cone_z`, tier, book, bucket id). History lookback is capped at just under 1 year per request; we page and cache.
- **Chart** — SPY with the cone bands and z-score, **trade markers overlaid** for every buy and sell, click-through to the decision that produced it.
- **Strategy state** — open lots by book, live re-entry buckets with tranche progress and expiry countdown.
- **Sessions & logs** — every session, its preflight verdict, and the day's log file.
- **Host panel** — battery level, charging state, exemption status, `canScheduleExactAlarms()`, next alarm, last boot, date the vivo checklist was last verified.

*Overlay note (carried from etoro_trader):* chart overlays are registered indicators, never
strategy-specific metadata.

---

## 9. Forward test — the acceptance gate

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

## 10. Build order

Nothing touches real money until step 9 passes on demo.

| # | Step | Gate |
|---|---|---|
| 0 | **Android platform proof.** A minimal APK — nothing but `setAlarmClock()`, a boot receiver, and a job that hits an HTTPS endpoint and logs the result. Grant `SCHEDULE_EXACT_ALARM` and the battery-optimization exemption, apply the vivo checklist, then **leave the phone on battery overnight** and confirm the alarm fires **on time** *and* **has working network**, for **several consecutive nights**, including one across a reboot. | **Blocks everything** |
| 1 | Gradle + Chaquopy scaffold: config, SQLite schema, logging, `host.py` bridge, desktop CLI parity | |
| 2 | eToro client: auth, rate limiting, retries, all read endpoints, typed models | Against **demo** |
| 3 | Market calendar + bar store + seeding (400 daily bars) + provisional/settle | |
| 4 | Strategy port with the four fixes; unit tests vs `etoro_trader` fixtures | |
| 5 | Forward-test harness + fake broker; criteria 1–8 (desktop CPython) | **Gate** |
| 6 | Session engine: preflight, executor, idempotency, reconciliation | |
| 7 | Telegram: outbound events, heartbeat, FCM-driven inbound with escalation-only hourly poll and the allowlist | |
| 8 | Webapp embedded + host panel; DHCP reservation, Wi-Fi-when-idle | |
| 9 | Demo end-to-end **on the phone, on battery**: alarm → real demo orders → back to Doze, several consecutive sessions | **Gate** |
| 10 | Go live: real keys, `NIGHTWATCH_ARM`, small budget override, watch closely | Your call |

Step 0 is the same kind of gate-zero the Windows design gave to `WakeProof.ps1`, and it
replaces it. **Do not build steps 1–10 on an unproven platform.** If the minimal APK cannot
reliably fire with network on battery on this specific handset, no amount of application code
fixes it.

---

## 11. Open risks

The host was chosen **knowing it is the highest-risk combination of the options considered**:
an undocumented vendor OS, running on battery rather than mains, with money at stake and a
once-a-day window that cannot be retried. That was an explicit, informed decision. It is
recorded here as a standing risk, not re-argued.

| Risk | Severity | Mitigation |
|---|---|---|
| **vivo's undocumented background killing on battery** — dontkillmyapp rates vivo 3/5 and states restrictions *"have not been fully uncovered yet"*; developers report alarms failing even with every known setting applied | **High** | §10 step 0 multi-night proof on battery before anything else; all five vivo settings (§6.4); foreground service for the run; daily Telegram heartbeat so a silent kill is detected the same morning. **There is no way to make this risk go away — only to detect it fast.** |
| **Battery-optimization exemption revoked** (by an OS update, a "battery saver" sweep, or the user) — silently removes network in the Rare/Restricted buckets | **High** | Preflight gate 2 checks it every run and fails loudly with the exact Settings path. Detected within one session. |
| **OS upgrade resets power-management settings** — the Z10 is early in its support life and *will* take major upgrades | **High** | Runbook: re-verify all five vivo settings **and re-run a one-night proof** after every OS update. Host panel shows the last-verified date. |
| **`SCHEDULE_EXACT_ALARM` revoked** — *"your app stops, and all future exact alarms are canceled"* | **High** | `ACTION_SCHEDULE_EXACT_ALARM_PERMISSION_STATE_CHANGED` receiver + `canScheduleExactAlarms()` in preflight gate 2. Never silently downgrade to an inexact alarm. |
| **Orders near the close may behave differently than documented** | **High** | Step 9 on demo settles it; status 11 handled either way. |
| **Running on battery rather than mains** — a flat phone is a missed session with no alert | Medium | Preflight gate 2 aborts below 40% and says so; daily heartbeat makes a dead phone obvious; charge cap and periodic inspection for the opposite failure. |
| **Battery swelling** from long periods on charge; 7300 mAh cell | Medium | Funtouch 80% charge cap if available (*unverified — community-sourced, confirm on device*); periodic physical inspection; replace at ~3 years. A swollen cell is a fire risk. |
| UPRO may not be tradable on your eToro entity | Medium | Preflight gate 8 — `allowOpenPosition` is authoritative. |
| Strategy is **unstudied** — no study results exist for this variant in `etoro_trader` | Medium | etoroNightwatch executes it faithfully; it does not validate the edge. Start with a small budget override. |
| Inbound commands lag if FCM is demoted before the hourly escalation poll kicks in | Low | FCM push makes the common case near-instant; escalation (§7.2) bounds worst-case lag to an hour and de-escalates automatically. Outbound alerts are unaffected and instant. |
| Live signal uses a provisional close; backtest used the official one | Low–Medium | Inherent to trading before the close. Quantified by the forward test. |
