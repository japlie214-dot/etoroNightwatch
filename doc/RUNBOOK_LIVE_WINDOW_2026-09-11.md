# Runbook — the 2026-09-11 live window

**Read this whole document before plugging the phone in.** Friday 2026-09-11 is the operator's
only upcoming window with the z10 plugged in during live US market hours before this all slips
another week (HANDOFF_2026-09-05.md's brief). Everything in this runbook drives one script,
[`tools/probe_live_2026-09-11.py`](../tools/probe_live_2026-09-11.py), which batches every open
question that needs a live weekday and/or on-device Chaquopy execution into a single run.

The dev PC cannot reach eToro at all (Kominfo DNS sinkhole, HANDOFF.md §2). **Nothing in sections
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
3. Have the phone's USB Private DNS fix active (HANDOFF.md §2/§7.4b) so the z10 itself can reach
   `public-api.etoro.com` — the dev PC's block does not apply to the phone once that fix is on.
4. Decide **now**, before running anything, whether section E2 is authorized this session. If you
   are not sure, the answer is no — it can always run in a later session.

---

## 3. Running it — read-only sections (A, C, D, E1, B)

This script needs to run somewhere `app/src/main/python/nightwatch` is importable — i.e. inside
the real app's Chaquopy environment on the phone, not a separate throwaway harness, so that the
client code it audits is the actual shipped code, not a stale copy (`android-probe-deps/`'s
`nightwatch/` copy is known stale as of this writing — missing `market/adopt.py`,
`market/source.py`, and several client.py fixes — see HANDOFF_2026-09-05.md; do not run this probe
against that copy without first re-syncing it from `app/src/main/python/nightwatch`).

Two ways to invoke it, in order of preference:

**A. Directly, if the z10 has a Python entry point that already has network + credential access**
(the same mechanism prior live probes used — HANDOFF_2026-09-04_market_data.md §8's "credential
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
