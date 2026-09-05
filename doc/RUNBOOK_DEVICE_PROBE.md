# Runbook — running the live probe from the `android-probe` APK (2026-09-05)

This is the **APK-wiring** runbook: build the `android-probe` project, install it on the z10,
trigger the read-only probe from its own button, and pull the results back. It complements
[`RUNBOOK_LIVE_WINDOW_2026-09-11.md`](RUNBOOK_LIVE_WINDOW_2026-09-11.md), which documents what each
probe section proves and how to invoke `tools/probe_live_2026-09-11.py` directly from a Python
entry point that already has network + credential access. Use *this* runbook if that direct
invocation path is not available tonight and the `android-probe` app is the only thing on the phone
that can run Python at all.

**Scope tonight: sections A, C, D, E1, F, plus the sharpened E1 per-trade fee audit.**
Section B (UPRO rates-freeze polling) needs an open US market — it is Saturday 2026-09-05, the
market is closed, so B is wired up in the underlying script but **is not requested by this app's
button and will not run**. Section E2 (places one real $50 SCHG order) is **not reachable from
this app at all** — there is no button for it, and the Kotlin bridge (`PyBridge.runLiveProbe`)
never calls it. If E2 is ever wanted, it stays a separate, deliberate, command-line-only decision
per §5 of `RUNBOOK_LIVE_WINDOW_2026-09-11.md` — not something this APK can trigger by accident.

**Section F, added this session:** exercises `market.window` (`fetch_symbol_window`,
`validate_window_completeness`, `compute_fingerprint`, `resolve_provisional_close`,
`assemble_spy_signal`) and `strategy.cone` end-to-end against a **real** Tiingo response and (where
SPY resolves) real eToro SPY candles. Every prior test of these two modules used hand-built
`EodBar`/`IexQuote`/`Candle` fixtures only — this is the first time either has seen a live payload.
Read-only: one Tiingo EOD GET, one Tiingo IEX GET, and (if SPY resolves) one eToro candles GET.
Does not need an open market — daily bars and instrument metadata are static on a weekend.

---

## 0. What got added, in one sentence each

- `android-probe/app/src/main/python/probe_live_2026_09_11.py` — a byte-for-byte copy of
  `tools/probe_live_2026-09-11.py` (read-only, not edited) under a valid Python module name
  (Python identifiers cannot contain hyphens) so Chaquopy can `import` it.
- `android-probe/app/src/main/python/probe/probe_runner.py` — new orchestration module. Calls the
  copied script's `main(["--output-dir", ..., "--sections", "A,C,D,E1"])` (explicitly excluding B),
  then runs the new fee-audit-plus report.
- `app/src/main/python/nightwatch/probes/fee_audit.py` — new, pure/offline-testable module: the
  full per-trade fee audit (see §3 below for what it computes).
- `android-probe/app/build.gradle.kts` — added `httpx`/`exchange_calendars` to the Chaquopy `pip`
  block (section A imports both; neither was previously installed) and pointed Chaquopy's own
  `sourceSets { main { srcDir(...) } }` at this repo's `app/src/main/python`, so `nightwatch.*` is
  importable inside this APK without duplicating the package.
- `PyBridge.kt` / `MainActivity.kt` / `activity_main.xml` — one new button, "Run live probe now (A,
  C, D, E1)", that calls into the above from a background coroutine.
- `tests/test_probes_fee_audit.py` — 21 new offline unit tests for `fee_audit.py`.
- **This session:** `tools/probe_live_2026-09-11.py` / the on-device copy
  `android-probe/app/src/main/python/probe_live_2026_09_11.py` — added section F
  (`section_f_window_assembly`), the first live-payload exercise of `market/window.py` and
  `strategy/cone.py`. `app/src/main/python/nightwatch/probes/live_window.py` — added
  `diff_required_keys`/`PARSER_REQUIRED_KEY_GROUPS`/`PARSER_OPTIONAL_KEYS`, a required-vs-fallback
  key diff (section C's own `diff_keys` over-reports "missing" keys for any field read through an
  `or`-chain). `probe/probe_runner.py` now requests `--sections A,C,D,E1,F`. Also fixed
  `config.py`'s `cone_center_mode` validation (accepted an unimplemented `"mid"` value; now only
  `"close"`).

---

## 1. Build

From `android-probe/`:

```
.\gradlew.bat assembleDebug
```

This was actually run tonight and **succeeded**:

```
BUILD SUCCESSFUL in 1m 29s
49 actionable tasks: 16 executed, 33 up-to-date
```

Output: `android-probe/app/build/outputs/apk/debug/app-debug.apk` (~88 MB — numpy/pandas/
exchange_calendars/httpx all bundled; this is expected, not a regression).

**One real dependency conflict was hit and fixed, not glossed over:** `exchange_calendars==4.13.2`
(this repo's desktop-venv version) requires `numpy>=1.26.4`, but Chaquopy's own package index only
offers a prebuilt `numpy` wheel at **1.26.2** for `cp313`/`arm64-v8a` — there is no newer numpy
wheel to install instead. `exchange_calendars` tightened its own numpy floor starting at version
4.12; **4.11.1** (the newest release before that) has an unconstrained `numpy` requirement and
installed cleanly. The Android build is therefore pinned to `exchange_calendars==4.11.1`, one minor
version behind the desktop venv's `4.13.2` — a genuine, reported version divergence between
desktop and on-device, not an oversight. See the comment in `build.gradle.kts` at that line.

Confirmed packaged inside the built APK (`assets/chaquopy/app.imy`, unzipped and checked):
`probe_live_2026_09_11.pyc`, `nightwatch/etoro/client.pyc`, `nightwatch/probes/fee_audit.pyc`,
`nightwatch/probes/live_window.pyc`, `probe/probe_runner.pyc`.

**What this proves and does not prove:** the APK *builds* and the Chaquopy pip/source-set wiring is
correct enough to package everything needed. It does **not** prove any of this runs correctly under
Chaquopy's embedded interpreter on the actual phone — that is what step 4 onward is for.

---

## 2. Install

```
adb devices
adb install -r android-probe\app\build\outputs\apk\debug\app-debug.apk
```

Same `applicationId` as any prior probe build (`com.nightwatch.etoro`, deliberately the real app's
package name — see `build.gradle.kts`'s own comment) — `-r` reinstalls over whatever is already on
the phone without losing the app's other state (schedules, event log).

---

## 3. Push config + credentials onto the device (no credential is ever hardcoded)

The probe needs `config/config.toml` and `config/.secrets.env` — the **existing** secrets
mechanism, never a new one. It reads them from a directory you push onto the device; nothing in
this app's code contains a key.

**Make a device-bound copy of the config** (a copy, not the repo file, because tonight's run needs
`environment = "real"` — the repo's checked-in `config/config.toml` currently says
`environment = "demo"`, which would point sections C/D/E1 at the demo account instead of the real
one this audit is about):

```
mkdir C:\Users\10102721\Documents\ClaudeFiles\etoroNightwatch\_device_config_tmp
copy config\config.toml _device_config_tmp\config.toml
copy config\.secrets.env _device_config_tmp\.secrets.env
```

Edit `_device_config_tmp\config.toml`'s `[etoro] environment` line to `"real"` (only in this
throwaway copy — never commit this change, and this directory is outside git's tracked tree
anyway).

**Push it onto the device's internal storage** (works without root, via `run-as`, because this is
a debug-signed build):

```
adb push _device_config_tmp\config.toml /data/local/tmp/config.toml
adb push _device_config_tmp\.secrets.env /data/local/tmp/.secrets.env
adb shell "run-as com.nightwatch.etoro mkdir -p files/nightwatch_config/config"
adb shell "run-as com.nightwatch.etoro cp /data/local/tmp/config.toml files/nightwatch_config/config/config.toml"
adb shell "run-as com.nightwatch.etoro cp /data/local/tmp/.secrets.env files/nightwatch_config/config/.secrets.env"
adb shell "rm /data/local/tmp/config.toml /data/local/tmp/.secrets.env"
```

(`/data/local/tmp` is world-readable, so `run-as`'s app-uid `cp` can read from it; the last line
removes the transiently-exposed copy from that shared staging location once it's inside the app's
own private storage.) The app's Kotlin side points the Python probe at
`<app files dir>/nightwatch_config` as `NIGHTWATCH_HOME`, which is why the files must land at
exactly `files/nightwatch_config/config/config.toml` — `nightwatch.host.DesktopHost.config_dir()`
appends `"config"` itself.

**Delete `_device_config_tmp\` afterward** — it holds a copy of your real credentials on local
disk outside the gitignored `config/` directory.

---

## 4. Trigger the run

Open the app on the phone, tap **"Run live probe now (A, C, D, E1, F: read-only, no order ever
placed)"**. The status line updates to `RUNNING`; this can take **several minutes** — do not close
the app. Nothing here can block the UI thread indefinitely: the call runs on a background
coroutine (`Dispatchers.IO`) with its own 25-minute Kotlin-side timeout on top of the Python
script's own per-section thread timeouts (`run_section`, 300s each by default) — if everything
somehow still hung, the UI would report a timeout rather than freeze.

What actually happens, in order:

1. `probe_live_2026_09_11.main(["--sections", "A,C,D,E1,F", "--output-dir", <output_dir>])` — **B
   is never in this list**; E2 is never reachable from this call at all.
2. The new fee-audit-plus report (`nightwatch.probes.fee_audit`, via `probe/probe_runner.py`) —
   full per-trade table, groupings, correlations, dividend-field scan, reconciliation (see §... note
   below).

Every raw payload this run touches is written to a file — **nothing is print-only**, per the hard
requirement that data must survive the run. Progress lines also stream to Android's own logcat
(`adb logcat | grep -E "PROBE:|RUNNER:"`) if you want to watch it live from a second terminal while
it runs.

---

## 5. What each section proves, and roughly how long it takes

| Section | Proves | Roughly how long |
|---|---|---|
| A | numpy/pandas/exchange_calendars/httpx actually import and run under Chaquopy on this phone (not just at build time) | seconds |
| C | The six previously-unverified parsers' assumed field names match a real live payload | seconds |
| D | Sleeve arithmetic re-measured *right now*, diffed against the documented figures | seconds |
| E1 | eToro's own read-only fee-adjacent fields (`get_costs`) plus the original aggregate fee-by-instrument summary | seconds |
| **E1-plus** | **The full per-trade fee audit** — see below | seconds to low tens of seconds (one paged trade-history call, up to 500 rows) |
| **F** | **`market.window`/`strategy.cone` exercised end-to-end against a real Tiingo response (and real eToro SPY candles, if SPY resolves) for the first time** — see below | tens of seconds (2-3 Tiingo calls, 1 eToro candles call) |
| B | *(excluded tonight — needs an open US market)* | not run |
| E2 | *(excluded — not reachable from this app; real money)* | not run |

### F, what it proves and what to send back

- Whether `fetch_symbol_window(SPY)` against the **real** Tiingo API returns a usable window at
  all, and the raw Tiingo EOD/IEX responses that produced it (`section_f_tiingo_eod_raw`,
  `section_f_tiingo_iex_raw`) — saved **before** any parsing, so a field-name surprise is provable
  from the file, not just from this session's own report of it.
- `validate_window_completeness`'s verdict, bar count, and the **full** missing/unexpected date
  lists (not truncated to "N mismatches") — a calendar disagreement here is exactly the kind of
  thing that gets argued about later, so both lists are printed and saved in full
  (`section_f_completeness`).
- `compute_fingerprint`'s SHA-256 and window bounds (`section_f_fingerprint`).
- Whether **SPY resolves to an eToro instrument id at all** — SPY is signal-only and has never
  been fetched from eToro in this project before; a `NOT RESOLVED` line here is itself a finding
  (it means the eToro-candle fallback in `resolve_provisional_close` has no fallback for SPY).
  If it does resolve, the raw eToro candle payload is saved before parsing
  (`section_f_etoro_candles_raw`).
- `resolve_provisional_close` and `assemble_spy_signal`'s real `cone_5_z`, ATH, drawdown, and tier
  (`section_f_assembled_signal`).
- The Tiingo-vs-eToro close cross-check over any overlapping dates — max/mean absolute deviation
  (`section_f_tiingo_vs_etoro_cross_check`); a large divergence is a finding to report, not a
  failure to hide.
- Required-vs-fallback key-diffs against `market/source.py`'s `get_eod_history`/`get_iex_quotes`
  and `_parse_candle` (`nightwatch.probes.live_window.diff_required_keys`) — deliberately distinct
  from section C's own `diff_keys`, which has a high false-positive rate against `or`-chain
  fallback fields (it once flagged `_parse_candle` for six "missing" keys that were entirely
  correct). A genuine `missing_required` entry here is real; `unused_in_payload` is informational.

**Send back everything under `section_f_*` in the pulled output, plus whichever PROBE: F lines
came out of the console log** — especially the SPY-resolution verdict and any `missing_required`
key-diff entries.

### E1-plus, the priority tonight

Answers, with arithmetic, not inference:

- **Fees grouped by instrument** — the single most important output: does *any* SCHG (plain-ETF)
  trade ever carry a fee? (`e1plus_by_instrument`)
- Fees grouped by leveraged/short vs. plain 1x long (`e1plus_by_leverage`)
- Every fee-bearing trade, in full, not summarized (`e1plus_fee_bearing_rows_full`)
- Whether fees correlate with holding duration (financing-charge hypothesis) or investment size
  (percentage-commission hypothesis) — computed (Pearson r), not eyeballed (`e1plus_correlations`)
- An explicit scan of every trade payload for any dividend- or account-charge-shaped field name —
  says plainly if none exists, rather than guessing (`e1plus_dividend_account_field_check`)
- Reconciliation: do the per-trade rows' sums reproduce Σfees = **-$380.40** and Σnet_profit =
  **+$327.25**? If not, that mismatch is reported loudly as a finding in its own right
  (`e1plus_reconciliation`)

---

## 6. Pull the results back

```
adb shell "run-as com.nightwatch.etoro cp -r files/nightwatch_probe_output /data/local/tmp/nightwatch_probe_output"
adb pull /data/local/tmp/nightwatch_probe_output .\pulled_probe_output
adb shell "rm -rf /data/local/tmp/nightwatch_probe_output"
```

`.\pulled_probe_output\` now has every raw JSON payload and report this run produced, each
filename timestamped and self-describing (e.g. `..._e1plus_by_instrument.json`,
`..._e1plus_reconciliation.json`, `..._section_d_measured_figures.json`).

**Send back:**

1. Every file under `pulled_probe_output\`.
2. The full event log from the app (**Share / export log** button, or `adb logcat -d | grep -E
   "PROBE:|RUNNER:" > probe_console.txt`).
3. Specifically call out, in your own words, whether `e1plus_reconciliation` says `RECONCILES` or
   `DOES NOT RECONCILE`, and whether `e1plus_by_instrument` shows `any_nonzero_fee=True` for SCHG.

---

## 7. If you need to change `config_dir` between runs

`nightwatch.host`'s `NIGHTWATCH_HOME` override is only read the *first* time
`nightwatch.host` is imported in the app's Python process (a module-level singleton). If you
re-push a different config after already running the probe once this app session, **force-stop the
app and relaunch it** before running again, so a fresh process picks up the change. (One-shot
tonight, so this should not matter in practice — noted for completeness.)

---

## 8. What was and was not verified tonight

- **Verified:** the APK builds (`assembleDebug`, `BUILD SUCCESSFUL`), the new pip dependencies
  resolve and install for arm64-v8a/cp313, `nightwatch.*` and the copied probe script are
  confirmed packaged inside the built APK, `fee_audit.py`'s arithmetic passes 21 new unit tests
  offline, and the desktop test suite (447 tests total) still passes.
- **NOT verified:** anything about actually running on the phone. This session cannot reach eToro
  from the dev PC and was not given control of the physical device — whether Chaquopy's embedded
  interpreter actually starts cleanly on this exact phone, whether `exchange_calendars==4.11.1`
  genuinely imports at runtime (only its *installation* was proven, at build time), whether the
  live API payloads still match `nightwatch.probes.live_window.PARSER_READ_KEYS`'s assumptions, and
  whether the fee reconciliation actually lands on -$380.40/+$327.25 are all open until the
  operator runs this on-device and reports back. Do not read "the APK builds" as "the probe works."
