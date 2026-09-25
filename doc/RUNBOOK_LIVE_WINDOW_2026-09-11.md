# Runbook — the 2026-09-11 live window

**Read this whole document before plugging the phone in.** Friday 2026-09-11 is the operator's
only upcoming window with the z10 plugged in during live US market hours before this all slips
another week (board nightwatch/handoff-2026-09-05.md's brief). Everything in this runbook drives one script,
[`tools/probe_live_2026-09-11.py`](../tools/probe_live_2026-09-11.py), which batches every open
question that needs a live weekday and/or on-device Chaquopy execution into a single run.

The dev PC cannot reach eToro at all (Kominfo DNS sinkhole, board nightwatch/handoff.md §2). **Nothing in sections
B through E1 can be run, checked, or debugged from a desktop machine.** Only section A (pure
imports) and the script's own syntax have been verified off-device — see §7 below for exactly
what that means and does not mean.

---

## 1. What this probe answers, section by section

| Section | Question | Read-only? | Roughly how long |
|---|---|---|---|
| A | Do numpy/pandas/exchange_calendars/httpx actually import and run on-device? (HANDOFF §7 item 4) | Yes | seconds |
| B | Is UPRO's rates-freeze bug permanent or transient? (DESIGN_market_data.md §10.6(a)) | Yes | ~(poll_count × poll_interval), default ~25 min |
| C | Do the six unverified parsers' assumed field names match a live payload? (HANDOFF §6/§8) | Yes | seconds |
| D | What is the sleeve arithmetic *right now*, re-measured, not carried over? (HANDOFF §11) | Yes | seconds |
| E1 | Does eToro ever charge a fee on a plain-ETF (SCHG) trade? (new operator report) | Yes | seconds |
| E2 | Is a disclosed fee actually hidden inside the resulting position's `amount`? | **NO — spends real money** | ~1-2 min |

E2 is **off by default** and is not part of the default run. Read §5 before even considering it.

---

## 2. Before you plug the phone in

1. Confirm the recurring SCHG DCA is stopped on the eToro side (DESIGN_market_data.md §10.10) —
   unrelated to this probe, but if you're touching the account anyway, check it now.
2. Confirm `config/config.toml` on the device points at the environment you intend
   (`etoro.environment = "real"` for the real account — this probe reads whatever the device's
   own config says; it does not force an environment).
3. Have the phone's USB Private DNS fix active (board nightwatch/handoff.md §2/§7.4b) so the z10 itself can reach
   `public-api.etoro.com` — the dev PC's block does not apply to the phone once that fix is on.
4. Decide **now**, before running anything, whether section E2 is authorized this session. If you
   are not sure, the answer is no — it can always run in a later session.

---

## 3. Running it — read-only sections (A, C, D, E1, B)

This script needs to run somewhere `app/src/main/python/nightwatch` is importable — i.e. inside
the real app's Chaquopy environment on the phone, not a separate throwaway harness, so that the
client code it audits is the actual shipped code, not a stale copy (`android-probe-deps/`'s
`nightwatch/` copy is known stale as of this writing — missing `market/adopt.py`,
`market/source.py`, and several client.py fixes — see board nightwatch/handoff-2026-09-05.md; do not run this probe
against that copy without first re-syncing it from `app/src/main/python/nightwatch`).

Two ways to invoke it, in order of preference:

**A. Directly, if the z10 has a Python entry point that already has network + credential access**
(the same mechanism prior live probes used — board nightwatch/handoff-2026-09-04-market-data.md §8's "credential
handling pattern," or an adb shell into the app's own Chaquopy process):

```
python tools/probe_live_2026-09-11.py --output-dir <writable-dir-on-device>
```

Default sections are `A,C,D,E1,B` (every read-only section, E2 excluded categorically — it is
never in the default list). Default poll schedule for section B is 6 polls, 5 minutes apart
(~25 minutes total) — override with `--poll-count`/`--poll-interval` if the window is tighter than
that; fewer/closer-spaced polls make B less conclusive but still run the rest of the script fully.

**B. Via Chaquopy's Kotlin bridge**, if invoked from inside the installed app (mirrors
`android-probe-deps/probe/nwprobe.py`'s `run_all()` convention this file follows):

```kotlin
Python.getInstance().getModule("probe_live_2026_09_11").callAttr("run_all")
```

(Copy this file into whatever Python source set the app's Chaquopy build packages, under a valid
module name — note the underscore, not the hyphen, in the module path above; Python module names
cannot contain hyphens.) `run_all()` runs with every default (read-only sections only).

**Watch for, and do not be alarmed by:** every line printed is prefixed `PROBE:` and a UTC
timestamp, flushed immediately (this is the specific fix for the hang HANDOFF_2026-09-05 §7 item 4
and DESIGN_market_data.md §8 describe — unflushed stdout hid the prior probe's own progress before
it hung). If one section times out or throws, you will see `SECTION <X> -- END (TIMEOUT ...)` or
`(EXCEPTION ...)` and the **next section still runs** — this is deliberate (see §6 below); do not
kill the whole process just because one section reported a failure.

---

## 4. What to send back after the read-only run

1. **The full console output**, copy-pasted or logcat-captured, start to finish.
2. **Every file under the output directory** (`--output-dir`, default
   `data/probe_2026-09-11/` relative to wherever the script ran) — these are the raw JSON payloads
   DESIGN_market_data.md §9 calls "the antidote to assumed field shapes." Do not summarize them;
   send the files themselves. Each filename is timestamped and says which section produced it.
3. Specifically flag, in your own words, whether you saw either of these two lines (search for
   them literally):
   - `D: !!! DISAGREEMENT between this probe's measurement and DESIGN_market_data.md §10.11 !!!`
   - Any `C: TRUTHINESS FLAG` line.
4. Section B's verdict lines (`B VERDICT SPY: ...`, `B VERDICT SCHG: ...`, `B VERDICT UPRO: ...`)
   and, if present, the `B: WARNING` or `B: CONCLUSION` line underneath them.

---

## 5. Section E2 — spends real money. Read this twice.

E2 places **one real $50 SCHG buy order** against whatever account `config/config.toml` points at.
If that config says `environment = "real"`, this is real money, no simulation, no dry run. It is
built into this probe because it is the only way to settle the operator's fee report — a $100 SCHG
buy once disclosed a fee that never showed up as a separate cash deduction, and the hypothesis is
that eToro nets the fee out of the reported position `amount` rather than charging it separately.
Settling that requires placing one more order and watching the same three numbers (cash before,
cash after, disclosed fee, resulting position amount) with full raw-payload logging.

**This capability existing does not mean it is authorized.** Nothing in this project has approved
spending real money on this probe. It requires, all three, together:

1. `--e2` on the command line (or the equivalent argument if invoked via `run_all`/Kotlin — you
   will need to call `main(["--e2", ...])` directly rather than `run_all()` for this, since
   `run_all()` never passes `--e2`).
2. The environment variable `NIGHTWATCH_PROBE_E2_ARM` set, **in the shell that launches the
   script**, to the exact string `I UNDERSTAND THIS SPENDS REAL MONEY` (case-sensitive, no
   trailing whitespace). A flag alone is refused — this mirrors `nightwatch.config`'s own
   "arming takes two keys" pattern (DESIGN §5) for the strategy engine's own live trading.
3. Typing `PLACE REAL ORDER` (case-sensitive) at the interactive prompt the script prints, or
   passing `--e2-confirm "PLACE REAL ORDER"` if running non-interactively.

Missing any one of the three causes E2 to print `E2: REFUSING -- ...` and send nothing to eToro.

**If you decide to run it:**

```
NIGHTWATCH_PROBE_E2_ARM="I UNDERSTAND THIS SPENDS REAL MONEY" \
  python tools/probe_live_2026-09-11.py --e2
```

(You can combine `--e2` with any subset of the read-only sections in the same invocation, or run
it as a separate second invocation after reviewing the read-only run's output first — the latter
is recommended, since E1's read-only fee summary may already answer the question for you without
spending anything.)

**What it does, precisely:** reads cash balance -> checks SCHG eligibility for $50 -> places one
$50 SCHG buy order -> polls it to a terminal status (up to 90s) -> dumps the full raw order
response, including every key that contains the substring "fee" (without assuming a name, since
none of this project's other endpoints agree on one) -> re-reads cash balance -> finds the
resulting position and dumps its raw payload -> prints the arithmetic explicitly (cash delta vs.
order amount, and a verdict sentence — see `nightwatch.probes.live_window.reconcile_fee_hypothesis`
for the exact logic, which is unit-tested in `tests/test_probes_live_window.py`).

**Send back exactly what the read-only sections need (§4) plus:**
- The `E2: VERDICT -- ...` line.
- `section_e2_order_result_raw`, `section_e2_new_position_raw`, and
  `section_e2_fee_reconciliation` from the output directory.

**This does not resolve the pre-existing $50 order the operator already placed** — this section
places a *new* $50 order for evidence; it cannot retroactively inspect a past one beyond what §E1's
trade-history/fee partition already surfaces.

---

## 6. Why the script is built the way it is (short version, for whoever reviews this)

- **Every network call carries an explicit timeout** (`httpx`'s own `timeout=` on every request,
  inherited from `EtoroConfig.timeout_seconds`, plus the client's bounded retry/backoff). This
  alone was not sufacient during testing this script off-device against the DNS-sinkholed dev
  PC — a resolve call issued directly in `main()`, before any section wrapper existed, hung well
  past a 120-second harness timeout. That is why:
- **Every section resolves its own instrument ids internally**, inside its own timeout-wrapped
  thread (`run_section`), rather than once, up front, in `main()`. A hang anywhere — DNS,
  TCP, a slow retry loop — is caught by the section's own timeout and the script moves on.
- **Every `print` goes through `_p`, which forces `flush=True`.** This is the second half of the
  original hang's root cause (DESIGN_market_data.md §8): unflushed stdout hid whatever had already
  succeeded before the hang, making it look like nothing ran at all.
- **Every raw payload is saved to a timestamped JSON file**, not just printed, so it survives the
  run and can be re-read later (DESIGN_market_data.md §9).
- **Read-only sections use a `role="read"` credential pair** structurally distinct from the
  `role="write"` pair E2 uses (`nightwatch.config`'s existing read/write credential separation,
  DESIGN §5) — so even a bug in this script's read-only sections cannot place an order, because
  the credential itself would be rejected by eToro for a write endpoint.

---

## 7. What has and has not actually been verified, as of the day this was written

This script was written and tested **from the dev PC**, which cannot reach eToro at all. What was
actually verified before Friday, and what was not:

- **Verified:** the script imports cleanly (`python -m py_compile`), runs end-to-end off-device
  with `--sections A` (all of section A's checks genuinely ran: httpx/numpy/pandas/
  exchange_calendars imported and computed real values, an unauthenticated reachability GET to
  `api.tiingo.com` succeeded).
- **Verified:** section isolation genuinely works — a run against sections C, D, E1 with a
  deliberately short `--section-timeout` demonstrated each section timing out independently (the
  DNS sinkhole means every eToro call from the dev PC eventually fails or hangs) and the script
  proceeding to the next section and finishing, rather than hanging on the first failure.
- **NOT verified, by necessity: every eToro-touching code path** — sections B, C, D, E1, E2's
  actual live behavior against a real payload, whether the parser-key registries in
  `nightwatch.probes.live_window.PARSER_READ_KEYS` still match the live API's current shape, and
  whether the whole script actually completes cleanly under Chaquopy on the z10 (as opposed to
  desktop CPython, which is what ran the tests above). **This is exactly why section A exists as
  its own labelled section** — it is the one thing this session could not independently confirm
  runs correctly under Chaquopy specifically, as distinct from desktop CPython.
- The pure arithmetic/key-diff/partitioning logic in `nightwatch.probes.live_window` **is** unit
  tested (`tests/test_probes_live_window.py`, 19 tests, all passing alongside the full suite) —
  that logic is confirmed correct in isolation; only its *use* against a genuine live payload is
  unverified.

Do not read a clean `py_compile` or a successful off-device dry run as proof this works end to end
on the phone. It proves the parts that could be checked without eToro were checked. The rest is
what Friday is for.

---

## 8. Session items folded in from `DESIGN_session.md` (updated 2026-09-06)

Friday 2026-09-11's open-market session gains one item beyond section B's existing UPRO
rates-freeze re-poll: the order-latency measurement, item (b) below. **A second item, added
2026-09-05 (a read-only `allowPartialClosePosition` eligibility check), was removed on
2026-09-06** once the operator decided partial close is live by direct statement rather than
pending that probe's answer — see §8(c) below for what it was and why it is gone, kept as a
recorded note rather than silently deleted.

**Neither remaining item is built into `tools/probe_live_2026-09-11.py` as of this writing** —
item (b) is specified here as an exact sequence of calls against
`nightwatch.etoro.client.EtoroClient`, to run by hand (REPL, adb shell into the app's Chaquopy
process, or added to the probe script by whoever picks this up next — this document does not
itself modify any Python).

### 8(a). The existing UPRO rates-freeze re-poll

Unchanged — this is section B of this runbook (§1/§3 above), `DESIGN_market_data.md` §10.6(a).
Run it as already specified; nothing about it changes with this update.

### 8(b). Order latency measurement — DEMO account only, no real money

**Why:** `DESIGN_session.md`'s two-phase session (§3) commits to **two broker round-trips
inside the T-30 window** — phase A's sells must reach a terminal status before phase B reads
settled cash and decides its buys. Live order latency has **never been measured**
(`DESIGN.md` §1.1's own standing highest-risk unknown). This item measures it for the first
time, cheaply and with no financial risk, by using the DEMO account.

**Updated 2026-09-06: also time a PARTIAL close, not just a full close.** Partial close is now
the primary SCHG-close mechanism (`DESIGN_session.md` §4.6.1, §4.8's sell-to-target loop), and
the design's viability inside the T-30 window depends on partial-close latency specifically —
not just full-open/full-close latency. Measure both in the same session (step 4b, below).

**Precondition:** `config/config.toml`'s `etoro.environment` must read `"demo"` for this
measurement (confirm before running — do not run this against `"real"`). This is the one
item in this update that must never run against the real account.

**Exact sequence, using existing client methods (`etoro/client.py`), each of the following
timestamped to wall-clock time:**

1. `t0 = now()`; call `client.place_buy_order(request_id, SCHG_INSTRUMENT_ID, amount_usd=50.0)`
   (small, deliberately — this is a latency probe, not a position). Record the returned
   `order_id` and `t1 = now()` (time to HTTP 200/accepted).
2. Call `client.poll_order_to_terminal(order_id=order_id, ...)` — do **not** treat its
   internal polling as opaque for this measurement: instrument or wrap it (or poll manually
   via `client.lookup_order(order_id=order_id)` in a loop) so that **every observed status
   change** is timestamped, not just the final terminal one. Record, for each transition
   observed: the status string (`pending`/`sent`/`WaitingForMarket`/`Filled`/etc. — use
   whatever `OrderResult.status` actually reports), and the wall-clock time it was first
   observed at.
3. `t_terminal = now()` when a terminal status is reached. Report `t_terminal - t0` as
   "time to terminal" for the buy leg.
4. Once terminal (and only if filled, not rejected), call `client.close_position(request_id,
   position_id, instrument_id)` for the position just opened — a **full** close, as
   `close_position` supports today. Repeat the same per-transition timestamping (steps 1-3)
   for the close leg.
4b. **NEW, 2026-09-06 — also time a PARTIAL close, on a separate demo position.** Open a
   second small DEMO SCHG position the same way (step 1), let it reach terminal, then submit a
   partial close for roughly half its `amount` (never leaving a remainder below eToro's own
   $10 `minPositionExposure`/`minPositionAmount` floor — e.g. a $50 position, close $25 of it,
   leaving $25... **note this itself is below the $10 floor's safety margin only in the sense
   that $25 remains well above $10, not that this is a boundary test** — do not use this
   measurement to also boundary-test the $10 floor, keep the two concerns separate).
   **`close_position` does not accept a partial-close parameter today** (`etoro/client.py`
   line ~1112, `UnitsToDeduct: null` hardcoded, `CloseRequest` refuses a non-`None` unit count
   — `DESIGN_session.md` §4.6.1 specifies exactly what must change, but this document may not
   edit Python, and this measurement should not wait for that change to land). **For this
   measurement only**, construct the partial-close HTTP request directly (bypassing
   `close_position`/`CloseRequest`, the same `POST /market-close-orders/positions/{positionId}`
   endpoint, with a non-null `UnitsToDeduct` computed the same way §4.6.1 specifies —
   `units_to_deduct = fraction * lot.units`) using the same authenticated session/credential
   `client` already holds, and apply the identical per-transition timestamping (steps 1-3) to
   it. Record explicitly, in the write-up, that this leg used a hand-built request because the
   production client does not support partial close yet — this is a measurement of the
   broker's behavior, not a demonstration that the shipped code can do this.
5. **Repeat the whole buy-then-close cycle 3-5 times** (a few repeats, not one) — the task
   is explicit that variance across repeats matters, not just a single sample. Use a fresh
   `request_id` each time (the idempotency anchor, DESIGN §6.H, requires this regardless).
6. **Record and report, for the buy leg, the full-close leg, and the partial-close leg
   (step 4b), across all repeats:**
   - Time from submission to each individual status transition observed (not just the final
     one).
   - Time to terminal status, for each repeat.
   - Whether any repeat produced a `PartiallyFilled` status (`OrderStatus` 5) rather than a
     clean full fill/close — DESIGN_session.md §3.4 already specifies how the app must
     handle this if it occurs; this measurement is what would first reveal whether it occurs
     in practice at all.
   - The variance (spread, not just the mean) across the 3-5 repeats — a single fast run and
     a single slow run tell a materially different story about whether the two-phase
     session's T-30 budget (`DESIGN_session.md` §3.2/§14 item 6) is safe "usually" or "only
     under good conditions."
7. **Clean up:** confirm no DEMO position or pending order is left open at the end of the
   measurement (the final close in the last repeat should leave the book flat) — this is a
   measurement exercise, not a standing position.

**Implemented, 2026-09-06 (same day, later): `tools/probe_order_latency_2026-09-11.py`.**
Steps 1–7 above are now a runnable script, not just a hand sequence — written after
`etoro/client.py::close_position` gained its `units_to_deduct` parameter this session (so this
harness calls the real, widened `close_position` for the partial-close leg; it does **not**
hand-build the request the way step 4b originally specified, since that workaround is no longer
necessary — `close_position(request_id, position_id, instrument_id, units_to_deduct=...)` now
does it through the shipped client, and the harness's own summary output says so explicitly).
The script's pure timing/statistics/projection logic lives in
`nightwatch.probes.order_latency` (unit-tested, `tests/test_probes_order_latency.py`, broker
mocked via `httpx.MockTransport` — no network in that test, per this project's established
`tests/test_etoro_client.py` idiom); the script itself does the actual DEMO-account I/O and is
not importable by the desktop test suite (mirrors `tools/probe_live_2026-09-11.py`'s own split).

**Before running it:** confirm `config/config.toml`'s `etoro.environment` reads `"demo"` — the
script also probes this itself and refuses loudly (see below) if it cannot positively confirm
demo, but do not rely on the script alone; check it by hand first, the same as every other item
in this runbook.

**The exact command to run on the phone Friday, from the repo root (or wherever
`app/src/main/python/nightwatch` is importable, same convention as §3 above):**

```
python tools/probe_order_latency_2026-09-11.py --output-dir <writable-dir-on-device>
```

Defaults: `--repeats 4` (within step 5's 3-5 guidance), `--order-amount-usd 50.0` (eToro's own
live-verified SCHG minimum), `--partial-fraction 0.5` (close roughly half of the second
position, per step 4b), `--poll-interval 2.0` / `--poll-timeout 90.0` seconds per leg,
`--orders-low 2 --orders-high 3` (today's typical lots/sell, `DESIGN_session.md` §4.9) and
`--orders-mature 10` (the projected count once the book matures, same section). Override any of
these if Friday's conditions call for it — e.g. `--repeats 3` if the market window is tighter
than expected, or `--section poll-timeout` raised if DEMO responses are slow that day.

**Safety, built into the script itself, not just this doc:**
- **Fail-closed demo check, before any order.** The script reads
  `config/config.toml`'s `etoro.environment`, the derived credential label, and a live
  `/balances` probe, and refuses — printing every check performed — unless all of them
  *positively* say `"demo"`. An inconclusive probe is refused exactly like a detected
  mismatch (`nightwatch.probes.order_latency.evaluate_demo_confirmation`); it does not rely on
  preflight gate 7 (`engine/preflight.py`), which remains an unimplemented stub.
- **Prints exactly what it is about to do** (instrument, amount or units, position id) before
  every buy/close it places.
- **Nothing left dangling.** Every position the script opens is tracked until its closing order
  is confirmed `Filled`; anything still open when the script ends — success, an exception, or an
  interrupt — is reported both on the console and in a dedicated `left_open_positions.json`
  output file, and the script's own exit code is nonzero in that case.

**What to send back after running it:** the full console output; every file under
`--output-dir` (per-leg raw traces, `leg_statistics.json`, `t30_projections.json`, and the
timestamped `_summary.txt`, the same "send the files, don't summarize them" rule as §4 above);
and, specifically, whether the summary's T-30 projection line says the design fits at today's
~2-3-lot count and/or at the ~10-lot mature-book count — that verdict is the entire point of
this measurement (`DESIGN.md` §1.1).

### 8(c). REMOVED, 2026-09-06 — the operator has answered this question directly

**What this item was:** a read-only check of `client.get_eligibility([UPRO_INSTRUMENT_ID,
SCHG_INSTRUMENT_ID])`'s `Eligibility.allow_partial_close` field, to determine whether SCHG
(and UPRO) allow partial position closes at all — the fork `DESIGN_session.md` §4.5/§4.6
(as it stood 2026-09-05) depended on before the executor's SCHG-close path could be built.

**Why it is no longer part of Friday's session:** the operator decided, 2026-09-06, that
partial close is live and is now the primary SCHG-close mechanism (`DESIGN_session.md` §0/§4,
updated) — directly, not contingent on this probe's answer. Friday's session no longer needs
this item to unblock the lot-selection build. **This is recorded here, not silently deleted,**
so a future reader who sees an old reference to "the allowPartialClosePosition check" (in this
document's own git history, or in `DESIGN_session.md`'s superseded 2026-09-05 text) knows what
it was and why it stopped being necessary, rather than re-deriving the need for it. If a future
session still wants empirical, broker-side confirmation that eToro's API actually accepts a
non-null `UnitsToDeduct` for SCHG/UPRO before this ships against the real account, that is a
legitimate **pre-launch verification step** in its own right (distinct from this now-removed
eligibility-field read, which only checks a permission flag, not whether a partial-close
request is actually honoured) — flagged as a residual, lower-stakes risk in
`DESIGN_session.md` §16, not reinstated here as a Friday requirement.

**Friday's session is therefore TWO items, not three:** §8(a) (the UPRO rates-freeze re-poll)
and §8(b) (the order-latency measurement, below — now extended to also time a partial close,
since the design's viability depends on partial-close latency specifically).
