# Audit: can an unannounced cash deposit startle etoroNightwatch?

**Date:** 2026-09-05
**Scope:** code and tests only. No behaviour, config value, or design doc was changed.
**Sleeve model in force (not re-litigated here):** exclusion —
`sleeve_cash = max(0, broker_available_cash - cash_reserve_usd)`, `cash_reserve_usd = 0.0`
today. A `max_sleeve_cash_usd` ceiling was proposed 2026-09-04 and **declined** by the operator
(DESIGN.md §4.4, §12: "not an open question; do not re-propose it"). This audit does not
propose one, a deposit-detection block, or any other structural cap on sleeve growth.

---

## The question

> "I will one day deposit more cash to the portfolio without notice, I hope nightwatch won't be
> startled by it."

Can an unannounced deposit make Nightwatch halt, error, abort a session, mis-reconcile, or
otherwise behave anomalously? The mirror case — cash going **down** unannounced — is covered
too, since it is arguably more dangerous (a session that thinks it has cash it no longer has).

## What was verified, and how

- Read `doc/DESIGN.md` §4.4, §4.5, §4.6, the §12 risk-table deposit row, and the acceptance
  criteria (§10, item 8: "a mid-run deposit is absorbed correctly with no special handling").
- Read `doc/DESIGN_market_data.md` §4.5–§4.7 (gate 11's inverted, position-only reconciliation),
  §6 (gate 10's five sub-checks), §10.11 (measured sleeve arithmetic).
- Read `doc/HANDOFF_2026-09-05.md` in full — the authoritative statement of what is built vs.
  stubbed as of this date.
- Read every line of `app/src/main/python/nightwatch/engine/preflight.py`, including its
  module-docstring map of all twelve gates.
- Read `config.py` (`StrategyConfig`, `RiskConfig`, `MarketDataConfig`), `db/repo.py`'s schema
  classes (`PortfolioSnapshot`, `Instrument`, `LotAnnotation`), `market/adopt.py`, and
  `strategy/params.py` in full.
- Grepped the whole `nightwatch` package for `sleeve_cash`, `sleeve_equity`,
  `broker_available_cash`, `core_floor`, `maxUnitsPerOrder`/`maxPositionExposure` to find every
  place cash amounts are actually read or bounded in code (not just described in a design doc).
- Added 15 tests (`tests/test_deposit_robustness.py`) exercising only code that exists today:
  `strategy/min_allocation.py`'s `allocation_warning`/`affordable_rungs`/`MinAllocationCache`,
  and `config.py`'s `StrategyConfig` validation. Ran the full suite before and after.

**Test result:** `.venv/Scripts/python.exe -m pytest -q` → **407 passed** (was 392; +15, all new,
all passing). See the literal output at the end of this document.

---

## Headline finding

**Nothing in the code that exists today raises, blocks, or crashes on a cash jump in either
direction.** The one module that actually reads and reacts to `sleeve_cash`
(`strategy/min_allocation.py`) is built precisely to *never* block — it only ever emits a
two-tier warning dict or `None`, and that contract is now pinned by tests including a
`neither tier ever raises` sweep across nine reserve/available-cash combinations (pre-existing,
`tests/test_min_allocation.py`) plus this audit's own sweep (deposit sequences, withdrawal-to-
zero, a $7.5M-scale deposit, negative sleeve_cash inputs).

**But that module is a thin slice of the system.** Ten of the twelve preflight gates, the
session engine that would compute `sleeve_cash`/`sleeve_equity` at runtime, and the entire
ladder/core-floor logic are **comment-only stubs**. The honest answer to "will nightwatch be
startled" is: *the part that exists says no; the much larger part that doesn't exist yet has a
real, identifiable way to get this wrong, and this document is the constraint list for whoever
builds it.*

---

## Gate-by-gate table

| # | Gate | Built? | Cash-jump verdict |
|---|---|---|---|
| 1 | Halt switch | Stub | Not cash-dependent. No interaction. |
| 2 | Power & host permissions | Stub | Not cash-dependent. No interaction. |
| 3 | Network | Stub | Not cash-dependent. No interaction. |
| 4 | Clock | Stub | Not cash-dependent. No interaction. |
| 5 | Database (schema / unfinished session) | Stub (query exists: `Repo.unfinished_sessions()`) | Not cash-dependent — checks session state, not sleeve state. A deposit does not touch the `sessions` table. No interaction. |
| 6 | Auth (`GET /balances` == 200) | Stub (client method `get_balance` exists and was itself buggy until 2026-09-05, HANDOFF §1) | Checks HTTP status, not the cash *value* returned. A larger or smaller balance does not change whether the call succeeds. No interaction. |
| 7 | Environment match | Stub | Not cash-dependent. No interaction. |
| 8 | Instruments (`minPositionExposure <= order_size`) | Stub (per-instrument minimums are fetched and cached; the gate function itself is not built) | **A deposit moves this in the passing direction, confirmed, and no upper bound exists anywhere in code.** `order_size` grows with `sleeve_cash` (`strategy/min_allocation.py`'s own formula: `order_size = max(core_fraction*sleeve_cash/order_divisor, min_order_size)`); a bigger sleeve only ever makes `order_size` closer to or further above the per-instrument minimum, never below it. Structural check (this audit, `test_no_sleeve_or_order_size_ceiling_field_exists`): `StrategyConfig` has no `max_sleeve_cash_usd`/ceiling field, confirming the declined-ceiling decision is not silently present anywhere. The only upper bound in the whole system is eToro's own broker-side `maxUnitsPerOrder` (13421 for UPRO, per §2.9's live probe) — a units cap the app has no code to check yet; at any plausible UPRO price this binds only on an extraordinarily large single order, not a realistic deposit. **Forward constraint for whoever builds gate 8:** it must compute the quantity *actually being ordered* (per `min_allocation.py`'s own note: `order_size` for UPRO, the projected new-ATH surplus-sweep amount for SCHG) and must never itself impose a cash ceiling — that would silently reopen the declined `max_sleeve_cash_usd` question. |
| 9 | Calendar | Stub | Not cash-dependent. No interaction. |
| 10 | Price data (freshness/sanity) | **Partially built** (items 1, 3 of 5) | Confirmed by code reading: gate 10 reads only `Rate`/`Candle` objects (prices, timestamps) — no cash field appears anywhere in `preflight.py`. A deposit cannot affect gate 10's pass/fail in either direction. No interaction, verified by reading, not just inferred. |
| 11 | Reconciliation (broker positions vs. sidecar) | Stub (the invariants are specified; `engine/reconcile.py` "not yet built" per the gate's own TODO) | **Reasoned through carefully, per the task brief: a pure cash deposit creates no position, and none of gate 11's four invariants (§4.5 of DESIGN_market_data.md: position membership both directions, `amount ≈ units × openRate`, `instrumentID`/`is_buy`/`leverage` match) reference cash or balance at all.** Confirmed by reading `market/adopt.py` (the nearest built analogue — the §4.7 adoption backfill) and the DESIGN_market_data.md §4.5 spec text itself: gate 11 became a *membership + invariant* check specifically because "there is nothing to disagree about" once the broker owns the financial facts of a position — cash was deliberately dropped out of this gate entirely when it inverted. **No interaction, by design, not by omission.** Forward constraint: whoever builds `engine/reconcile.py` must not reintroduce a cash-agreement check here — that would resurrect exactly the "broker and local must agree on numbers" model DESIGN_market_data.md §4.5 replaced. |
| 12 | Risk caps (order count/notional vs. `config.risk`) | Stub (`RiskConfig` fields exist: `max_orders_per_session`, `max_notional_per_session_usd=5000.0` default, `max_daily_loss_usd`) | **The one gate where a large deposit can legitimately trigger an abort — and that is the cap working as designed, not a bug.** A bigger sleeve → bigger `order_size` → bigger per-order and per-session notional. At the live config (`core_fraction=0.4`, `order_divisor=60`), a single UPRO order's notional only approaches the default `$5,000` session cap once `sleeve_cash` is roughly in the hundreds-of-thousands range (`order_size ≈ 0.006667 × sleeve_cash`); summed across a full session's worth of ladder + SCHG-sweep orders, the cap could bind far sooner after a large deposit. **This is the intended fail-closed behaviour of a blast-radius control (DESIGN.md §6.C), not an anomaly** — but it is a real behaviour change triggered purely by an unannounced deposit, and it aborts the *session* (loud Telegram alert, per DESIGN §5), not just one order. **Forward constraint:** gate 12, once built, must abort gracefully and loudly (as every other gate does) rather than let an unhandled comparison crash, and the operator should know that `max_notional_per_session_usd`'s default may need raising after a large deposit if they want that day's session to actually deploy the newly-available cash rather than partially abort. This is an operational note, not a code defect — and not a proposal to cap the sleeve itself. |

**Summary:** 9 of 12 gates have no cash interaction at all (verified, not assumed, for gates 5, 6,
10, 11 by reading their actual logic or spec). Gate 8 is helped, not hurt, by a deposit, with no
ceiling anywhere. Gate 12 is the one place a large deposit can trigger a (correct, intentional)
session abort via the risk-notional cap — worth knowing operationally, not a bug to fix.

---

## Order sizing and the ladder budget

`order_size = max(core_fraction × sleeve_cash / order_divisor, min_order_size)` — read directly
out of `strategy/min_allocation.py`'s `affordable_rungs` (the only place in the codebase today
that actually computes this formula against a runtime `sleeve_cash`, since the ladder itself,
`strategy/ladder.py`, is still a step-4 stub per `strategy/params.py`'s own module docstring).

- **No assertion, invariant, or clamp anywhere rejects a discontinuous jump in `sleeve_cash` or
  `order_size`.** Checked `config.py`'s `__post_init__` validators (only reject negative/zero
  values and out-of-range fractions — nothing bounds magnitude), `min_allocation.py` (no upper
  bound in the arithmetic or the cache-invalidation signature), and `market/adopt.py` (does not
  read cash at all). This audit's tests pin the absence of a ceiling structurally
  (`test_no_sleeve_or_order_size_ceiling_field_exists`) and empirically (a $7.5M-scale simulated
  deposit still just clears the full-ladder warning threshold with no exception,
  `test_a_very_large_deposit_still_only_warns_never_blocks`).
- **`affordable_rungs` correctly caps the *rung count* at `max_lots` regardless of how large the
  sleeve gets** — this is a deliberate design cap on how many ladder rungs the strategy is meant
  to use, confirmed pre-existing (`test_affordable_rungs_full_ladder_case`) and re-confirmed here
  at 1000× the full-ladder threshold. This is not a cash ceiling; it caps *rungs*, not dollars —
  `order_size` and `budget` themselves are never clamped.
- **`budget_history` / annual budget mid-year discontinuity (DESIGN.md line ~539):** the annual
  budget rule (`budget = floor(0.4 × prior_year_mean_SCHG_value / 20) × 20`) is struck once a
  year from the sleeve's own SCHG *history*, which a pure cash deposit does not touch at all (it
  adds cash, not SCHG position value) — DESIGN.md states this explicitly ("unaffected either way
  by a mid-year deposit") and nothing in the codebase contradicts it; `budget_history`'s schema
  and the annual-budget code path are themselves still stubs, so this is a design-level, not a
  code-level, confirmation. **No code exists yet to test this against.**

---

## The core floor — the most likely real finding, worked out arithmetically

> **RESOLVED 2026-09-05 — the operator reviewed this finding and accepted it.** Their words:
> *"'SCHG's sell headroom shrinks by half of every dollar you deposit' is no problem."* The
> behaviour below is therefore **intended and closed**, not a defect awaiting a fix. Do not
> propose a mitigation, a floor exemption for deposited cash, or a change to
> `core_floor_pct × sleeve_equity`. The derivation is retained because whoever builds
> `strategy/ladder.py` still needs to understand *why* headroom moves when no trade occurred —
> and because the effect is conservative (it protects SCHG harder exactly when more cash is
> available to fund ladder buys directly, which is the thing selling SCHG would otherwise fund).

**This logic (`strategy/ladder.py`) is entirely unbuilt.** No code exists to test. Everything
below is a hand-worked derivation from the design spec (DESIGN.md §1.4, §4.5;
DESIGN_market_data.md §4.7), offered as the forward-looking constraint for whoever builds the
ladder, per the task brief's explicit instruction to reason this through even though it is a
stub.

**The definitions in force:**

```
sleeve_equity = sleeve_cash + market_value(UPRO lots) + market_value(SCHG lots)
core_floor    = core_floor_pct × sleeve_equity   =   0.5 × sleeve_equity   (live config)
```

SCHG is sold only to fund a ladder buy, and **never below** the core floor
(DESIGN.md §1.4, §4.5).

**The arithmetic.** Let `D` be a pure-cash deposit (no position change), landing entirely inside
the sleeve (i.e. above `cash_reserve_usd`, today `0.0`):

```
sleeve_cash_after    = sleeve_cash_before + D
sleeve_equity_after  = sleeve_equity_before + D           (SCHG/UPRO market value unchanged)
core_floor_after     = 0.5 × sleeve_equity_after
                     = 0.5 × (sleeve_equity_before + D)
                     = 0.5 × sleeve_equity_before + 0.5D
                     = core_floor_before + 0.5D
```

**The floor rises by exactly half the deposit.** Define the "SCHG sell headroom" as how much
SCHG market value sits above the floor and is therefore sellable:

```
headroom_before = SCHG_market_value − core_floor_before
headroom_after  = SCHG_market_value − core_floor_after
                = SCHG_market_value − core_floor_before − 0.5D
                = headroom_before − 0.5D
```

**Concrete numbers (using the measured live sleeve figures from DESIGN_market_data.md §10.11 —
SCHG ≈ $44,867, UPRO ≈ $4,047, cash ≈ $1,000 at the time of that measurement, so
sleeve_equity ≈ $49,914, core_floor ≈ $24,957, headroom ≈ $19,910 today).** A $20,000
unannounced deposit:

```
core_floor_after = 24,957 + 0.5 × 20,000 = 34,957
headroom_after   = 44,867 − 34,957        = 9,910   (was 19,910 — cut roughly in half)
```

A $40,000 deposit flips the sign of headroom entirely (`24,957 + 20,000 = 44,957` ≈ SCHG's
market value itself — headroom effectively zero, SCHG becomes non-sellable for that session).

**Verdict on this arithmetic: this is not a bug, and it does not crash anything — but it is a
real, silent behaviour change with no code anywhere to bound or even log it.** A large enough
unannounced deposit can flip SCHG from "some sellable to fund a ladder buy" to "fully protected,
no sell possible" in one session, purely because the floor moved, with SCHG's own value
untouched. This is *consistent with, and arguably the point of, the operator's decision that
adopted positions count in full* ("counting is the protective choice," DESIGN_market_data.md
§4.7) — a bigger sleeve should protect the core book more, not less. It is flagged here as a
finding rather than a defect because:

1. It never produces an invalid or negative state — the floor and headroom formulas are
   monotonic and well-defined for any `D ≥ 0`.
2. It cannot be a preflight failure, a crash, or a mis-reconciliation — gate 11 does not read
   equity or floor values at all (see gate 11's row above), and nothing in `strategy/params.py`
   or `market/adopt.py` computes or checks this floor today.
3. It is exactly the trade-off the operator already reasoned through for adopted positions
   (§4.7) — a deposit is just a smaller, more frequent version of the same "counted equity moves
   the floor" mechanism, not a new failure mode.

**Forward constraint for whoever builds `strategy/ladder.py`'s core-floor check:** it must
re-read `sleeve_cash`/`sleeve_equity` fresh at decision time (never cache a floor computed
earlier in the same session or a prior day), must never treat a floor that moved between two
reads as an error condition to abort on, and must not add any deposit-detection or
"floor changed unexpectedly" alert — the floor is *supposed* to move with the sleeve; that is
the entire justification for the exclusion model. If a "the floor moved a lot, you may want to
know" heartbeat-style notice is ever wanted, it belongs in the informational Telegram report
(§4's step 10), not as a gate that can fail a session.

---

## Withdrawals — the mirror case, and the one genuinely useful finding here

**Cash going down unannounced (or `sleeve_cash` hitting zero) is handled gracefully by the code
that exists today, confirmed by tests** (`test_withdrawal_to_near_zero_cash_degrades_to_a_
warning_never_an_error`, `test_withdrawal_below_the_reserve_clamps_sleeve_cash_to_zero_not_
negative`, both new; the pre-existing `test_reserve_larger_than_available_cash_clamps_to_zero_
never_negative` already covered the reserve-editing side of the same clamp).
`sleeve_cash = max(0.0, broker_available_cash − cash_reserve_usd)` cannot go negative by
construction, and `allocation_warning` degrades to a `"critical"`/tier-1 warning, never an
exception, all the way down to `broker_available_cash = 0.0`.

**One genuinely useful finding, not previously written down anywhere in the design docs:**
`order_size = max(core_fraction × sleeve_cash / order_divisor, min_order_size)` **floors at
`min_order_size` ($50) regardless of how close to zero `sleeve_cash` actually is.** If
`sleeve_cash` collapses to, say, $5 after a large withdrawal, `order_size` as a bare number is
still nominally $50 — the formula does not know the sleeve can no longer afford even one order
of that size. This is not a crash today because `affordable_rungs` (the function that actually
gets called) computes `floor(budget / order_size)` and correctly returns `0` in this case
(verified, `test_withdrawal_to_near_zero_cash_degrades_to_a_warning_never_an_error` and the
pre-existing `test_affordable_rungs_zero_when_sleeve_cash_is_zero`) — but `order_size` itself,
as a bare value, is never affordability-checked at the point it would be computed for an actual
order.

**Forward constraint for whoever builds the executor / session engine (DESIGN §6.B, "never
exceed available cash" — currently unbuilt):** the "sum of all intent amounts in one session
must not exceed `sleeve_cash` minus the tier-1 reserve" check, and the "re-check remaining
sleeve cash immediately before submission" check, **must exist as real code before any order is
placed** — they cannot be inferred from `order_size` alone, because `order_size`'s own floor
means it will happily compute a plausible-looking $50 order even when the sleeve has $5 left.
Skipping this check because "`order_size` looked fine" would send an order eToro will reject for
insufficient funds — not catastrophic (a rejected order, not a wrong one), but exactly the kind
of check the task brief asked to hunt for, and it does not exist yet.

---

## What could NOT be determined — unbuilt code, and the constraint each must respect

Restating this plainly per the task brief: **most of this question is unanswerable today**
because the relevant code does not exist. Below is the complete list, with the binding
constraint for whoever builds each piece — this list is the most valuable part of this document.

| Unbuilt piece | What it must do about a cash jump |
|---|---|
| **Gates 1–7, 9** (`engine/preflight.py`) | No cash interaction expected or required — build them per their own spec rows; nothing here changes because of a deposit. |
| **Gate 8** (instruments/minPositionExposure) | Check the quantity actually being ordered (order_size for UPRO, the surplus-sweep amount for SCHG — `min_allocation.py`'s own note). Must never impose a cash ceiling of its own. |
| **Gate 10 items 2/4/5** (window completeness, corporate actions, signal sanity) | Read no cash field. Confirmed not cash-related by their own spec (bar-window and price-signal checks only) — no constraint needed here. |
| **Gate 11 / `engine/reconcile.py`** | Must stay a position-only membership + invariant check, per DESIGN_market_data.md §4.5. Must NOT reintroduce a cash-agreement comparison — that would resurrect a model the design explicitly replaced. |
| **Gate 12 / risk caps** | Must abort gracefully and loudly (Telegram, per DESIGN §5) if a deposit-enlarged order/session notional trips `max_notional_per_session_usd` or `max_orders_per_session` — this is intended behaviour, not a bug to work around. Must not crash on the comparison itself. |
| **The session engine's `sleeve_cash`/`sleeve_equity` runtime computation** (DESIGN §4.5, "belongs to step 9... still a stub") | Implement the clamp formula verbatim: `sleeve_cash = max(0.0, broker_available_cash − cash_reserve_usd)`. Must read `broker_available_cash` fresh every session (never cache across sessions) so a deposit is picked up the very next run, per the operator's expectation. |
| **`strategy/ladder.py`'s core-floor check** | See the core-floor section above. Must re-read equity fresh at decision time; must never treat a floor that moved since last session as an error; must not gate/abort on it — only the sell-vs-floor comparison at decision time matters. |
| **The executor's "never exceed available cash" checks** (DESIGN §6.B) | Must exist as real pre-submission code, not be inferred from `order_size` alone (see the withdrawal finding above) — `order_size`'s `min_order_size` floor means it does not self-report unaffordability. |
| **`budget_history` / annual budget mid-year rule** | DESIGN.md states a mid-year deposit does not affect the annual budget derivation (it is struck from SCHG history, not cash). No code exists yet to verify this against; carry the constraint forward when this is built: the annual budget computation must key off SCHG's historical market values, never off `sleeve_cash` directly, or a mid-year deposit would leak into it contrary to spec. |
| **`portfolio_snapshots`' sleeve columns** (DESIGN.md §3: `cash_reserve_usd`, `sleeve_cash`, `sleeve_equity` — not yet in `db/repo.py`'s `PortfolioSnapshot` dataclass) | When added, must be written fresh every session (not carried over/cached) so the snapshot pushed to the Cloudflare Worker reflects a same-day deposit — this is also the visibility mechanism DESIGN.md §12 relies on for the operator to *notice* an unplanned deposit via the dashboard. |

---

## Bottom line

**Can an unannounced deposit startle etoroNightwatch? For the code that exists today: no.**
`strategy/min_allocation.py` — the only module that reads a runtime cash figure and reacts to
it — absorbs a deposit or withdrawal of any size without raising, blocking, or crashing; this is
now pinned by 15 new tests plus the pre-existing suite. Gates 5, 6, 10, and 11 (the ones with
enough real logic to check) have no cash interaction at all, confirmed by reading their code or
spec, not assumed.

**The answer changes under two specific, identifiable conditions, neither of which is a defect:**

1. **A deposit large enough to push a session's order notional past `max_notional_per_session_usd`
   (default $5,000, gate 12, unbuilt) will abort that session loudly** — by design, as the
   intended blast-radius control. If the operator wants a large deposit fully deployed the same
   day it lands, they may need to raise this cap first; if not, the abort-and-alert is the
   correct behaviour, not a bug.
2. **A deposit large enough to move the core floor (`0.5 × sleeve_equity`) meaningfully above
   SCHG's current market value will silently make the core book non-sellable for that session** —
   worked out arithmetically above since the code does not exist yet to test. This is consistent
   with the operator's own stated intent for adopted positions (protect the core stack as it
   grows) and is not something this audit recommends changing; it is flagged so it is not a
   surprise once `strategy/ladder.py` is built.

Everything else examined — 9 of 12 gates, order sizing's lack of an upper bound, the reserve's
withholding arithmetic, and graceful degradation on the withdrawal side — either has no
interaction with cash amount at all, or interacts in the direction the design already intends.
The one concrete gap worth fixing when the executor is built (not fixed here, since it is a
stub): `order_size`'s floor at `min_order_size` means it never self-reports "the sleeve can't
actually afford this," so the pre-submission "never exceed available cash" check (DESIGN §6.B)
must be real code, not inferred from `order_size` looking plausible.

---

## Literal test output

```
$ .venv/Scripts/python.exe -m pytest -q
........................................................................ [ 53%]
........................................................................ [ 70%]
........................................................................ [ 88%]
...............................................                          [100%]
407 passed in 8.45s
```

(392 before this audit's tests were added, per `doc/HANDOFF_2026-09-05.md` §3; +15 new tests in
`tests/test_deposit_robustness.py`, all passing, none modified in any pre-existing file.)
