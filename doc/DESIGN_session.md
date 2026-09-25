# `engine/session.py` — Design

**Status:** design only. Nothing built by this document. `engine/session.py` remains a
24-line stub; `engine/preflight.py`, `engine/executor.py`, `engine/reconcile.py` are the
other stubs this design orchestrates. `strategy/ladder.py`/`strategy/state.py` are built and
treated here as a fixed, pure interface (`decide(inputs, state, config) -> LadderDecision`).

**Written:** 2026-09-05. Supersedes DESIGN.md §4.1's step list where the two disagree (flagged
inline, never silently reconciled) and board nightwatch/handoff-2026-09-05-evening.md §6 item 13's stale-comment
observation. Concurrent, out of scope: `android-probe/` — not read, not touched.

**Updated:** 2026-09-05 (later the same day), folding in the operator's SCHG lot-selection
decision (closest-to-target, overruling this document's earlier FIFO recommendation — §4.3)
and the resulting fork on whether SCHG allows partial position closes (§4.6/§4.7), plus
Friday 2026-09-11's two new session items (RUNBOOK_LIVE_WINDOW_2026-09-11.md §8): a DEMO-only
order-latency measurement and a read-only `allowPartialClosePosition` eligibility check. §14's
operator-decision list is updated to match (two items struck as decided).

**Updated again: 2026-09-06.** The operator reversed the 2026-09-05 "not allowed" framing on
partial closes after considering the impact on the strategy as a whole, and made four further
decisions, superseding parts of the above:

- **Partial close is LIVE.** §4.6.1 (exact-deduction) is now the primary, live SCHG-close
  path. §4.6.2 (bounded subset search, full-close-only) is now dead — compressed to a short
  recorded note (§4.7). The Friday 2026-09-11 `allowPartialClosePosition` eligibility check
  (RUNBOOK §8(c)) is **no longer needed** — the operator has answered the question it existed
  to answer — and is removed from Friday's session; its slot is replaced by adding a
  partial-close timing measurement to the existing latency probe (RUNBOOK §8(b)).
- **Lot selection is now highest-cost-basis-first (HCBF), not closest-to-target.**
  Closest-to-target (2026-09-05's decision) and FIFO (this document's original recommendation)
  are both now rejected alternatives, recorded fairly but not live — §4.3/§4.4.
- **The target is now always MET, never merely "stayed under."** Combined with the standing
  "never predict proceeds" principle, this is a sell -> read actual cash -> if short, sell
  again for just the shortfall loop, bounded by a buffer, an iteration cap, and a minimum
  viable top-up floor (§4.8).
- **The $10 broker minimum (`minPositionExposure`/`minPositionAmount`, verified in
  `etoro/client.py` for both SCHG and UPRO) is the binding constraint** on how a shortfall is
  raised: a partial close must never leave a remainder below $10 (§4.6.1/§4.8).

This second 2026-09-06 update touches §0 (problem 2 restated), §3.4 (partial closes are now the
designed-for case, not a rare broker anomaly), §4 in full (lot selection and the
sell-to-target loop rewritten), §6/§7 (reconcile/persistence invariants for a shrunk-but-open
lot), §11 (observability for multi-pass sells), §14 (operator-decision list), and §16
(updated disagreements). Where this update reverses 2026-09-05 text, the old text is
compressed to a short "what it was, why it is no longer needed" note — not silently deleted —
per this project's own audit-trail practice (§8's own precedent).

**Updated once more: 2026-09-06 (later still).** This document's own §16 reservation (item 8)
— that HCBF's ordering field and the sell-to-target loop's sizing field were being used
interchangeably — was raised by this document itself and has now been put to the operator and
resolved: **ordering is by cost PER SHARE (the broker's `openRate`, `Position.entry_price` in
`etoro/client.py`), descending, tie-broken by `position_id` ascending — not by total cost basis
(`lot_annotations.entry_amount`) and not by current market value. Sizing — the partial-close
amount, the shortfall arithmetic, the overshoot/undershoot bounds, and the $10
`minPositionExposure`/`minPositionAmount` remainder check — is by current MARKET VALUE (units ×
today's fetched price, i.e. `etoro/client.py`/`models.py::mark_position`'s
`PositionMark.current_value`) — never cost basis.** In the course of this correction, a real
field-name bug surfaced in the process: `Position.amount` (the broker's own field, confirmed
`amount == units * openRate` on all 227 live positions, `models.py`) is itself COST BASIS, not
current market value, and neither is `lot_annotations.entry_amount` — current market value is
not a stored field anywhere; it must be computed fresh each session as `units × price`. §0, §4.2,
§4.3, §4.4, §4.6.1, §4.8, §14, and §16 are corrected below for this; nothing else in this
document is touched by this pass.

**Updated once more: 2026-09-06 (a fourth pass, per board nightwatch/handoff-2026-09-06.md §1's rule that a
document disagreeing with the code or with a resolved operator decision is the same class of
defect as the code being wrong).** Three corrections, all against the same day's operator
decisions:

- **§16 item 9's cumulative-ratchet claim is RETRACTED — checked directly against
  `strategy/ladder.py:926-929`/`:769`/`:909-917` and found factually wrong.** A too-large
  sell-to-target buffer does not erode the core floor further session after session; the sell
  is already netted against existing cash and skipped once cash covers `cash_target`, so
  overshoot is a float that either shrinks next session's `need` or, past `order_size`, funds a
  buy-back via the "cash to core" path. The undershoot half of item 9 is unchanged and is now
  the dominant asymmetry. See the corrected §16 item 9 for the full trace and the residual
  (smaller, different) costs of a too-large buffer.
- **`max_orders_per_session` is retired, replaced by a latency-derived time gate** — full
  reasoning lands in DESIGN.md §6.C (this document only cross-references it, at §4.9/§14 item 5)
  — `engine/order_budget.py` is the concurrent implementation. **Stated there in terms this
  document repeats because it matters: the replacement is deliberately simple scaffolding, not
  a settled architecture, and may be redesigned at any time without ceremony.**
- **`N` (§3.4/§14 item 2) no longer escalates a persistently non-terminal order to an
  auto-halt.** The app must be fully automatic; there is no cancel endpoint anywhere in
  `etoro/client.py` (grep-verified), so the only automatic resolution available is state-based —
  mark the order abandoned after the polling horizon and resolve against observed position/cash,
  with a narrow, provenance-backed auto-adopt path for a later fill matching one of our own
  recorded abandoned intents (distinguished explicitly from DESIGN.md §1.4 fix #3's
  fabricate-a-lot bug by that provenance). Halts remain reserved for money-losing ambiguity,
  never bookkeeping ambiguity.

§3.4, §4.9, §14 (items 2 and 5), and §16 (item 9) are corrected below for this; nothing else in
this document is touched by this pass. Per this project's audit-trail practice, none of the
retracted/superseded text is deleted — it is struck and dated in place.

**Read in full before this:** DESIGN.md §3.1/§4.1–§4.5/§6.C, DESIGN_market_data.md §3.3/§4.3/§4.4/
§4.7/§10.x, board nightwatch/handoff-2026-09-05-evening.md, board nightwatch/handoff-2026-09-06.md §1/§9, DESIGN_sandbox.md,
`strategy/ladder.py`,
`strategy/state.py`, `market/window.py`, `market/adopt.py`, `engine/preflight.py`,
`etoro/client.py`, `db/repo.py`, `tools/adopt_backfill_runner.py`.

---

## 0. The two hard problems, answered up front

**Problem 1 — async fills vs. synchronous `decide()`.** Solved by a **two-phase session**:
phase A calls `decide()` once and executes only the **sells** (and SCHG-funds-ladder-buy
sells) it returns; phase B, after every sell from phase A has reached a terminal status,
re-derives cash/equity from the broker (never predicted) and calls `decide()` a **second
time**, discarding phase A's buy-side intents and using phase B's own. See §3.

**Problem 2 — SCHG dollar amounts vs. eToro `position_id`s.** The ladder does **not** solve
this for SCHG (it does for UPRO — UPRO closes already carry `position_ref`). A lot-selection
policy is required, plus an execution loop that actually meets the cash target it is asked to
raise. **As of 2026-09-06, decided by the operator, twice, superseding both this document's
2026-09-05 text and each other in sequence:**

1. **Lot-selection order: highest cost PER SHARE first (HCBF).** Sort open SCHG lots by the
   broker's `openRate` (`Position.entry_price` in `etoro/client.py`) descending, tie-broken by
   `position_id` ascending. This supersedes both the 2026-09-05 closest-to-target decision and
   this document's original FIFO recommendation — both are now rejected alternatives, recorded
   fairly at §4.4, alongside a third rejected alternative this correction adds: ordering by
   highest TOTAL cost basis (`lot_annotations.entry_amount`) instead of per-share cost, rejected
   because it **degenerates** — every app-created position opens at exactly
   `min_order_size = $50`, so all future lots tie on total cost basis and the ordering collapses
   into an arbitrary tie-break; it would meaningfully order only the 202 legacy adopted DCA lots
   (~$202 average cost) and stop meaning anything as that book is sold down (§4.9). HCBF-per-share
   realizes the smallest gain PER UNIT sold first — its accepted long-run cost is that the
   cheapest-per-share lots are retained, so embedded gain concentrates over time in what
   remains. See §4.3/§4.4.
2. **The aim is to always MEET the target, never merely stay under it.** Combined with the
   standing "never predict proceeds, read the cash" principle (§2), this makes selling a
   sell -> read actual cash -> if short, sell again for just the shortfall loop, bounded by a
   first-pass buffer, a hard iteration cap, and a minimum-viable-top-up floor below which the
   session accepts the shortfall rather than loop further. See §4.8.
3. **Partial close is LIVE, and is now the primary mechanism**, reversing the 2026-09-05
   "not allowed" framing after the operator reconsidered the impact on the strategy as a whole.
   `etoro/client.py::close_position` currently hardcodes `UnitsToDeduct: null` (full close
   only) under an earlier "partial close is explicitly out of scope" scope decision — **that
   scope decision is now revisited** (§4.6.1 specifies precisely what must change; this
   document does not implement it). A partially-closed position **stays open with reduced
   units**, not `is_open=False`; `lot_annotations` needs an update-in-place path for a shrunk
   lot, and `engine/reconcile.py` needs a new invariant for "DB amount vs. broker's reduced
   amount, position open on both sides." See §3.4/§4.6.1/§6.
4. **The broker's $10 minimum (`minPositionExposure`/`minPositionAmount`, both $10 for SCHG
   and UPRO, verified in `etoro/client.py`) is the binding constraint** on how a partial close
   may be sized: it must never leave a remainder below $10 — checked against the position's
   current MARKET VALUE, never its cost basis. **This is the same field discipline as item 1,
   applied to sizing instead of ordering: the partial-close amount, the shortfall arithmetic,
   the overshoot/undershoot bounds, and this $10 check are all computed against current market
   value (units × today's fetched price — `PositionMark.current_value`,
   `etoro/client.py`/`models.py::mark_position`), never against cost basis (`Position.amount`,
   `lot_annotations.entry_amount`, or `entry_price × units`).** Cost basis (per-share, item 1)
   decides WHICH lot is next; market value decides HOW MUCH closing it raises and whether $10
   is left over. See §4.6.1/§4.8 for the exact rule and a worked trace of the operator's own
   $51-vs-$50/$50/$60 example, confirmed expressed in market-value terms (a $60 position being
   a $50 buy that has appreciated).

Reasoning for HCBF over closest-to-target and FIFO, and the full sell-to-target algorithm, is
in §4. ---

## 1. Section outline

1. The two hard problems, answered up front
2. Why `decide()` cannot be called once per session — the mechanism
3. The two-phase session: design and rejected alternatives
4. SCHG lot selection: candidate policies, recommendation, operator decision
5. The session sequence (reconciled against DESIGN.md §3.1/§4.1, contradictions flagged)
6. Preflight integration: which gates gate which step, fail-open vs. fail-closed
7. State persistence: exact write points, the `LadderState.bars` trap, crash semantics
8. The adoption migration bootstrap: where it runs, exactly-once, verification
9. The `exchange_calendars` cold start: where it is warmed
10. The single-session constraint (SCHG ATH test) — reaffirmed, not reopened
11. Idempotency and observability
12. Module layout
13. Phased build order and dependencies
14. Operator decisions required before implementation
15. Correctness beyond the fixtures — defending against the signature failure mode
16. Disagreements, ambiguities, and things guessed at

---

## 2. Why `decide()` cannot be called once per session

`strategy/ladder.py::decide` is pure and synchronous: given `LadderInputs` (including
`cash`, `equity`, `schg_position_value`, `open_lots`) it returns a `LadderDecision` — a batch
of `LadderIntent`s — in one call, no callback, no partial return.

Two of those intents are asynchronous by construction once handed to the broker:

- **UPRO closes** (`side="close"`, `position_ref` set) — full-close via
  `POST /market-close-orders/positions/{positionId}`, HTTP 200 = accepted, not filled
  (DESIGN.md §1.1). `_sell_lots` computes `proceeds` from `upro_price * lot.units` — an
  **estimate**, used only to decide whether the sale created a new-ATH re-entry bucket
  (`new_ath_sale`) and to size that bucket's `tranche_amount`. The *actual* proceeds are not
  known until the close order reaches a terminal status.
- **SCHG close-to-fund-ladder-buy** (`side="close"`, `amount=sell_amount`, no `position_ref`
  at all — see §4). Same asynchrony, same estimate-vs-actual gap.

Then, still inside the **same** `decide()` call, `_buy_if_allowed` and
`_process_reentry_buckets` size **UPRO buys** and `_schg_buy_intents` sizes **SCHG buys**
against `inputs.cash` — a number that, in production, has not yet been updated by the sells
this exact call just decided to make. This is the ladder's own **cash recycle** design
(DESIGN.md §1.4): sale proceeds fund the same session's buys. The backtest this was ported
from can do this because its proceeds are known synchronously (a simulated fill is instant);
`decide()` inherited that assumption verbatim, and `ladder.py`'s own module docstring
explicitly declines to invent a two-phase interface, leaving it to this design (task brief;
confirmed by re-reading the module — there is no phase marker, no partial-decision return, no
"pending sells" input field anywhere in `LadderInputs`/`LadderState`).

The operator's own standing principle — `FINDINGS_fees_2026-09-05.md` §4: *"On selling, don't
predict proceeds — just read the cash balance"* — is not a style preference here. It is
**measurement-backed**: eToro's carrying fee correlates with `investment × holding_days` at
r=0.9918 and is *settled on close*, so `proceeds = units × close_price` is provably wrong by
an amount that grows with how long the lot was held (§4.1 of that document). There is no
formula available to `decide()` that would let it predict its own sale's proceeds correctly
even if it tried.

---

## 3. The two-phase session

### 3.1 Design chosen

**Phase A — decide and execute sells only.**

1. Build `LadderInputs` from the broker's current state (cash, equity, open lots).
2. Call `decide()` once.
3. From the returned `LadderDecision.intents`, execute **only** the `side="close"` intents
   (UPRO lot closes and the SCHG "fund ladder buy" close, if present) through
   `engine/executor.py`'s poll-to-terminal path.
4. **Discard every `side="open"` intent from this call.** They were sized against
   pre-sale cash and are stale the instant a sell is submitted, by the ladder's own design.
5. Persist `LadderDecision.closed_lots`, `updated_buckets` (bucket creation from a
   new-ATH sale — see §3.3 on why this is provisional), and the sell-side portion of
   `new_state` are **not yet committed** — see §7 for exactly which fields are safe to
   write now vs. must wait for phase B.

**Phase B — re-derive cash, decide and execute buys.**

6. Wait for every phase-A order to reach a terminal status (`poll_order_to_terminal`,
   bounded by a timeout — see §3.4 for what happens if one does not).
7. **Read the broker's cash balance fresh** (`GET /balances`, the documented
   `available = credit − Σorders` formula, DESIGN.md §4.5) — never sum estimated proceeds
   onto the pre-sale figure. This is the direct application of the operator's principle
   from §0/§2.
8. Re-read `schg_position_value` and `equity` the same way (mark-to-market off the same
   fetched prices phase A used — prices do not need to be re-fetched mid-session; only cash
   and position state do, since those are what the sells changed).
9. Rebuild `LadderInputs` with this **actual** cash/equity and the **now-updated**
   `open_lots` (phase-A closes removed, no new opens yet), and **the same `LadderState`**
   from phase A (bars/ath/tier context does not change mid-session; only cash-shaped fields
   do — see §7 for which fields phase B's second `decide()` call is allowed to overwrite).
10. Call `decide()` a **second time**.
11. This second call may itself propose a **sell** (e.g. if the ladder's own re-entry-bucket
    logic wants to fill a tranche using freshly-recycled cash and that pushes a different
    gate) — in practice this is rare because phase A already ran the sell-side logic, but it
    is not forbidden by the interface, and this design does not special-case it away: any
    `side="close"` intent phase B returns is executed exactly like a phase-A close, and the
    session does **not** loop into a phase C. One re-derivation is enough because the second
    call's sells (if any) are sized off already-verified cash, not off an estimate — the
    asymmetry that justified two phases in the first place does not recur. If phase B
    proposes a close anyway, execute it, but do not synthesize its proceeds into a phase C
    buy step — book it and let the *next day's* session see the updated cash. This bounds
    the design at two phases, always, never an unbounded fixed-point loop.
12. Execute phase B's `side="open"` intents (and any `side="close"` per the note above)
    through the same executor path.
13. Persist everything: `new_lots`, `new_state` (now genuinely final for the day),
    remaining `updated_buckets`, portfolio snapshot.

### 3.2 Tradeoff accepted

- **Cost:** two `decide()` calls and (up to) two executor passes per session, inside the
  same T-30-to-close window. Each `decide()` call is a pure in-memory computation
  (microseconds); the cost is entirely the **second wait for cash to settle**, which is
  bounded by however long phase A's closes take to reach terminal (poll-to-terminal,
  bounded timeout, §3.4). This eats into the T-30 margin but does not require a second wake
  or a second schedule — it is still one session, one wake, matching the single-session
  constraint (§10).
- **Benefit:** phase B's buys are sized against **real**, broker-confirmed cash — never a
  guess. This is the only design considered that keeps `decide()` pure, keeps the "read cash,
  don't predict it" principle intact for the strategy's own recycling logic (not just for
  P&L reporting), and needs no change to the ladder's signature.
- **What was rejected, and why:**
  - *Persist pending sells as state, resume them next session, buy on realized proceeds the
    following day.* Rejected: this changes the ladder's actual behavior (cash recycling
    becomes next-day, not same-day), which is not this design's authority to alter silently
    — the ladder was explicitly ported to recycle *within* a session (DESIGN.md §1.4). It
    also multiplies the "crash mid-session" surface across two calendar days instead of
    containing it to one wake, and it means a re-entry bucket's `tranche_amount` (sized off
    phase-A's estimated `proceeds`) would silently be one day stale relative to what the cash
    target actually needed. Kept as a **fallback**, not the primary design: if a phase-A
    close is still non-terminal when the session's time budget is exhausted, the session
    degrades to exactly this — see §3.4.
  - *Predict proceeds and buy in one pass.* Rejected outright — this is the exact anti-pattern
    `FINDINGS_fees_2026-09-05.md` measured as **wrong by a fee-dependent, holding-time-
    dependent amount**, not a rounding error.
  - *Invent a genuinely two-phase `decide()` signature (`decide_sells()` /
    `decide_buys(realized_cash)`).* Considered and rejected for this document's scope: it
    would require changing `strategy/ladder.py`, which the ladder author deliberately left
    alone and which `DESIGN_sandbox.md`'s adapter (`ladder_adapter.py`) already depends on as
    currently shaped. The two-phase **session**, calling the existing single-shot `decide()`
    twice with different inputs, achieves the same effect without touching the strategy
    interface or the sandbox's contract.

### 3.3 The re-entry bucket's proceeds are estimated in phase A — accepted, bounded

`_sell_lots` computes `proceeds` (used for `tranche_amount` and `new_ath_sale` bucket
creation) from `upro_price * units`, not from the broker's fill. This estimate **is not
corrected** by phase B — the bucket is created in phase A with its estimated
`tranche_amount` and is not re-sized once real proceeds are known. This is a deliberate,
narrower acceptance than "guess the recycled cash for buys": a re-entry bucket's tranche
size affects *future* sessions' buy sizing (it dribbles out over up to 3 tranches across
future bars, DESIGN.md §1.4), not *this* session's buy budget — the quantity `FINDINGS_
fees_2026-09-05.md` was measured against and demanded be exact is cash spent *this* session,
which phase B does derive from real balances. A bucket sized a few dollars off its true
proceeds (the fee-driven gap `FINDINGS §3` measured, ≤0.32%/yr annualized, on a
same-day-realized sale) is a bounded, self-correcting error: subsequent tranches from the
same bucket use `last_reference_spy`/`tranches_bought`, not a re-derived proceeds figure, so
the error does not compound. **Recorded as an accepted approximation, not silently
smoothed over** — a future pass could correct `updated_buckets[i].tranche_amount` once real
proceeds are known, by reading the actual close order's `fill_amount_usd` and rewriting the
bucket before persisting it in phase A's own persistence step (§7); this document does not
require that correction for a first working version, but flags it explicitly as an
improvement, not an oversight.

### 3.4 Non-terminal order at session end, and partial fills

**Non-terminal order when the session must end (time budget exhausted, or
`poll_order_to_terminal`'s own bounded timeout expires on a phase-A close):**

- The order is **not** cancelled and its outcome is **not** guessed. `engine/executor.py`'s
  `intents` row (already persisted before the HTTP call, per the idempotency anchor,
  DESIGN §5/§6.H) stays in whatever status the last poll observed
  (`pending`/`sent`/`WaitingForMarket`).
- Phase B is **skipped entirely for this session** — the session does not proceed to buy on
  a cash figure it cannot yet verify, and does not fall back to an estimate (that would be
  exactly the anti-pattern §3.2 rejects). This is the **fallback path** named in §3.2: cash
  recycling degrades from same-session to next-session for this one sale.
- The session reports (Telegram, §7.10) that N sells were placed, M reached terminal, and
  buys were skipped pending settlement — never silently reported as "nothing to do."
- **The very next session's preflight** (specifically, a new check this design adds to gate
  11's reconciliation scope — see §6) resolves the pending order by calling
  `recover_by_reference_id` before doing anything else: if it is now terminal, book the fill
  (or the rejection) exactly as phase A would have, then proceed normally with fresh cash
  that already reflects it — no special "catch-up phase B" needed, because the *next*
  session's own phase A will read post-settlement cash as a matter of course.

  **UPDATED, operator decision 2026-09-06 — retracts the auto-halt escalation this paragraph
  previously specified. ~~If it is **still** non-terminal after N consecutive sessions
  (config-driven, see §14), this is escalated as an auto-halt condition (DESIGN.md §6.I) rather
  than polled forever — an order that has not settled in N trading days is no longer "in
  flight," it is anomalous.~~ The operator's stated requirement: the app must be fully
  automatic, and a halt requiring manual resolution is not acceptable for this failure mode.**

  The facts that drive the replacement design:
  - Every order this app places is a **market** order, never a limit order, so a genuinely
    non-terminal order should be very rare in the first place — this is not a failure mode the
    design expects to trigger often, only one it must not depend on a human to close out when
    it does.
  - **There is no cancel path.** `etoro/client.py` exposes `recover_by_reference_id` (line 1196)
    for exactly this kind of recovery, but grepping the whole client for a cancel/DELETE
    endpoint (`cancel`, `DELETE`, `delete` — checked 2026-09-06) finds **none anywhere in the
    file**. A stuck order cannot be retracted; the only automatic resolution available is
    state-based, not order-based.
  - **Therefore: stop treating the order itself as an entity that must reach a terminal
    status.** The order is a *means*; the **position and the cash are the truth**, and the
    session already re-reads both every night regardless (§0/§2). After the polling horizon
    (still governed by `N`, below), the order is marked **abandoned** and the session resolves
    against observed account state instead of continuing to wait on the order.
  - **The one thing that would otherwise force a human back in:** if an abandoned order later
    fills anyway, gate 11 (§6) sees an unmatched broker position and fails closed, exactly as
    designed for an unexplained position. The fix is a **narrow auto-adopt path**: a broker
    position that matches one of our **own recorded abandoned intents** (same instrument, same
    size, same `referenceId`) is adopted automatically rather than failing the gate. **This is
    NOT the fabricate-a-lot bug DESIGN.md §1.4 fix #3 exists to prevent** — bug #3 invented a
    lot from nothing, with no record anywhere that it should exist; this auto-adopt path only
    ever matches a position against a lot **we ourselves already persisted** as an abandoned
    intent, complete with its instrument, size and reference id. **Provenance is the whole
    distinction.** A bare unmatched broker position with **no** matching abandoned intent of
    ours still fails gate 11 exactly as today — this path narrows one specific, provenance-
    backed case out of an otherwise-unchanged fail-closed gate, it does not weaken the gate
    generally.
  - **`N` therefore changes role: it is no longer "sessions until a human is needed."** It is
    now "sessions we keep polling before switching from order-based to state-based
    resolution" — see §14 item 2 for the value itself, still open.
  - **Principle worth stating explicitly, since it should govern future decisions of this
    shape too: halts are reserved for money-losing ambiguity, never for bookkeeping
    ambiguity.** A non-terminal market order with no cancel path is a bookkeeping question
    (what does our ledger say happened, and can we reconcile it against the broker's own
    state) — not a question of whether real money is at risk in a way only a human can assess,
    which is what a halt exists to gate.

**Partial closes and partial fills — UPDATED 2026-09-06: partial close is now LIVE and
designed-for, not an out-of-scope broker anomaly.**

There are now two distinct ways a SCHG (or, in principle, UPRO) lot ends a session smaller but
still open, and this design treats both the same way at the persistence/reconciliation layer,
even though they arise differently:

- **Deliberate partial close (the new, primary mechanism, §4.6.1).** The executor
  deliberately requests a non-null `UnitsToDeduct` sized to raise exactly the shortfall the
  sell-to-target loop (§4.8) needs, honouring the $10 remainder rule. This is not a broker
  anomaly — it is the app asking for exactly this outcome. `~~eToro's close endpoint is always
  a full close (`UnitsToDeduct: null`, DESIGN.md §4.3) for UPRO lots and (per this design's
  recommendation, §4) for SCHG lots too — this app never requests a partial close.~~` is
  **no longer true as of the 2026-09-06 operator decision** and is struck here rather than
  silently removed, since it was this document's own prior claim. UPRO still only ever receives
  full closes (the ladder tracks UPRO at lot granularity and never asks for a partial UPRO
  close); SCHG closes may now be full or partial depending on where in the HCBF-descending
  order (§4.6.1) a given close falls.
- **Broker-side partial fill on a requested full close (`OrderStatus` 5, PartiallyFilled) —
  still possible, now rarer.** A `PartiallyFilled` status on a close the app *did* request as
  full (`UnitsToDeduct: null`) means the **broker** partially executed it — a genuine, if rare,
  broker behavior, not something the app asked for.

**Both cases are handled identically from here on**, and this is the invariant this document
adds: the lot is **not** removed from `open_lots`/`lot_annotations`; it is updated **in place**
to reflect the remaining `units`/`amount` from the broker's own next portfolio read (never
assumed — DESIGN.md §6.H: "the actual filled units; the requested quantity is never
assumed"), exactly as a shrunk-but-open position, not a closed one. Phase B (and, within phase
A, the sell-to-target loop's own next pass, §4.8) proceeds with only the **actually-realized**
cash from whichever portion actually closed — never the full expected proceeds, whether the
shortfall was deliberate (a partial close request) or accidental (a broker partial fill).

**`engine/reconcile.py`'s invariant, extended again:** gate 11's contract (DESIGN_market_data.md
§4.5's original two invariants, plus this document's earlier addition for an assumed-complete
close whose broker position still exists) now needs a **third, distinct** shape, because a
deliberate partial close is not a bug to catch — it is the normal case, and the check is no
longer "did the app wrongly assume a close was total," it is "does the DB's recorded amount for
a still-open position match the broker's own reduced amount for that same `position_id`":
a DB lot whose recorded `amount`/`units` no longer matches the broker's own (smaller) figure for
that `position_id`, with the position **open on both sides**. This is a genuinely new shape,
not a restatement of the existing two, and is added here as an explicit third invariant.

- On a **buy**, a partial fill is booked as a lot with the actually-filled `units`/cost basis
  from the poll result — the ladder's own sizing (`order_size`, tier caps) was computed
  against the *requested* amount, and a partially-filled buy is simply a smaller lot than
  requested; no correction is needed because the ladder recomputes its budget gates fresh
  every session from `open_lots`, which will already reflect the smaller cost basis.

---

## 4. SCHG lot selection and the sell-to-target loop

**Updated 2026-09-06 in full.** The operator made four decisions the same day that together
replace this section's 2026-09-05 content: HCBF lot ordering (not closest-to-target, not
FIFO), partial close now live as the primary mechanism, "always meet the target" rather than
"stay under it," and the $10 broker minimum as the binding constraint shaping how a partial
close may be sized. The 2026-09-05 text is not deleted — it is compressed into short recorded
notes at §4.4/§4.7 so a future reader does not have to re-derive why closest-to-target and
the full-close-only subset-search branch are no longer live.

### 4.1 The problem, restated precisely

The ladder's `LadderIntent(symbol="SCHG", side="close", amount=sell_amount, ...)` — from
`decide()`'s "fund ladder buy" step — carries **no `position_ref`**. Contrast with UPRO
closes, which always carry `position_ref` because `LadderLot` tracks UPRO at lot granularity
(one `LadderLot` = one broker position, per the module's own "Scope boundary" docstring).
The ladder's docstring states this explicitly: *"this module decides... SCHG buys/sells at
DOLLAR-AMOUNT granularity only — it does not track individual SCHG lots... Resolving an SCHG
dollar-amount close into specific eToro `position_id` closes is the executor's job."*

**Confirmed: the ladder does NOT supply explicit lot refs for SCHG, and this is not an
oversight to "fix" by reading the code more carefully — it is a deliberate scope boundary
stated in the module's own docstring.** `engine/executor.py` (or a helper this design adds
ahead of it) must choose which of the (currently 202, growing) SCHG `lot_annotations` rows to
close — in whole or in part — to raise `sell_amount` in cash, using partial closes as the
primary mechanism as of the 2026-09-06 decision (§4.6.1), not the full-close-only subset
search this document specified on 2026-09-05 (now dead, §4.7).

### 4.2 Candidate policies

| Policy | What it optimises | What it costs |
|---|---|---|
| **Highest cost PER SHARE first (HCBF)** — **DECIDED, 2026-09-06, ordering field clarified same day** | Realizes the smallest gain PER UNIT sold first: sorting by `openRate`/`entry_price` descending (tie-break `position_id` ascending) closes the lots bought most expensively per share first, leaving the cheapest-per-share (most gain-concentrated) lots in the book at any point in time. This is a different axis from total cost basis (`entry_amount`, see the rejected row below) and from the market value used for SIZING (§4.6.1/§4.8). Combined with partial close (§4.6.1) and the meet-the-target loop (§4.8), it also tends to minimize round-trips for a given `sell_amount` in the typical case. | **Accepted long-run cost:** the cheapest-per-share lots are retained, so embedded gain concentrates over time in what remains — the account's realized-gain profile trends toward "small, frequent realized gains now, one large concentrated position eventually." This is an accepted, stated cost, not an oversight. |
| **Highest TOTAL cost basis first (`entry_amount` descending)** — considered, **rejected: degenerates** | Would order lots by total dollar cost basis rather than per-share cost — the literal reading someone might reach for first. | **Rejected because it degenerates:** every app-created position opens at exactly `min_order_size = $50`, so every future lot ties on total cost basis and the ordering collapses into an arbitrary tie-break. It would meaningfully order only the 202 legacy adopted DCA lots (~$202 average cost, §4.9) and stop meaning anything as that book is sold down relative to the growing $50-origin book. The operator's own worked example ($50/$50/$60) does **not** distinguish this reading from per-share HCBF — all three lots tie under a total-dollars sort (each is a $50 cost-basis lot) — so the example cannot be cited as evidence for either reading on the ordering question; it is only unambiguous on the sizing question (§4.3/§4.8). |
| **Closest-to-target amount** — 2026-09-05 decision, **now rejected** (§4.4) | Minimized the dollar difference between what was needed and what was raised, when closes were all-or-nothing (no partial close). | Rejected because the premise it was solving — "closes are all-or-nothing, so minimize overshoot" — no longer holds once partial close is live: there is no overshoot to minimize when the exact shortfall can simply be taken. |
| **FIFO** (oldest `entry_date` first) — this document's original recommendation, **still rejected** | Simplicity, predictability, matches typical tax-lot accounting (though this account is not shown to be tax-optimised anywhere in the docs). | Ignores cost basis and current price entirely, and (per §4.3's original reasoning) does not connect its ordering to any of the strategy's own economic quantities — core floor headroom or realized-gain shape. |
| **Honour the ladder's own lot refs** | N/A — **the ladder does not have per-SCHG-lot refs to honour.** `LadderInputs.schg_position_value` is a single aggregate float; there is no `SchgLot` type anywhere in `strategy/ladder.py`. This column exists to record that the option was checked and does not exist. | N/A |

**Does the ladder's internal lot tracking dissolve this problem? No — checked directly
against the module** (unchanged finding from 2026-09-05): `strategy/ladder.py` tracks UPRO at
lot granularity but SCHG only as a single scalar (`LadderInputs.schg_position_value`)
everywhere it appears. The problem is real and must be solved at the executor layer.

### 4.3 Decision (operator, 2026-09-06): highest cost PER SHARE first, partial close live, meet the target

**Sort open SCHG lots by `entry_price` (the broker's `openRate` on the position,
`Position.entry_price` in `etoro/client.py`) descending, tie-broken by `position_id` ascending
— NOT by `lot_annotations.entry_amount` (total cost basis) and NOT by current market value.
Walk that order closing whole positions until the remaining need is smaller than the next
position's CURRENT MARKET VALUE, then partially close that position for exactly the shortfall
— unless doing so would leave a market-value remainder below $10, in which case close that
position whole as well (and continue if still short). Never predict proceeds: read actual
settled cash after each pass and, if short of the target, sell again for just the remaining
shortfall, bounded by a buffer/cap/minimum-top-up loop (§4.8).**

**Why cost PER SHARE, not total dollar cost, for ordering:** this is the standard meaning of
"highest-cost-first" in lot selection, and it is the ordering that actually realizes the
smallest gain PER UNIT sold — the operator's stated intent. Ordering by total dollar cost basis
(`entry_amount`) instead **degenerates**: every app-created position opens at exactly
`min_order_size = $50`, so all future lots tie on total cost basis and the ordering collapses
into an arbitrary tie-break; it would meaningfully order only the 202 legacy adopted DCA lots
(~$202 average cost, §4.9) and stop meaning anything as that book is sold down (§4.4 records
this as a rejected alternative). **The operator's own worked example ($50/$50/$60) does not
distinguish the two readings** — under a total-dollars sort all three lots tie (each is a $50
cost-basis lot) — so it cannot be cited as evidence for either ordering reading; it is decisive
only about SIZING (below).

**Why current MARKET VALUE, not cost basis, for sizing:** the partial-close amount, the
shortfall arithmetic, the overshoot/undershoot bounds, and the $10
`minPositionExposure`/`minPositionAmount` remainder check are all computed against the
position's current MARKET VALUE (units × today's fetched price), never its cost basis. Market
value is what can actually be realized by closing the position, and it is the quantity the
broker's $10 floor applies to; cost basis is a historical, entry-time number with no bearing on
how much cash a close raises today. **Cost basis is used for ORDERING ONLY.**

This supersedes both the 2026-09-05 closest-to-target decision and this document's original
FIFO recommendation (§4.4 records both fairly, as rejected alternatives, not reopened).

**What HCBF optimises, and what it accepts as a cost — stated plainly, not oversold:**

- **What it optimises:** HCBF realizes the smallest gains PER UNIT first. Each close is, by
  construction, the lot with the highest `entry_price` (openRate) among those still open, so
  whatever gain-per-share that lot carries is realized earliest, and the lowest-`entry_price`
  (often the most gain-concentrated, since these lots have appreciated the most per share since
  entry) lots are the ones left untouched longest. Cost-per-share and current market value are
  genuinely different axes — the ordering uses the former, the sizing arithmetic (§4.6.1/§4.8)
  uses the latter, and this document no longer conflates them (§16 item 8, now resolved).
- **What it accepts as a cost, long-run:** the cheapest lots are retained over time, so
  embedded gain concentrates in what remains. This is the operator's own accepted trade‑off,
  not a defect this document found and is flagging as a problem — it is recorded here because
  the task brief asks this document to state what a decision costs, not merely what it buys.
- **Combined with partial close (§4.6.1), HCBF also minimizes lots touched per session** in
  the typical case: descending-cost-basis order means the first 1-2 lots usually cover most of
  a `sell_amount`, with at most one partial close finishing the remainder exactly — see §4.6.1
  and the worked example in §4.8.4.

### 4.4 Rejected alternatives — recorded, not reopened

- **Highest TOTAL cost basis first (`lot_annotations.entry_amount` descending)** — considered
  as the literal-sounding reading of "highest cost first" and rejected in favor of per-share
  ordering (§4.3): it **degenerates** once the book is dominated by app-created lots, since
  every one of them opens at exactly `min_order_size = $50` and therefore ties under a
  total-dollar sort, collapsing the ordering into an arbitrary tie-break. It would only ever
  meaningfully order the 202 legacy adopted DCA lots (~$202 average cost, §4.9), and that
  meaning erodes further every session those lots are sold down. **The operator's own
  $50/$50/$60 worked example does not distinguish this reading from per-share HCBF** — all
  three lots tie under a total-dollars sort (each is a $50 cost-basis lot) — so the example is
  not evidence either way for this choice on the ordering question; it only becomes decisive
  once sizing (market value, not cost basis) is considered separately (§4.3).
- **Closest-to-target (2026-09-05 operator decision, now superseded) — what it was, why it is
  no longer needed.** Closest-to-target minimized overshoot against `sell_amount` under the
  assumption that SCHG closes were always all-or-nothing (no partial close). Its stated
  rationale was that overshoot directly erodes the core floor (`0.5 × sleeve_equity`), so
  minimizing overshoot was framed as a safety property. **This document's own §16 already
  flagged (2026-09-05) that the real floor protection came from the pre-submission floor
  check, not from the choice of lot-selection policy itself** — a reservation that turned out
  to matter: once partial close is live, there is no overshoot left to minimize (a partial
  close raises *exactly* the shortfall), so the entire objective function closest-to-target
  was built around is moot. It is not being rejected because it was wrong on 2026-09-05's own
  terms; it is being rejected because the premise under it (no partial close) changed. Its
  bounded-subset-search algorithm (§4.6.2 of the pre-2026-09-06 text) is likewise now dead —
  see §4.7.
- **FIFO (oldest `entry_date` first, tie-broken by `position_id`)** — this document's original
  recommendation, rejected on 2026-09-05 and still rejected: it needs no new optimization
  code and is trivially auditable, but it ignores cost basis entirely and does not connect its
  ordering to either the core-floor headroom argument or the realized-gain-shape argument that
  now motivates HCBF. Not re-argued for; recorded as the rejected alternative.
- **HCBF was rejected as the default on 2026-09-05** (this document's original text, before
  the operator's 2026-09-06 reversal) on the grounds that concentrating which lots get closed
  first "could, without operator input, systematically liquidate the account's largest
  cost-basis lots first." **The operator has now made exactly that call, with the trade-off
  stated explicitly (§4.3)** — this is not a case of the document's prior reasoning being
  wrong; it is a case of the missing operator input (P&L/tax preference) now having been
  supplied directly.

### 4.5 The partial-close fork — resolved 2026-09-06 (was pending Friday's check)

**2026-09-05's text left open which of two structurally different algorithms was live,
pending Friday 2026-09-11's read-only `allowPartialClosePosition` eligibility check (§4.7 of
that text).** The operator's 2026-09-06 decision resolves this directly and makes the Friday
check unnecessary: **partial close is live, by operator decision, not by a broker-payload
read.** The Friday eligibility check (RUNBOOK §8(c)) is removed from Friday's session as a
result (RUNBOOK_LIVE_WINDOW_2026-09-11.md §8, updated) — not because the question stopped
mattering in the abstract, but because the operator has answered it directly and a broker-side
confirmation is no longer gating the build. (If a future session wants empirical confirmation
that eToro's API actually accepts a non-null `UnitsToDeduct` for SCHG/UPRO before this ships to
the real account, that would be a *pre-launch* verification step, not a design-blocking
unknown — flagged in §16 as a residual, lower-stakes risk.)

### 4.6 The live algorithm

#### 4.6.1 Partial close — LIVE, primary mechanism

Closing a lot is now, in the common case, **exact**: given a `sell_amount` to raise from SCHG,
the executor sorts open SCHG lots by `entry_price` (the broker's `openRate`,
`Position.entry_price` in `etoro/client.py`) descending, tie-broken by `position_id` ascending
— **NOT** by `lot_annotations.entry_amount` (total cost basis) — and walks the list. Every
arithmetic comparison below (the running need, the next lot's size, the $10 remainder) is
against each lot's **current MARKET VALUE** (units × today's fetched price —
`PositionMark.current_value`, `etoro/client.py`/`models.py::mark_position`), **never** against
cost basis. Ordering and sizing are two different fields, deliberately — see the named rule at
the end of this subsection.

1. While the running `sell_amount` remaining is **≥** the next lot's current MARKET VALUE:
   close that lot **whole** (full close, `UnitsToDeduct: null`, exactly as today), subtract its
   market value from the remaining need, advance to the next lot (in `entry_price`-descending
   order).
2. As soon as the remaining need is **smaller than** the next lot's current MARKET VALUE: this
   is the one lot that finishes the job.
   - **If `lot.market_value - remaining_need >= $10`** (the broker's `minPositionExposure`/
     `minPositionAmount` floor, both $10 for SCHG and UPRO, checked against the remaining
     MARKET VALUE, never cost basis): partially close this lot for **exactly**
     `remaining_need`, leaving a valid `>= $10` remainder. Deduction is computed as
     `units_to_deduct = remaining_need / lot.market_value * lot.units` (or the equivalent
     units-per-dollar the broker's own payload expects — the exact field name a partial-close
     request must carry needs verifying against a live payload before this ships, since
     `close_position` has never sent one; flagged, not assumed — §16).
   - **Else (the remainder would be below $10):** close this lot **whole** instead — never
     leave a sub-$10 stub. This necessarily overshoots the remaining need (the lot's market
     value exceeds it, which is why it was reached in this branch), so after this whole-close
     the remaining need is satisfied (typically with some overshoot); if by some edge case
     (rounding, a tie) the need is still not fully met, continue the loop with the next lot —
     the algorithm does not assume this branch always terminates the loop, it only expects to
     in the overwhelmingly common case.
3. **Zero net overshoot is the normal outcome** when step 2's first branch fires (an exact
   partial close); overshoot only occurs when the $10-remainder rule forces a whole close in
   step 2's second branch, and even then it is bounded by that one lot's market value.

**Named rule, stated once so an implementer cannot pick one field for both jobs: cost PER SHARE
(`entry_price`/`openRate`) decides the ORDER lots are considered in; current MARKET VALUE
(units × today's price) decides everything about SIZE** — how much a close raises, where the
running need stands, and whether the $10 floor is satisfied. Never cost basis
(`Position.amount`, `lot_annotations.entry_amount`, or `entry_price × units`) for any sizing
question; never market value for the ordering question. **Rationale:** market value is what can
actually be realized by selling, and it is the quantity the broker's $10 floor is checked
against; cost basis is a historical, entry-time number with no bearing on what a close raises
today, but it is the right axis for deciding whose gain to realize first (§4.3).

**What revisiting `close_position`'s current full-close-only scope requires — specified,
NOT implemented (no Python file may be edited by this document):**

- `etoro/client.py::close_position` (line ~1112) currently hardcodes `UnitsToDeduct: null` and
  its own docstring states this is deliberate ("partial close is explicitly out of scope for
  this app"); `CloseRequest` "refuses to construct with a non-`None` unit count." **Both of
  these must change**: `close_position` needs a new optional parameter (e.g.
  `units_to_deduct: float | None = None`) threaded into the request payload's
  `UnitsToDeduct` field, and `CloseRequest` must accept a non-`None` unit count instead of
  refusing to construct one.
- **The exact broker field name and units convention for a partial-close request is
  unverified** — today's `close_position` never sends one, so whether the payload wants
  `UnitsToDeduct` in raw units, in the same units `lot.units` already uses, or as a fraction,
  needs a live check before this ships (this document flags it; it does not assume the answer
  — consistent with this project's "green suite is evidence about your fixtures, not eToro"
  lesson, HANDOFF §1).
- **A partially-closed position stays open with reduced units, not closed.** Every place this
  design and DESIGN_market_data.md currently reason about a close as "the position disappears"
  must instead handle "the position's `units`/`amount` shrank" (§3.4, already updated above).
  Concretely:
  - `lot_annotations` needs an **update-in-place** path for a shrunk lot (not `is_open=False`)
    — closer to how a partial-fill on a *buy* is already handled, but new for the close side.
  - `engine/reconcile.py` (gate 11) needs the third invariant this document adds at §3.4/§6:
    DB `amount` vs. broker's reduced `amount`, position open on both sides.
- **Whether eToro's own `minPositionExposure`/`minPositionAmount` ($10, verified for SCHG and
  UPRO in `etoro/client.py`'s eligibility parsing) is enforced by the broker on the
  **remainder** of a partial close, not just on the closed amount, remains to be confirmed
  live** — this design's own $10-remainder rule (step 2 above) is written to never rely on the
  broker to catch this, precisely so a live surprise here degrades to "our own check was
  stricter than necessary," never to "our own check missed a broker rejection."
- **Ordering by `entry_price` needs a live per-position field `lot_annotations` does not
  persist today.** `lot_annotations` stamps `entry_amount` (total cost basis) once at open and
  never updates it (`db/repo.py::LotAnnotation`'s own docstring: "stamped once at open, never
  updated") — there is no per-unit entry-price column anywhere in that table. The broker itself
  supplies `openRate` and `units` on **every** open position on every `get_portfolio()` call
  (`etoro/client.py::_parse_position`), so the HCBF sort does **not** strictly require a schema
  change: the executor can join `lot_annotations` to the freshly fetched broker portfolio by
  `position_id` each session and read `openRate` (and current `units`, for the market-value
  sizing above) straight from the broker response. **If a future pass wants `entry_price`
  durably recorded on `lot_annotations`** — for observability without depending on a live
  broker read at that exact moment (§11), or to make the sort auditable from the database alone
  the way `entry_amount` already is — that would need a new column (e.g. `entry_price REAL`)
  added to the `lot_annotations` table, populated at the same point `entry_amount` is stamped
  today (position open, and by the adoption backfill for the 202 legacy lots, §8). **This
  document does not implement that column** — it is specified here as the one piece of storage
  the ordering rule could use but does not strictly require, per the task's scope (no Python
  file edited).

**The pre-submission core-floor check (`inputs.schg_position_value - total_raised >=
config.core_floor_pct * inputs.equity`, `core_floor_pct = 0.5`) is now EXACT, not
approximate.** Under the dead full-close-only branch (§4.7), the check could only bound
overshoot to "at most one lot's `amount`" because the selection itself was an approximation
(a subset of whole lots, never landing exactly on `sell_amount`). With partial close live and
primary (this section), the amount actually raised in the common case is `sell_amount` itself,
to the cent — so the pre-submission check compares the floor against the *exact* post-sale
`schg_position_value`, not an estimate bounded by a lot-size margin. **This is still the same
check doing the load-bearing work it always did (§16's 2026-09-05 reservation about this),
not the lot-selection policy** — HCBF does not itself guarantee the floor any more than
closest-to-target did; the guarantee comes from running this check, with real numbers, before
any order is submitted, and refusing the close outright if even the best available selection
would breach it. What changes with partial close is only that "real numbers" are now exact
rather than bounded by an unavoidable subset-sum approximation error.

### 4.7 The full-close-only subset-search branch — dead, recorded so it is not re-derived

**What it was:** the 2026-09-05 text's §4.6.2 specified a bounded, deterministic
greedy-with-one-swap subset search over whole SCHG lots (`select_schg_lots_to_close`),
for the branch where partial close was assumed unavailable — sort lots by `amount` descending,
accumulate into a candidate set, also try the single smallest lot large enough alone, also try
one bounded swap of the last-added lot, pick whichever candidate set had the lowest overshoot.

**Why it is no longer needed:** partial close is now live by direct operator decision (§4.5),
so the problem that subset search existed to solve — "closes are all-or-nothing, so
approximate the target with a combination of whole lots" — no longer exists. HCBF plus an
exact partial close (§4.6.1) replaces it with zero-overshoot exact deduction in the common
case. **This algorithm is not deleted from the project's history — it is compressed to this
note** so a future reader who sees a reference to "the bounded subset search" in an older
document or commit message knows what it was and why building it is not necessary, rather than
re-deriving a combination-search algorithm the project no longer needs.


### 4.8 The sell-to-target loop — bounded, per the operator's own concern

**The problem, stated by the operator:** a naive "keep selling until the target is met" loop
does not obviously terminate in a way safe for a 30-minute T-30 window. Each pass the
shortfall shrinks, but a tiny final top-up (say $0.20) is almost certainly un-placeable at the
broker, and every pass is another broker round-trip whose latency has **never been measured**
(DESIGN.md §1.1's own standing highest-risk unknown, unresolved until Friday's DEMO-account
measurement — RUNBOOK §8(b)).

**The design, in four parts:**

1. **A small, deliberate buffer on the first pass.** Ask for `target * (1 + buffer_pct)` on
   the first sell, not `target` itself, so the first pass almost always covers the true need
   and the loop becomes a rarely-fired safety net, not the primary mechanism.
   - **Proposed basis, stated so it can be checked, not asserted:** the closest existing
     measured number is `DESIGN_market_data.md` §12's `provisional_close_deviation_pct`
     calibration — 272 common Tiingo-raw-vs-eToro dates, mean |dev| 0.128%, max 0.640%,
     calibrated tolerance set to 1.0%. The measured SCHG carrying-fee effect
     (`FINDINGS_fees_2026-09-05.md`: 0.320%/yr annualized) is a second, much smaller
     contributor for a same-day realized sale. **Proposed buffer: 1.0% of `target`**, matching
     the already-calibrated deviation tolerance. **This is flagged, not asserted, as the right
     number** — that calibration was derived for a different purpose (provisional-close
     staleness sanity-checking, not sizing a sell to cover a cash target), and no measurement
     of the actual gap between "sell now for $X" and "cash increases by $X" exists yet. **The
     exact buffer percentage is an operator decision this document cannot derive defensibly on
     its own** — 1.0% is offered as a starting point, not a final number (§14).
2. **A hard iteration cap: 2, at most 3 passes.** Proposed as 2 (the buffered first pass plus
   one shortfall pass), with 3 as the outer bound if the operator wants one extra margin. On
   exhausting the cap while still short: **stop looping, accept the remaining shortfall** —
   phase B re-reads actual cash (as it always does, never predicted) and simply buys less,
   which the two-phase session already degrades gracefully into (§3.2).
3. **A minimum viable top-up floor.** If the remaining shortfall after any pass is below the
   $10 `minPositionExposure`/`minPositionAmount` floor this design already treats as binding
   (§4.6.1), **do not attempt another pass** — stop, accept the shortfall. A shortfall that
   small is very likely un-placeable at the broker regardless, so looping for it would spend a
   round-trip inside the T-30 window on an attempt this design's own $10 rule already expects
   to fail.
4. **Never predict proceeds, at any pass.** Every pass re-reads actual settled cash from the
   broker (`GET /balances`) before deciding whether another pass is needed and, if so, how
   large the remaining shortfall actually is — the same principle §0/§2 already applies to
   the two-phase session, applied one level down, inside phase A's own sell execution.

**The lot-selection rule inside each pass — HCBF-by-entry-price with the $10-remainder rule,
sized in market-value terms, and an ambiguity this document previously left unresolved (old
§16 item 8) that is now fixed:**

Decision D's prose ("close whole positions in cost-order until the remaining need is smaller
than the next position's value, then partially close that position for exactly the shortfall")
and the operator's own worked example are **now reconciled by the operator's 2026-09-06
clarification**: the example's `$50/$50/$60` figures are each position's **current market
value**, not cost basis — the operator states explicitly that the `$60` position is "a $50 buy
that has appreciated," meaning its cost basis is $50, tied with the other two lots, not $60.
**This document's earlier text (before this correction) mis-stated the pseudocode's sort key
as "cost-basis descending" while attaching market-value figures (`[$60, $50, $50]`) to it —
those two things cannot both be true of the same lot (cost basis and current market value are
different axes for a lot that has appreciated), and this was exactly the wrong-field bug old
§16 item 8 flagged but did not resolve.**

**Corrected reading:** the three lots are already given in HCBF order (sorted by `entry_price`
descending, tie-broken by `position_id`) — which lot has the highest per-share cost is not
determinable from the $50/$50/$60 figures alone, since those are market values, not entry
prices; the example simply asserts an order and attaches each lot's current market value to it
for sizing. Every figure in the trace below ($50, $50, $60, $51, $9, $59, ...) is a MARKET VALUE
or a cash amount — never a cost basis — consistent with the market-value sizing rule (§4.6.1).
The selection mechanics themselves (close smaller lots whole first, smallest-market-value-first,
until a legal ≥$10 partial becomes available on the largest-by-HCBF-order lot) are unchanged
from the prior text; only the field each number represents is corrected:

```
def select_and_close(need, lots_in_hcbf_order):
    # lots_in_hcbf_order: already sorted by entry_price (openRate) descending,
    # position_id ascending as tie-break -- established BEFORE this function runs,
    # not re-derived from the market-value numbers below.
    # Each lot's `.market_value` is its CURRENT market value (units x today's fetched
    # price), re-read fresh each pass -- NEVER `entry_amount` or `Position.amount`
    # (both cost basis). e.g. lots_in_hcbf_order = [lot_A($60 mv), lot_B($50 mv), lot_C($50 mv)]
    largest = lots_in_hcbf_order[0]
    smaller = lots_in_hcbf_order[1:]
    # 1. Close smaller lots whole, smallest-market-value first, only as many as needed
    #    to bring `need` down to a value that admits a LEGAL partial close on `largest`.
    for lot in sorted(smaller, key=lambda l: l.market_value):
        if need <= 0:
            break
        if largest.market_value - need >= 10.0:
            break   # a legal partial on `largest` is already available -- stop consuming
        if need >= lot.market_value:
            close_whole(lot); need -= lot.market_value
    # 2. Finish against `largest`: partial if legal (by market value), else close it whole too.
    if need > 0:
        if largest.market_value - need >= 10.0:
            partial_close(largest, need); need = 0
        else:
            close_whole(largest); need -= largest.market_value   # need now <= 0 (or,
                                                                   # rarely, still > 0 --
                                                                   # continue the loop over
                                                                   # any further lots)
    return need   # <= 0 means met
```

**Trace of the operator's own example — need $51, positions with market values $50, $50, $60
(already given in HCBF order; no buffer, for clarity — the buffered version follows):**

1. `largest = $60` (market value), `smaller = [$50, $50]` (market values), `need = $51`.
2. Step 1: is a legal partial on `$60` already available? `$60 - $51 = $9 < $10` — no. Close
   the first `$50` lot whole (smallest market value available). `need = $51 - $50 = $1`.
3. Recheck: is a legal partial on `$60` now available? `$60 - $1 = $59 >= $10` — yes. Stop
   consuming smaller lots.
4. Step 2: partial-close `$60` for exactly `$1` (market value), leaving `$59` (market value).
5. **Result: close the $50 whole, take $1 from the $60, leaving $59. Matches the operator's
   stated answer exactly — confirmed expressed in market-value terms.**

**With the buffer applied** (target $51, proposed buffer 1.0% → ask $51.51 on pass 1): same
mechanics — close the $50 whole (`need = $51.51 - $50 = $1.51`), then partial-close the $60
for $1.51 (market-value remainder $58.49, still ≥ $10). Met in one pass; no shortfall pass
needed.

### 4.9 Lot-size reality — the book is bimodal, and order count grows over time

**Do not assume uniform $50 lots.** The book today is bimodal, and this shapes how many
orders a single sell touches, which in turn shapes how much the sell-to-target loop's per-pass
round-trip latency (§4.8, unmeasured until Friday — RUNBOOK §8(b)) actually costs in wall
clock time inside the T-30 window:

- **The 202 existing adopted SCHG positions** came from the operator's manual DCA and average
  **~$222 market value** (~$44,867 total, ~$40,867 cost basis) — not $50.
- **Future app-created positions** open at `min_order_size = $50` (unchanged, not reopened by
  this update) and grow in value over time as the ladder adds to them.

**Consequence, stated as a scaling risk, not solved by this document:** a sell today, against
the ~$222-average adopted book, touches roughly 2-3 lots for a typical `sell_amount`. As the
book fills with more $50-origin lots over the strategy's life, the *same* dollar
`sell_amount` will touch roughly 10 lots instead — an order-count (and therefore
round-trip-count, and therefore latency) growth that is **structural**, not transient, and
that **multiplies the still-unmeasured per-order latency** (§4.8/RUNBOOK §8(b)) as the book
ages.

**UPDATED, operator decision 2026-09-06: `max_orders_per_session` is retired, replaced by a
latency-derived time gate — see DESIGN.md §6.C for the full reasoning.** An arbitrary order-count
cap was the wrong mechanism: buying is already bounded by the sleeve-cash re-check (DESIGN.md
§6.B), selling is already bounded by the core-floor check (`strategy/ladder.py:926-929`), and
with both dollar totals capped, order *count* is implied by `sleeve_cash / min_order_size` — a
second count cap added no safety. The only thing genuinely unbounded was **wall-clock time**
inside the T-30 window, and a count is a poor proxy for time. The replacement — before each order,
require `now + measured_p95_latency < hard_deadline`, else stop and accept the shortfall — is
exactly the mechanism this section's scaling risk needed: it bounds *time* directly rather than
bounding *count* and hoping count tracks time, so it self-adjusts as the book's growth (this
section's own finding) changes how many orders a given `sell_amount` touches, without needing a
human to re-tune a constant as that growth plays out. `tools/probe_order_latency_2026-09-11.py`'s
`project_session_budget` function computes exactly this projection against the 2-3-lots-today /
~10-lots-mature curve documented above. **DESIGN.md §6.C states plainly that this time gate is
deliberately simple scaffolding — expected to be redesigned, not a settled architecture** — that
caveat applies here too: this section's scaling risk is what the gate exists to answer, not
something this document considers closed by the gate's current shape. A separate small integer
circuit-breaker against a bug-driven absurd intent count may still exist, sized off `MAX_LOTS`,
explicitly not a tuning knob (DESIGN.md §6.C). This retires the corresponding half of §14 item 5
below — see that item for what remains open (`max_notional_per_session_usd`, `max_open_lots`, the
daily loss guard, all untouched by this decision).
---

## 5. The session sequence

Reconciling DESIGN.md §3.1 (the 12-step list at "T-15min... [1]..[12]") and §4.1 against the
ladder's two-phase requirement (§3) and `engine/session.py`'s own current TODO comment:

```
T-15min  Wake, foreground service starts, PARTIAL_WAKE_LOCK acquired (released in finally)
         Warm exchange_calendars (§9) — first call of the process, absorbs the 13-19s cold
         start here, off the trading-decision critical path
         ↓
  [1]    PREFLIGHT — gates 1-9, 11, 12 (§6). Gate 10 reruns again just before [4] below,
         since price data can go stale between preflight and the trade window even when it
         passed here (see §6's note on gate 10 running twice)
         ↓  any hard-fail → Telegram with gate + reason, HOLD the wake lock, do not return
            to Doze, stop (no order placed, no state mutated)
  [1b]   BOOTSTRAP CHECK — has the adoption migration (§8) run and been verified? If not,
         run it now, once, before anything else touches lot_annotations. If it has already
         run, this step is a fast no-op (a single COUNT query) every subsequent session
         ↓
  [2]    SLEEP until exactly T-30min (market.calendar, honouring 13:00 ET half-days) —
         DESIGN.md §3.1's step [2] ("settle previous bar") is GONE per DESIGN_market_data.md
         §3.2; there is no `bars` table and no settle step in this design. Flagged: DESIGN.md
         §3.1 itself is stale here and this document does not silently follow it — see the
         contradiction table below
         ↓
  [3]    FETCH — Tiingo history (SPY/UPRO/SCHG) + Tiingo IEX provisional close + eToro
         candles cross-check + eToro live rates (sanity only, never the provisional close)
         ↓
  [4]    GATE 10, RE-RUN — freshness/sanity on the just-fetched data (§6). This is the
         second and authoritative gate-10 pass; the [1] pass is a coarse early check so a
         dead network fails fast before the T-30 sleep, not a substitute for this one
         ↓  fail → halt exactly as [1] does
  [5]    DERIVE — market.window.assemble_spy_signal(): cone_5_z (adjClose-based),
         SpyContext (ath/close/drawdown/tier, raw-close-based, floored by open lots' stamped
         entry_spy_ath per HANDOFF §3a). Read open_lots' entry_spy_ath from lot_annotations
         BEFORE calling this — the no-default parameter is not optional (market/window.py's
         own docstring: passing it is mandatory, not a style preference)
         ↓
  [6a]   PHASE A DECIDE — strategy.ladder.decide(inputs, state, config) using
         pre-sale cash/equity/open_lots. Persist decision row (raw LadderDecision, marked
         "phase A, pre-settlement — buy intents provisional")
         ↓
  [7a]   ARM GATE — mode==live AND NIGHTWATCH_ARM set AND no HALT. Checked once, reused for
         phase B too (re-checking mid-session would let an operator's mid-run /halt silently
         orphan a half-executed sale batch; see §11 on why arming is a single per-session
         decision, not per-phase)
         ↓  not armed → log + Telegram "would have done X" using phase A's FULL decision
            (both sells and buys, since this is the one place a dry-run report is allowed to
            show the buys that never got the real cash re-derivation — it is disclosed
            language, not a real execution), stop cleanly (successful, idempotent session)
  [8a]   EXECUTE PHASE A — sells only (UPRO closes + SCHG "fund ladder buy" close, resolved
         to specific position_ids per §4). insert_intent BEFORE each HTTP call. Poll each to
         terminal, bounded timeout
         ↓  any order still non-terminal at timeout → skip to [9a]-partial, see §3.4
  [9a]   PERSIST PHASE A — closed_lots, updated_buckets (bucket tranche_amount from
         estimated proceeds, §3.3), the sell-driven portion of new_state (sale_batch_
         sequence/date, recyclable_cash as computed from phase A's own proceeds estimate —
         corrected implicitly once phase B re-derives cash, never re-written here)
         ↓
  [6b]   RE-DERIVE CASH — GET /balances (documented formula, never totalBalance), re-mark
         schg_position_value/equity off the SAME prices [3]/[5] already fetched (no re-fetch)
         ↓
  [6c]   PHASE B DECIDE — strategy.ladder.decide() again, same LadderState as phase A left
         it (bars/ath/tier unaffected by phase A), inputs.cash/equity/open_lots refreshed.
         Persist decision row ("phase B, post-settlement — authoritative")
         ↓
  [7b]   Reuse [7a]'s arm decision — do not re-check
         ↓
  [8b]   EXECUTE PHASE B — buy intents (UPRO ladder/re-entry, SCHG core), plus any close
         intent phase B itself proposes (executed, not looped into a phase C, §3.1 step 11)
         ↓
  [9b]   PERSIST PHASE B — new_lots, final new_state (bars incremented exactly once total
         across both decide() calls — see §7's persistence-ordering note), portfolio snapshot
         ↓
  [10]   TELEGRAM report — both phases summarised as one session narrative (sells: N/M
         terminal; buys: sized off settled cash of $X), or the daily heartbeat if truly
         nothing happened in either phase
         ↓
  [11]   RESCHEDULE next session (before releasing anything — a crash here must not orphan
         the alarm)
         ↓
  [12]   Release wake lock, stop foreground service, return to Doze
```

### 5.1 Contradictions found between the docs and the code — flagged, not smoothed over

1. **DESIGN.md §3.1 step [2] ("SETTLE — overwrite yesterday's provisional bar...") is dead.**
   `DESIGN_market_data.md` §3.2 deletes the `bars` table, `is_provisional`, and
   `settle_previous()` entirely ("impossible — the provisional close lives in memory for one
   session and is never persisted"). DESIGN.md §3.1 was never updated to remove this step.
   This design's sequence above has **no** settle step, matching the newer, market-data
   document — DESIGN.md §3.1 is the party that is wrong here.
2. **board nightwatch/handoff-2026-09-05-evening.md §6 item 13 says `engine/session.py`'s step [5] comment is
   stale against §3.1's actual sequence** — confirmed directly: `engine/session.py`'s own
   TODO comment (as it stands today) says `[5] strategy.cone.cone_5_z() over settled+
   provisional history`, but there is no "settled" history anymore (item 1 above) — the
   comment should read "over the freshly fetched, discarded-after-use window." This document
   does not silently fix the stub's comment (out of scope — "do not implement anything"); it
   records the discrepancy here so whoever builds step 6 replaces that stale comment rather
   than trusting it.
3. **DESIGN.md §4.1's step numbering assumes ONE decide/execute pass** (`[6] DECIDE`,
   `[8] EXECUTE`). This document's two-phase session (§3) does not fit that numbering — steps
   6 and 8 each split into an "a"/"b" pair. This is a genuine, necessary departure from
   DESIGN.md §4.1, not an oversight: DESIGN.md §4.1 predates the ladder's cash-recycle
   semantics being fully worked through against a real async broker, and this document is
   the first to reconcile them. DESIGN.md §4.1 should be updated to reflect this two-phase
   structure the next time that document is touched; this design does not edit it.
4. **DESIGN.md §5's preflight gate table still describes gate 10 under the superseded
   `bars`-table architecture** (the row is marked "⚠️ STALE — superseded by DESIGN_market_
   data.md §6" in DESIGN.md's own text) — already flagged in DESIGN.md itself, carried
   forward here rather than re-litigated: this design's gate 10 references are to
   `engine/preflight.py`'s actual implementation (items 1 and 3 built; items 2/4/5 stubs),
   never to DESIGN.md §5's stale prose.
5. **`DESIGN_sandbox.md`'s ladder-adapter description matches `strategy/ladder.py`'s SCHG
   dollar-granularity scope boundary exactly** — no contradiction found here; both documents
   agree the ladder does not track SCHG lots. Recorded as a checked-and-consistent point,
   not a gap.

---

## 6. Preflight integration

Gate numbering and descriptions per DESIGN.md §5 / DESIGN_market_data.md §6, cross-checked
against `engine/preflight.py`'s actual (partial) implementation.

| # | Gate | Runs before | Fail-closed or fail-open | Reasoning |
|---|---|---|---|---|
| 1 | Halt switch | step [1] (first) | **Fail-closed** — halt, hold wake lock | An explicit operator halt must never be second-guessed by any other check. |
| 2 | Power & host permissions | step [1] | **Fail-closed** | Losing the battery exemption or `SCHEDULE_EXACT_ALARM` silently kills future runs (DESIGN.md §5 "gate 2 deserves emphasis") — must halt loudly now while the app can still alert. |
| 3 | Network | step [1] | **Fail-closed** | No path to eToro means no trade is possible at all; nothing downstream can proceed. |
| 4 | Clock | step [1] | **Fail-closed** | A skewed clock corrupts every timestamp comparison this design relies on (order polling deadlines, staleness checks in gate 10, T-30 sleep timing). |
| 5 | Database | step [1] | **Fail-closed** | An unfinished prior session or schema mismatch means state cannot be trusted — trading on unreadable/ambiguous state is worse than not trading. |
| 6 | Auth | step [1] | **Fail-closed** | No authenticated session, no order can be placed or verified. |
| 7 | Environment match | step [1] | **Fail-closed** | A real/demo mismatch is a "real money placed by accident" or "thinks it traded but didn't" class of bug — DESIGN.md §6.F treats this as structural, not advisory. |
| 8 | Instruments | step [1] | **Fail-closed** | If `allowOpenPosition` is false or `minPositionExposure > order_size`, no valid order can be constructed for that symbol this session — halting the WHOLE session (not just that symbol) is deliberate, because UPRO/SCHG/SPY all feed one shared decision (`decide()` needs all three prices); a partial gate-8 failure is treated as a whole-session failure, not silently degrading to "trade what's left." |
| 9 | Calendar | step [1], again effectively at step [2] (T-30 sleep math) | **Fail-closed** | Not a trading day (or T-30 miscalculated) means no order should be attempted; this also has the unhandled-failure-mode-above-it noted in DESIGN.md §5 (module-import-time `ZoneInfo` construction) — carried forward as a tracked risk (§16), not solved here. |
| 10 | Price data (freshness/sanity, items 1/3 built; 2/4/5 stubs) | step [1] (coarse) **and** step [4] (authoritative, post-fetch) | **Fail-closed** | This is the gate DESIGN_market_data.md calls "the highest-value single fix" — §2.9's UPRO 19-hour-stale-quote bug is exactly what this catches. Running it twice (§5's sequence) is deliberate: a pass at [1] does not guarantee freshness at [4], since price data can go stale in the minutes between preflight and the T-30 fetch — only [4]'s result gates the actual decide() call. |
| 11 | Reconciliation | step [1b] (bootstrap, §8) then step [1] proper, every session | **Fail-closed**, with the invariant set extended twice (§3.4): an unknown broker position in a managed symbol, a DB lot with no matching broker position, a `lot_annotation` marked closed whose broker position is still open (an assumed-complete close that wasn't), **and now also (2026-09-06)** a `lot_annotation` whose recorded `amount`/`units` no longer matches the broker's own reduced figure for a position that is open on both sides (the normal outcome of a deliberate partial close, §3.4/§4.6.1 — not an anomaly to catch, but a state to reconcile correctly) — all four checked. A position in a non-managed symbol is display-only, never a failure (DESIGN.md §6.D). |
| 12 | Risk caps | step [1], and re-checked implicitly by phase A/B's own per-order cash re-checks (DESIGN.md §6.B: "each order re-checks remaining sleeve cash immediately before submission") | **Fail-closed** for the session-level cap (order count/notional); the per-order cash re-check inside execution is a **narrower, always-on** guard layered on top, not a separate gate. | Session-level caps are a deliberate second, independent backstop (DESIGN.md §6.C) beyond the strategy's own `MAX_LOTS`. |

**On "halt the session" vs. "skip trading but still persist" — stated once, applied
uniformly.** Every gate above is fail-closed to the **same** action: abort before step [2],
hold the wake lock, alert, write every attempted `PreflightCheck` row (so the audit trail
exists even for a halted run), and reschedule normally (a halted session still needs
tomorrow's alarm — see §11). **No gate in this design is "fail-open"** — DESIGN.md never
describes one, and this document does not introduce one: real money and a phone nobody is
watching make "skip trading but still persist positions we can't verify" categorically worse
than "do nothing today." The one place something *does* proceed despite an incomplete
picture is the **arming** gate ([7a]/[7b], not numbered among the twelve) — an unarmed
session still runs preflight, decide(), and reports what it *would* have done, precisely
because that is not a failure state, it is the deliberately-safe default (DESIGN.md §6.E).

---

## 7. State persistence

### 7.1 `LadderState.bars` — the trap, addressed exactly

`strategy/ladder.py::decide` increments `bars` by exactly one per call and has **no memory of
its own** between calls (`strategy/state.py`'s own docstring: "the ENTIRE burden of 'does
bars actually persist' falls on whoever calls decide"). This design calls `decide()` **twice**
per session (phase A, phase B). **`bars` must be incremented exactly once per trading session,
not once per `decide()` call**, or the counter would silently drift to roughly 2x real
elapsed sessions.

**Resolution:** phase A's `LadderState` input is loaded once, at the top of step [5]/[6a], via
`strategy.state.load(repo)`. Phase A's `decide()` call returns `new_state` with
`bars = old_bars + 1`. **Phase B's `decide()` call must be given phase A's returned
`new_state` as its own `state` argument (not the original pre-phase-A state), and the
persistence layer must NOT call `strategy.state.save()` between phase A and phase B** — only
once, after phase B, with phase B's `new_state`. Since phase B's `decide()` also increments
`bars` by one off whatever it is handed, feeding it phase A's *already-incremented* `new_state`
would double-count. **The actual rule: phase B is handed a `LadderState` built from phase A's
`new_state` but with `bars` held at the pre-phase-A value** (i.e., phase B's own `bars`
increment is the one that counts once for the day) — equivalently, phase A's returned `bars`
field is discarded and only its other fields (`ath`, `recyclable_cash`, `sale_batch_sequence`,
etc., §3.1 step 9) are carried into phase B's input state. This is a specific, easy-to-get-
backwards detail, and it is called out explicitly here because it is exactly the kind of
one-line arithmetic error this project's signature failure mode (HANDOFF §1) would let slip
through a test that only ever exercises a single-phase call.

`config.StrategyConfig.warmup_bars = 0` in live config makes the bar-count *gate* itself
inert regardless of this arithmetic (`cone_5_z`'s own `None`-until-265-bars is what actually
gates trading, per `strategy/ladder.py`'s own docstring) — but `bars` is still load-bearing
for diagnostics and for any future config change that raises `warmup_bars` above zero, so
getting the double-increment right matters even though nothing currently depends on its
exact value for gating.

### 7.2 Exact write points, relative to order execution

| Write | When | Why then, not earlier/later |
|---|---|---|
| `intents` row (per order) | **Before** the HTTP call, both phases | The idempotency anchor (DESIGN §5/§6.H) — must exist before any network round-trip that could be lost. |
| `orders` row + status history | After each poll, both phases | Recorded as observed, not assumed; a partial fill's actual units are what gets written, never the requested amount. |
| `lot_annotations` (full close: `is_open=False`; **partial close (now the primary SCHG mechanism, §4.6.1) or a broker-side partial fill: update `amount`/`units` in place, stays `is_open=True`**, §3.4) | **After** phase A's closes reach terminal — **before** phase B re-derives cash | Phase B's cash/equity re-derivation and its `open_lots` rebuild both depend on the sidecar already reflecting phase A's closes, including any shrunk-but-open lot from a partial close; writing this after phase B would let phase B compute against stale lot state. |
| `buckets` (new re-entry bucket from a new-ATH sale) | Same point as the row above — end of phase A | Bucket creation is part of phase A's sell-side outcome (§3.3); it does not depend on phase B at all. |
| `strategy_state` (`ath`, `recyclable_cash`, `sale_batch_sequence/date` as phase A left them) | **Not written yet** — held in memory, passed into phase B (§7.1) | Writing an intermediate, phase-A-only state to the singleton row and then overwriting it again after phase B is unnecessary I/O and, worse, creates a window where a crash between phase A and phase B leaves `strategy_state` reflecting a sell that has not yet been followed by its paired buy decision — see §7.3 for why this is exactly the crash case that must be handled explicitly. |
| `lot_annotations` (new opens from phase B) | After phase B's buys reach terminal | Same reasoning as the close row above, mirrored. |
| `strategy_state` (final, phase-B `new_state`, `bars` incremented exactly once) | **Last**, after every phase-B order is either terminal or the session has decided to stop trying (§3.4) | This is the row whose staleness causes the warmup trap (§7.1) — it must reflect the FULL session's outcome, written exactly once, after both phases are done, never partially. |
| `portfolio_snapshots` | After the state write above | A snapshot is a summary of "what is true now" — it must reflect the final, fully-persisted state, not an intermediate one. |

### 7.3 Crash mid-session — what must not happen, and how this design prevents it

**The failure this design must rule out: losing or double-counting a sell or a buy.**

- **Crash after a phase-A close reaches terminal but before `lot_annotations`/`buckets` are
  written (§7.2's third row):** On restart, the next session's gate 11 (reconciliation)
  catches this directly — the broker position is gone (closed) but the sidecar row still
  says `is_open=True`. This is exactly the "DB lot with no matching broker position" halt
  DESIGN_market_data.md §4.5 already specifies; no new mechanism is needed, only the
  discipline (already stated in §6) that gate 11 runs on *every* session, including the one
  immediately following a crash.
- **Crash after phase A's persistence (§7.2) but before phase B ever runs (`strategy_state`
  never gets its phase-A-derived intermediate fields, by design — §7.2's note on why that
  row is deliberately not written yet):** On restart, the next session's phase A runs fresh:
  it reads the CURRENT broker cash/equity/open_lots (post phase-A-sell, since that already
  landed) and the LAST FULLY-PERSISTED `LadderState` (from the session before the crashed
  one — since the crashed session's `strategy_state` write never happened). This is
  **exactly equivalent to the crashed session's phase A having simply not run its buy side
  yet** — precisely the same "sell now, buy next session" fallback path §3.4 already
  specifies for a non-terminal order, arrived at via a different trigger (a crash instead of
  a timeout). No separate crash-recovery code path is needed because this design's ordinary
  degraded path already covers it. `bars` is off by one relative to true elapsed trading
  sessions in this scenario (the crashed session's phase A never got to increment it,
  correctly, because `strategy_state` was never written) — an acceptable, self-correcting
  drift given `warmup_bars=0` makes the counter's exact value inert for gating (§7.1) and the
  next real session increments it normally from a **correct** starting point (the crash did
  not lose the increment; it simply never happened, and re-deciding from scratch is correct
  behavior, not a state-corruption case).
- **Crash after `intents` is written but before the HTTP call completes (either phase):**
  Already solved by the existing idempotency design (DESIGN §6.H) — `recover_by_reference_id`
  on restart determines whether eToro ever saw the request; this document adds no new
  mechanism here, it only confirms the two-phase structure does not weaken it (each phase's
  intents are still written before their own HTTP calls, individually).
- **Double-counting**, specifically: could a crash-and-restart ever cause the SAME sell or
  buy to be submitted twice? No — the `intents`/`orders`/`referenceId` recovery path
  (DESIGN §6.H, unchanged by this design) is checked **before** any new HTTP call in a
  restarted phase A or phase B, exactly as it would be checked before any single-phase call.
  The two-phase structure does not introduce a new double-submission risk because both
  phases route through the same `executor.execute()` idempotency anchor.

---

## 8. The adoption migration bootstrap

**Finding restated:** `tools/adopt_backfill_runner.py`'s dry run confirms there is currently
**no** `lot_annotations` row on the device at all (0 of 209 managed-symbol positions have
one) — the app would halt on gate 11 every session until this migration runs. The migration
(`market.adopt.run_adoption_backfill`) is **not re-runnable to correct**: it treats an
already-annotated `position_id` as "done" and silently skips it (`already_annotated` set,
§4.7 of DESIGN_market_data.md / `build_adoption_plan`'s own contract) — so a wrong `book`
assignment or a bad `entry_context_source` written once cannot be fixed by re-running the
same function; it needs a manual `UPDATE`/`DELETE` against `lot_annotations` first.

### 8.1 Where it runs

**As the app's own one-shot bootstrap, at step [1b] of the session sequence (§5), on the
device, against the device's own `nightwatch.db`.** Not a laptop-driven write — the task
brief is explicit about this, and it matches the dev-machine's own hard constraint (the dev
PC cannot reach eToro at all, DESIGN_market_data.md §2.8, so any laptop-driven migration
against the *live* account was never going to be possible from there in the first place;
`tools/adopt_backfill_runner.py` is offline-and-Tiingo-only by construction for exactly this
reason and was always a **dry-run** verification tool, never the production migration path —
its own docstring: `dry_run` is hardcoded `True`, "never parameterised... there is no flag...
that can flip it").

### 8.2 Exactly once, safely

Step [1b], concretely:

1. `has_run = repo.table_names()` includes `lot_annotations` **and** a row count > 0 **and**
   a dedicated marker (see below) — not just "table exists," since the table exists from
   schema migration regardless of whether the backfill itself has run.
2. **Dedicated marker, not a row-count heuristic alone:** a single row in a small
   `migrations_run` (or reuse of the existing schema-migration bookkeeping if
   `db/migrations/` already has a suitable table — not inspected in depth here, flagged as an
   implementation detail for whoever builds this) recording `adoption_backfill_completed_at`.
   A row-count check alone is fragile against the edge case of a portfolio that genuinely
   holds zero adoptable positions at bootstrap time (not this account's actual state today,
   209 positions, but not a case this design should silently mishandle) — an explicit marker
   distinguishes "ran, found nothing to adopt" from "never ran."
3. If not yet run: fetch the current broker portfolio (`get_portfolio()`), fetch SPY history
   from `FULL_HISTORY_START_DATE` (already required for step [5]'s signal derivation anyway —
   **this fetch is not additional**, it is the same SPY window step [3]/[5] already pulls;
   sequence step [1b] runs logically before [2]'s sleep specifically so this reuse is
   possible, since the SPY window does not depend on T-30 timing at all), and call
   `run_adoption_backfill(repo, positions, spy_bars, dry_run=False)`.
4. **On `AdoptionError`** (any unresolved position): **halt the session**, exactly like a
   preflight gate failure (§6's fail-closed rule) — do not proceed to trade with 209
   positions still unreconciled. Alert to Telegram naming the count and reasons (`AdoptionPlan
   .report()` already produces this text). This is a one-time, operator-attended event in
   practice (the position count is fixed at 209 unless new positions are opened outside the
   app before this runs, which should not happen if the app is the only writer) — a repeated
   nightly halt on the same unresolved positions is itself a signal something needs manual
   intervention, not a transient condition to retry silently.
5. On success: write the completion marker, **then** proceed to step [2] normally, in the
   **same session** — this is deliberately not a separate wake or a separate day; it is
   the first session's step [1b], adding perhaps a few seconds (209 positions, one SPY fetch
   already needed) to the T-30 budget, comfortably inside the 30-minute margin.

### 8.3 Verified afterward

- **Immediately, same session:** gate 11 (§6), which runs at step [1] for every subsequent
  session, is the natural verifier — if the backfill left any managed-symbol position
  unannotated, gate 11 fails the very next time it runs (the same session, if step [1b]
  precedes step [1] in wall-clock terms — see the note below on ordering — or the next
  session if not). **This design places step [1b] before step [1]'s reconciliation check
  specifically so a broken bootstrap is caught before any trading is attempted in the same
  run it happened in**, not one day later.
- **Explicit count check:** after `run_adoption_backfill` returns successfully, compare
  `len(repo.list_lot_annotations())` against the fetched `len(positions)` restricted to
  managed symbols — they must match exactly (209 for the count observed at design time; the
  live figure at whatever moment this actually runs). A mismatch with no `AdoptionError`
  raised would itself be a bug in the migration's own accounting and is worth a defensive
  assertion here, cheap insurance given step 4 above already treats any unresolved position
  as fatal.
- **Manual/human verification, once:** the operator should independently sample a handful of
  adopted rows (e.g. via the LAN-only debug webapp, §9 of DESIGN.md, once it exists, or a
  direct SQLite read) and confirm `book='catapult'` (the only value the migration ever
  assigns, per §4.7) and a plausible `entry_tier` against a position they recognise — this is
  a one-time sanity check this document recommends but cannot itself perform.

---

## 9. The `exchange_calendars` cold start

**Measured:** 13-19s for the first calendar call in a fresh process, ~13-22ms thereafter
(task brief). Against a 30-minute T-30 budget this is not a schedule risk on its own — but it
recurs on every process restart, and vivo's background killer (DESIGN.md §1.2, "the weakest
link, chosen knowingly") makes process restarts between nightly sessions a real, not
theoretical, occurrence.

**Specification: warm it at wake-service start, not at step [1] or step [9] of the session
sequence.** Concretely, `SessionService.kt`'s Kotlin entry point, immediately after acquiring
the partial wake lock and before invoking any Python entry point that needs a trading
decision, calls a dedicated Python function (e.g. `market.calendar.warm()`) whose only job is
to construct the `exchange_calendars` XNYS calendar object once and discard it — paying the
13-19s cost **before** gate 9 (calendar) or the T-30 sleep math needs a fast answer, and
overlapping it with the network-reachability work of gates 2-3 rather than serialising after
them. This is a small, deliberate scheduling choice: it costs nothing extra (the calendar
must be constructed at least once regardless), and it means gate 9's own calendar check and
the T-30 sleep computation both benefit from the already-warmed ~13-22ms path rather than
each independently risking being the one that eats the cold-start cost during a
latency-sensitive step.

**Also warmed here, for the same reason, by the same mechanism:** nothing else needs an
explicit warm — `market/calendar.py`'s only other module-level cost noted anywhere in the
docs is the `ZoneInfo("America/New_York")` construction at import time (DESIGN.md §5's "gate
9 has an unhandled failure mode above it, not within it" — tracked as a pre-existing risk,
not solved by this document, since it is a Python-import-time cost that happens before any
Kotlin-side warming call could run at all; flagged again in §16).

---

## 10. The single-session constraint — reaffirmed

The SCHG all-time-high test (the `new_ath_bar` check inside `decide()`, driving the SCHG
core-buy step) uses **the previous day's settled bar**, evaluated inside the **same** T-30
session this design specifies — DESIGN.md §4.2a's decision, unchanged and not reopened here.
This document's two-phase session (§3) does not add a second wake, a second schedule, or a
second cash-sleeve consumer: both phases run inside the **one** session this constraint
already governs. A second daily schedule was proposed and rejected (battery, another surface
for vivo's background killer, two sessions spending one cash sleeve) — nothing in this
design's two-phase structure revisits that rejection; the two phases are two `decide()`
calls and two executor passes **within** one wake, one foreground service lifetime, one wake
lock hold.

---

## 11. Idempotency and observability

**Idempotency.** One session per `trading_date` (DESIGN.md §6.I, `sessions.trading_date`
UNIQUE, `already_ran(trading_date)` flag) is unchanged by the two-phase structure — the whole
two-phase sequence (§5) constitutes **one** session row, checked once at the top (before
step [1]) via `get_session_by_date`. A safety-net-alarm re-fire that lands on an
already-completed `trading_date` is a no-op, exactly as before. A re-fire that lands on a
`trading_date` whose session is **unfinished** (crashed mid-phase, §7.3) does **not** start a
fresh session from scratch — it resumes: `intents`/`orders` recovery (§6.H, unchanged)
handles any in-flight HTTP call, and the crash-recovery reasoning in §7.3 handles which
phase effectively gets retried.

**Arming is per-session, not per-phase (§5's [7a]/[7b] note).** A single arm check at the top
of execution, reused for both phases, is a deliberate simplification: re-checking `HALT`
between phase A and phase B would create a scenario where phase A's sells already executed
(irreversible — a full close cannot be undone) but phase B's buys are skipped because the
operator halted mid-session, leaving cash sitting un-recycled. That is a safe outcome (cash
sitting idle is never a hazard the way an unexpected buy would be) but it is *inconsistent*
with how `HALT` is documented elsewhere (DESIGN.md §6.I: "every kill switch can only stop
trading, never start it") — a halt that only stops the *buy* half but not a *sell* already in
flight is exactly "stopping trading," so this is consistent, not a loophole. Restated for
clarity: **a mid-session `/halt` cannot recall phase A's already-submitted sells** (they are
already accepted by the broker before the operator's command could possibly reach the
device — Telegram polling is not sub-second, DESIGN.md §8.2), but it **does** prevent phase
B from ever running, which is the one thing still controllable at that point.

**Observability — what must be reconstructible after the fact.** Every one of the following
must be readable from the database alone, without needing to ask the phone anything, per the
existing audit-trail discipline (DESIGN.md §3's table) extended for the two-phase structure:

- Which `decide()` call (phase A or phase B) produced which persisted `Decision` row — a
  `decisions` table `phase` column (`'A'`/`'B'`) is a small, necessary addition this design
  requires beyond what's documented today, since DESIGN.md's `decisions` table (§3) was
  specified for a single-decision session.
- The estimated proceeds phase A used for bucket sizing (§3.3) **vs.** the actual settled
  cash phase B observed — both numbers, not just the final one, so a future reader can
  quantify how large the estimate-vs-actual gap actually was in practice (this is the kind
  of measurement this project has repeatedly found necessary rather than assumed — HANDOFF's
  own recurring "measured, not waved away" discipline).
- For every order: which phase it belongs to, its full status history, and — for a
  SCHG close — which lot-selection policy chose it and why (§4's HCBF rationale, §4.3/§4.6.1),
  whether it was a full or partial close and, for a partial close, the exact `units_to_deduct`
  and the resulting remainder, should be a literal string in the `reason` field of the
  persisted `Intent`, not just implicit in code.
- **New, 2026-09-06: which sell-to-target pass (§4.8) an order belongs to** (pass 1, buffered;
  pass 2, shortfall; etc.), the target and buffer used for that pass, the actual cash read
  back after it, and whether the loop terminated by "met," "accepted shortfall — cap hit," or
  "accepted shortfall — below minimum top-up" — this is exactly the kind of outcome that must
  be distinguishable from the database alone, not inferred from the fact that a buy afterward
  was smaller than expected.
- Whether the adoption bootstrap (§8) ran this session, and its outcome, even when it was a
  fast no-op (a log line or a lightweight `preflight_checks`-style row is sufficient — this
  does not need its own dedicated table if `migrations_run`'s single marker row is enough,
  per §8.2 point 2).

---

## 12. Module layout

```
engine/
  session.py       # run_session(trading_date) -- the orchestrator this document specifies.
                    # Owns: session dedup (get_session_by_date), the bootstrap check (§8),
                    # the two-phase loop (§3/§5), arming (once, §11), rescheduling, Telegram
                    # summary. Delegates everything else.
  preflight.py      # gates 1-9, 11, 12 + run_preflight() -- THIS DOCUMENT DOES NOT BUILD
                    # THESE; only specifies which gate blocks which step (§6). Gate 10 is
                    # already built (items 1/3); this design calls it twice (§5).
  reconcile.py      # gate 11's engine -- THIS DOCUMENT DOES NOT BUILD IT; specifies the
                    # invariant set (§3.4/§6), now four-way as of 2026-09-06: the two
                    # DESIGN_market_data.md §4.5 invariants, the assumed-complete-close one,
                    # and the new "DB amount vs. broker's reduced amount, position open on
                    # both sides" invariant a deliberate partial close normally produces.
  executor.py       # execute(intent) -- THIS DOCUMENT DOES NOT BUILD IT; specifies (a) that
                    # it is called once per phase, not once per session (§3), and (b) the
                    # SCHG lot-selection + sell-to-target loop (§4) it must implement for a
                    # "close" intent with no position_ref -- a new function, e.g.
                    # `raise_target(target, open_schg_annotations, buffer_pct, max_passes,
                    # min_topup) -> list[position_id]`, implementing HCBF + the $10-remainder
                    # rule + the buffered/capped meet-the-target loop (§4.3/§4.6.1/§4.8) --
                    # this IS buildable now (2026-09-06): partial close is live by direct
                    # operator decision, not pending Friday's eligibility check, which is
                    # removed from the runbook (RUNBOOK §8, §4.5 of this document) -- what
                    # remains open before building is `etoro/client.py::close_position`
                    # actually accepting a non-null `UnitsToDeduct` (§4.6.1's specified,
                    # not-yet-implemented change) and the exact broker field/units convention
                    # for a partial-close request (unverified, flagged in §4.6.1) -- belongs
                    # here or in a new small helper module `engine/lot_selection.py` if
                    # executor.py otherwise stays broker-protocol-only -- left as an
                    # implementation-detail choice for whoever builds step 6.
  bootstrap.py      # NEW, this document's addition -- the one-shot adoption-migration
                    # runner for on-device use (§8), thin wrapper around
                    # market.adopt.run_adoption_backfill(dry_run=False) plus the completion-
                    # marker read/write (§8.2). Distinct from tools/adopt_backfill_runner.py,
                    # which stays a laptop-side, Tiingo-only, dry-run-only verification tool
                    # and is NOT repurposed into this role (§8.1's reasoning).
strategy/
  ladder.py         # UNCHANGED by this document -- called twice per session (§3), never
                    # modified. Its interface is treated as fixed.
  state.py          # UNCHANGED -- load()/save() called exactly once each per session (§7.1),
                    # around BOTH decide() calls, not once per call.
market/
  window.py         # UNCHANGED -- assemble_spy_signal() called once per session (step [5]);
                    # its output (cone_z, SpyContext) is reused for BOTH phase A and phase B
                    # decide() calls (prices do not change mid-session, only cash/lots do,
                    # §3.1 step 8).
  calendar.py        # UNCHANGED except for the new warm() entry point (§9), called from
                    # Kotlin at wake, not from within session.py's own sequence.
```

---

## 13. Phased build order and dependencies

| Phase | Builds | Depends on | Notes |
|---|---|---|---|
| 1 | `engine/reconcile.py` (gate 11, including the partial-fill invariant, §6) | `db/repo.py` (built), `etoro/client.py` (built) | Needed before gate 11 can run at all; also needed before the bootstrap (§8) can be verified. |
| 2 | Remaining preflight gates (1-9, 12) + `run_preflight()` | phase 1 (gate 11 delegates to it) | Gate 10 is already built; this phase wires the whole gate table together, including running gate 10 the second time (§5 step [4]). |
| 3 | `engine/bootstrap.py` (§8) | phase 2 (needs preflight passing before it runs, per step ordering in §5), `market/adopt.py` (built) | Can be developed and unit-tested against the same fixtures `tools/adopt_backfill_runner.py` already uses, before wiring into the real session. |
| 4 | `engine/lot_selection.py` (or equivalent, §4/§12) — HCBF + $10-remainder rule + sell-to-target loop (§4.3/§4.6.1/§4.8) | `db/repo.py`'s `list_lot_annotations` (built); `etoro/client.py::close_position` widened to accept a non-null `UnitsToDeduct` (§4.6.1, not yet implemented — Python file, out of this document's scope to edit) | Small, independently testable against a synthetic `lot_annotations` fixture. **No longer blocked on Friday's eligibility check** (§4.5, superseded 2026-09-06 by direct operator decision) — the one real blocker is `close_position`'s own widening (§4.6.1) and confirming the exact broker field/units convention for a partial-close request, which needs a live payload, not a design decision. |
| 5 | `engine/executor.py` (full build: open/close, poll-to-terminal, idempotency recovery) | phases 1, 4 | Needs the lot-selection policy (phase 4) to build the SCHG-close path; UPRO's path needs nothing new (position_ref already supplied by the ladder). |
| 6 | `engine/session.py` (the two-phase orchestrator, §3/§5/§7) | phases 2, 3, 5, plus `strategy/state.py`, `market/window.py` (both built) | The integration point; cannot start meaningfully before phases 2/3/5 exist, since it calls all of them. |
| 7 | End-to-end test against the sandbox's fake broker (`DESIGN_sandbox.md`'s `broker_sim.py`, itself a separate, largely-parallel build) OR a hand-built two-phase-aware fake broker if the sandbox is not ready first | phase 6, and whichever fake-broker path is chosen | This is the first place the two-phase design's actual behavior (not just its individual pieces) can be exercised without touching real money — see §15 for why this is necessary, not optional. |
| 8 | Live demo-account dry run (`mode=dry_run`, arming withheld) | phase 6 | Exercises the full sequence including gate failures and the bootstrap, without financial risk — the natural first on-device proof. |
| 9 | Live demo-account armed run | phase 8 | First real order placement, still demo. |
| 10 | Live real-account armed run | phase 9, operator sign-off | Out of this document's scope to gate further — DESIGN.md §11's own go-live checklist governs this step. |

**What can start now, in parallel:** phases 1-4 have no dependency on each other and can be
built concurrently by different sessions/agents; phase 5 needs 1 and 4; phase 6 is the
integration point and should be built last among the engine work.

---

## 14. Operator decisions required before implementation starts

Updated 2026-09-06. Three items are now fully decided (struck, not deleted, per this
project's audit-trail practice); one is decided differently than it was struck on 2026-09-05
(item 1, below); the rest remain open, plus one new item this update adds (item 7).

1. ~~**SCHG lot-selection policy (§4).**~~ ~~DECIDED, 2026-09-05: closest-to-target~~
   **SUPERSEDED, DECIDED AGAIN 2026-09-06: highest cost basis first (HCBF)**, with partial
   close live as the primary close mechanism and the target always met (never merely stayed
   under). Reasoning recorded at §4.3/§0. **What was open underneath the 2026-09-05 decision
   — which of two structurally different partial-close implementations was live, pending
   Friday's eligibility check — is now closed by the same 2026-09-06 decision: partial close
   is live by direct operator statement, not by a broker-payload read (§4.5). Friday's
   `allowPartialClosePosition` check is removed from the runbook as a result (RUNBOOK §8).**
   **Further resolved, same day, in a later pass: the ordering field itself.** "Highest cost
   first" means highest cost PER SHARE (`entry_price`/`openRate`), not highest total dollar cost
   (`entry_amount`) — the latter was checked and rejected as degenerate (§4.3/§4.4). Sizing (the
   partial-close amount, the shortfall arithmetic, the $10 check) uses current MARKET VALUE,
   never cost basis — a distinct question, raised by this document at old §16 item 8 and now
   resolved by the operator; see §4.3/§4.6.1/§16 item 8.
2. ~~**Non-terminal-order escalation threshold (§3.4).** How many consecutive trading
   sessions may an order sit non-terminal before auto-halt fires?~~ **REFRAMED, operator
   decision 2026-09-06: `N` no longer escalates to a halt (§3.4) — halts are reserved for
   money-losing ambiguity, never bookkeeping ambiguity, and there is no cancel path to make a
   stuck market order actionable by a human anyway.** `N` is now "how many consecutive
   sessions the design keeps polling an order before switching from order-based to
   state-based resolution (mark it abandoned; resolve against observed position/cash; auto-
   adopt a later fill that matches one of our own recorded abandoned intents, §3.4)." The
   numeric value of `N` **itself remains open** — this document still does not propose one; it
   depends on how quickly the operator wants the design to stop polling a rare non-terminal
   market order and fall back to state-based resolution, not on how much manual intervention
   they are willing to tolerate (there is none, by the decision above). ~~PROPOSED, PENDING
   EXPLICIT CONFIRMATION: `N = 3`~~ **OPERATOR-DECIDED, 2026-09-06: `N = 3`** (not derived from
   any measurement — an explicit operator choice, not a calibration).
3. **Whether re-entry bucket `tranche_amount` should ever be corrected against real
   settled proceeds (§3.3).** This document accepts the estimate as a bounded, self-
   correcting approximation for a first version; confirm this is acceptable, or require the
   correction be built now rather than deferred. Still open.
4. **The `migrations_run` marker mechanism (§8.2).** Whether this reuses existing schema-
   migration bookkeeping or is a new dedicated table is left as an implementation detail;
   if the operator has a preference (e.g. "never add a new table without asking"), state it
   before phase 3 (§13) is built. Still open.
5. **Session-level risk caps' actual numeric values** (~~`max_orders_per_session`~~,
   `max_notional_per_session_usd`, `max_open_lots`, the daily loss guard threshold) —
   DESIGN.md §6.C lists these as config keys with no operator-set defaults recorded anywhere
   this document found. Gate 12 (§6) cannot be built to a real threshold without them. Still
   open for the three remaining keys (`max_notional_per_session_usd`, `max_open_lots`, the daily
   loss guard threshold — **untouched** by the item below).
   **`max_orders_per_session` itself is RESOLVED, operator decision 2026-09-06 — no longer an
   open numeric value to set.** An arbitrary count cap was the wrong mechanism (DESIGN.md §6.C
   has the full reasoning: buying is already dollar-capped via the sleeve-cash re-check,
   selling is already capped by the core-floor check, so count is implied by
   `sleeve_cash / min_order_size` and added no safety); it is replaced by a latency-derived time
   gate (`now + measured_p95_latency < hard_deadline`, else stop and accept the shortfall),
   self-adjusting as §4.9's order-count-per-sell scaling risk plays out rather than needing a
   human to periodically re-tune a constant. **DESIGN.md §6.C is explicit that this time gate is
   deliberately simple scaffolding, expected to be redesigned at any time without ceremony — not
   a settled architecture this document is closing the door on.** A separate small integer
   circuit-breaker against a bug-driven absurd intent count may still exist, sized off
   `MAX_LOTS`, explicitly not a tuning knob.
6. ~~**Whether the two-phase session's added wall-clock cost (§3.2) is acceptable inside the
   T-30 margin**~~ **Still open, but no longer "unmeasured" after Friday 2026-09-11:** this
   document's earlier text noted the wait depends on how long phase-A closes take to reach
   terminal in practice, and that no live order had ever been placed (DESIGN.md §1.1's
   "highest-risk unknown"). Friday's session now includes a DEMO-account order-latency
   measurement built specifically to answer this, **now extended (2026-09-06) to also time a
   partial close, not just a full close and a full open**, since the design now depends on
   partial closes working and on their latency specifically (RUNBOOK_LIVE_WINDOW_2026-09-11.md
   §8(b)). The operator decision itself — whether the measured latency is acceptable inside
   the T-30 margin, and whether §3.4's fallback (skip phase B, recycle next session) is an
   acceptable outcome for the "sometimes it doesn't fit" case — remains theirs to make once
   real numbers exist; this document does not presume the answer.
7. ~~**NEW, 2026-09-06: the sell-to-target loop's first-pass buffer percentage (§4.8).** This
   document proposes 1.0% (matching `provisional_close_deviation_pct`'s calibrated tolerance,
   the closest existing measured number, itself derived for a different purpose) but states
   plainly that it cannot derive the correct number defensibly — no measurement of
   "sell now for $X, does cash actually increase by $X" exists yet. The operator should either
   confirm 1.0% or state a different number before phase 4 (§13) is built.~~
   **OPERATOR-DECIDED, 2026-09-06: 1.0%.** See §16 item 9 for why the asymmetry argument for
   staying conservative strengthened once that item's ratchet claim was retracted.

---

## 15. Correctness beyond the fixtures

This project's signature failure mode (HANDOFF §1): "code and tests encoding the same wrong
assumption, agreeing with each other, and the suite staying green" — five parser bugs of this
shape in three days, plus two more the first hour `window.py` met live data. A hand-written
unit test for `engine/session.py`'s two-phase logic, written by the same author who designed
the two-phase logic, is exactly the shape of artifact that failure mode predicts will pass
for the wrong reason.

**What this document specifies to establish correctness against something other than its
own fixtures:**

1. **The sandbox's `broker_sim.py` (`DESIGN_sandbox.md` §4.3), extended (not rebuilt) to
   simulate async fills with a configurable settlement delay**, is the first line of
   defense — it is built by a different design document, against different, independently-
   stated frictions (measured T-30 provisional-close divergence, measured fee model), so a
   two-phase `session.py` tested against it is checked against an independently-authored
   model of broker behavior, not a model this same document invented to make its own logic
   look correct. This requires the sandbox's fake broker to grow a "not yet terminal" state
   it does not appear to model explicitly today (per the excerpt of `DESIGN_sandbox.md`'s
   own §4.3, which only mentions the T-30 provisional-vs-settled split, not order latency
   itself) — flagged as a needed, small extension to that design, not something this
   document builds.
2. **A dedicated known-answer fixture for the `bars`-double-increment trap (§7.1):** a test
   that runs a full two-phase session against a fixture with a KNOWN correct `bars` value
   the day before, asserts the persisted value is `+1`, not `+2` or `+0` — written by hand
   against a value computed independently of the session code (e.g. by tracing `decide()`
   calls manually), not derived by running the code and recording its output as the answer.
3. **`FINDINGS_fees_2026-09-05.md`'s own 120-trade, 227-position live dataset** is the
   independent, non-self-referential evidence this document's phase-B cash re-derivation
   rests on — the design does not invent a fee model to validate itself against; it defers
   entirely to the measurement already on record and never computes an estimated proceeds
   figure as anything more than a bucket-sizing input (§3.3), exactly bounded to not affect
   the number that measurement showed must be read, not predicted.
4. **A crash-injection test**, specifically: kill the process (or simulate the equivalent)
   between every one of the seven persistence points listed in §7.2's table, in turn, and
   assert that (a) no order is ever submitted twice, (b) `bars` never double-increments, and
   (c) the very next session's gate 11 either passes cleanly or fails for a reason traceable
   to the exact injected crash point — not a vague "something is wrong" but a specific,
   predictable discrepancy matching §7.3's analysis for that crash point. This is the test
   this document considers most load-bearing, because §7.3's reasoning is exactly the kind
   of "I traced through it carefully and it should be fine" claim this project's own history
   shows is not enough on its own.
5. **A zero-trade-in-both-phases run is a hard failure to validate as "passed," not a quiet
   pass** — mirroring `DESIGN_sandbox.md` §6 rule 4 and `HANDOFF §7`'s "a check that passes
   because nothing happened" rule: an end-to-end test suite for this design must include at
   least one scenario where both phases genuinely produce zero intents (e.g. cone_z stays
   flat, no sell/buy conditions met) and assert this is reported distinctly from "phase A
   sold, phase B's buys got skipped" — the two "nothing visibly happened" outcomes must be
   distinguishable in the persisted record (§11), not silently identical.

---

## 16. Disagreements, ambiguities, and things guessed at

Stated plainly, per this task's own instruction not to smooth these over.

1. **I disagree with treating gate 8's failure as whole-session** rather than
   per-symbol-degrade (§6). An argument could be made that a UPRO-only eligibility failure
   should still allow an SCHG-only session to proceed (buy on new ATH, sell core to fund...
   nothing, since there'd be no UPRO buy to fund). I chose whole-session-halt because
   `decide()` itself has no notion of "SPY/UPRO/SCHG partially available" — it is handed one
   `LadderInputs` with all three prices — and inventing a partial-input mode for `decide()`
   would be exactly the kind of unrequested change to the ladder's interface this document
   elsewhere refuses to make (§3.2's rejected two-phase-decide alternative, same reasoning).
   Flagged as a judgment call, not a certainty.
2. **Ambiguous: what "the session must end" means operationally in §3.4.** DESIGN.md
   describes a ~15-minute total awake time with ~60s of actual API traffic (§7.6) but this
   document could not find an explicit "hard stop, must release wake lock by X" deadline
   anywhere in the read documents. I assumed a configurable session-level timeout (distinct
   from `poll_order_to_terminal`'s own per-order timeout) governs when phase B is abandoned
   in favor of the next-session fallback (§3.4) — this value is not specified anywhere and is
   added to the operator-decision list (§14) implicitly via item 6, though it deserves its
   own explicit number, not just "whatever fits."
3. **Guessed at: whether `engine/lot_selection.py` should be its own module or a function
   inside `executor.py` (§12).** No document expresses a preference either way; I left it as
   an implementation-detail choice for whoever builds phase 5 (§13) rather than deciding it
   here, since it has no behavioral consequence, only an organizational one.
4. **I disagree with re-litigating nothing about the `max_sleeve_cash_usd` ceiling** — not a
   new position, just confirming I did not, anywhere in this document, propose one, since
   DESIGN.md §4.4 is explicit that this was declined twice and should not be re-proposed;
   stated here only so a reader auditing this document for compliance with that closed
   decision can see it was checked, not merely absent by accident.
5. **Ambiguous: whether phase B's second `decide()` call, if it proposes a sell (§3.1 step
   11), should count as a second "sale batch" for `sale_batch_sequence` purposes** the same
   way two sells on two different calendar days would. `_sell_lots` increments
   `sale_batch_sequence` when `sale_batch_date != inputs.trading_date` — since phase A and
   phase B share the same `trading_date`, a phase-B sell would NOT get a new batch sequence
   number, it would (if it happened) need to merge into phase A's batch conceptually, but
   phase A's batch was already computed and persisted by the time phase B runs. This is a
   real, unresolved edge case this document surfaces rather than papers over: if phase B ever
   does propose a sell (stated in §3.1 as rare but not forbidden), the sale-batch bookkeeping
   needs a specific rule this document does not fully work out — flagged as a genuine gap,
   not a confident answer.
6. **I did not find, and could not derive from the read documents, a stated wall-clock
   budget split between the two phases** — i.e., how much of the T-30-to-close window should
   be reserved for phase A's poll-to-terminal wait vs. left for phase B's own execution and
   the T-30 sleep math. This document treats the whole two-phase sequence as fitting inside
   the existing margin "usually," per §3.2/§14 item 6, but does not propose a specific
   internal budget split, because no measured order-latency data exists yet to size one
   against (DESIGN.md §1.1's own "highest-risk unknown," still unresolved as of this
   writing — Friday 2026-09-11's DEMO-account latency measurement, §4.7/RUNBOOK_LIVE_WINDOW_
   2026-09-11.md §8, is the first attempt to close this).
7. **HISTORICAL, 2026-09-05 — superseded by the operator's own 2026-09-06 decision, kept
   verbatim below because it is arguably part of why that decision changed.** A genuine
   problem with the operator's closest-to-target decision (§4.3 as it stood 2026-09-05),
   stated plainly rather than ratified. The floor-protection argument for closest-to-target over
   FIFO (§4.3) is real, but it is weaker than the reasoning as written makes it sound, in two
   ways this document should not paper over:
   - **The improvement is average-case, not worst-case.** §4.6.2's overshoot bound is "at
     most one lot's `amount`" — the exact same *order of magnitude* bound §4.4 already
     accepted for FIFO. In the worst case (the only lots available near `sell_amount` all
     happen to be large), closest-to-target's overshoot is not meaningfully smaller than
     FIFO's could be. The real gain is in the *typical* case (2-3 candidate lots checked
     instead of whichever oldest lot happens to be next), which is worth having, but the
     safety framing in §4.3 ("protects the floor in a way oldest-first does not") reads as a
     structural guarantee when it is actually a distributional improvement. §4.6.2 step 4's
     pre-submission floor check is what actually makes the floor un-breachable — not the
     choice of closest-to-target over FIFO by itself. Closest-to-target without that
     pre-check would be no safer than FIFO without one; the floor check, not the lot-
     selection policy, is doing the load-bearing work.
   - **Closest-to-target inherits HCBF's rejected P&L objection, partially.** §4.4 rejects
     HCBF specifically because "concentrating which specific lots get closed... could,
     without operator input, systematically liquidate the account's largest cost-basis lots
     first — a decision with real tax/P&L shape this document has no authority to make
     unilaterally." Closest-to-target's own selection is *also* a function of dollar amounts
     rather than age or cost basis, and §4.6.2's candidate search will, in practice, often
     prefer larger lots when they land closer to `sell_amount` (a single ~$500 lot is a
     tighter match to a ~$500 `sell_amount` than three ~$165 lots would be) — the same
     amount-driven selection shape HCBF was rejected for, just pursuing a different (tighter-
     fit, not largest-first) objective. This document does not think this rises to HCBF's
     level of concern (closest-to-target's objective is overshoot, not size, so it does not
     *systematically* prefer the largest lots the way HCBF explicitly does), but the operator
     should know the P&L/tax randomness §4.4 flagged as a real cost for HCBF is not fully
     absent from closest-to-target either — it is reduced, not eliminated. No operator P&L/
     tax preference has been stated for either policy, and this document still has no basis
     to assume one.
   Neither point argues for reverting to FIFO — the pre-submission floor check (§4.6.2 step
   4) is required regardless of which lot-selection policy sits above it, and closest-to-
   target's typical-case overshoot reduction is real and worth having. The point is narrower:
   the reasoning offered for the decision oversells what the policy choice itself guarantees,
   and undersells how much of the actual protection comes from a check this document is
   adding anyway. Recorded as a genuine reservation, not a re-litigation of the decision.

8. **RESOLVED — raised by this document (2026-09-06), accepted and resolved by the operator,
   later the same day.** This document's own §4.6.1/§4.8 conflated "cost basis" and "current
   value" where they should not have been conflated: the operator's HCBF ordering decision (D)
   said sort by cost; the $10-remainder rule and the sell-to-target loop's arithmetic (§4.8)
   then operated on `lot.amount` for both the sort key and the "how much cash does closing this
   lot raise" question. `db/repo.py`'s `LotAnnotation` schema distinguishes `entry_amount`
   (total cost basis, fixed at entry, never updated) from the position's *current* market value
   (not itself a stored field anywhere — not on `LotAnnotation`, and, checked directly against
   `etoro/client.py`/`models.py`, not on the broker's own `Position.amount` either, which is
   itself cost basis: `amount == units * openRate`, verified 227/227 — current market value
   must be computed fresh each session as `units × price` via `mark_position`). Even this
   document's own worked-trace pseudocode had mis-labeled market-value figures (`[$60, $50,
   $50]`) as "cost-basis descending," which cannot be correct for a `$60` lot the operator
   himself describes as "a $50 buy that has appreciated" (its cost basis is $50, tied with the
   other two lots, not $60) — exactly the kind of silent mis-sizing this reservation warned
   about. **The operator has now resolved it directly:**
   - **Ordering is by cost PER SHARE** — the broker's `openRate` (`Position.entry_price` in
     `etoro/client.py`), descending, tie-broken by `position_id` ascending. Rejected: ordering
     by total cost basis (`lot_annotations.entry_amount`), because it degenerates once the
     $50-min-order-size app-created book dominates (§4.3/§4.4).
   - **Sizing is by current MARKET VALUE** (units × today's fetched price —
     `PositionMark.current_value`) for the partial-close amount, the shortfall arithmetic, the
     overshoot/undershoot bounds, and the $10 `minPositionExposure`/`minPositionAmount`
     remainder check. Never cost basis for any of these.
   - Cost basis is used for ORDERING ONLY; market value is used for SIZING ONLY. Neither field
     does both jobs — see the named rule at the end of §4.6.1 and the corrected worked trace at
     §4.8.
   This is now a **closed** question, not an outstanding reservation — recorded here, per this
   project's audit-trail practice, so a future reader can see it was raised, not merely assumed
   away.
9. **NEW, 2026-09-06 — genuinely risky: the buffer percentage is unvalidated, and a wrong
   value in either direction has a real cost.** Too small, and the loop fires its shortfall
   pass often, spending an extra unmeasured round-trip inside the T-30 window — a cost that
   is itself trending upward as the book fills with $50-origin lots and orders-per-session
   grows (§4.9), so a buffer that looks fine today could look undersized later without any
   change to the buffer itself. **This half stands, unchanged, and — per the retraction
   immediately below — is now the dominant asymmetry.**

   ~~Too large, and every session sells more SCHG than the target strictly needs, quietly
   eroding the core floor's headroom session after session in a way the pre-submission check
   (§4.6.1) does not catch, because that check only refuses a *single* close that would breach
   the floor outright — it has no memory of "this session's buffer sold slightly more than
   necessary, again."~~ **RETRACTED, 2026-09-06 — checked directly against the ladder's own
   arithmetic and found factually wrong; there is no cumulative ratchet.**
   `strategy/ladder.py:926-929` already nets the sell-to-target amount against whatever cash is
   already on hand, and skips the sell outright once cash covers `cash_target`:

   ```python
   if normal_ladder_buy and inputs.cash < cash_target:
       need = cash_target - inputs.cash
       available = max(0.0, inputs.schg_position_value - config.core_floor_pct * inputs.equity)
       sell_amount = _floor_cents(min(need, available))
   ```

   A session that oversold by, say, $0.51 leaves the next session's `inputs.cash` exactly $0.51
   above `cash_target`; `need` (and therefore `sell_amount`) is smaller by exactly that $0.51 the
   next time this branch fires — total SCHG sold across sessions is unchanged, not eroded further
   session after session. Once accumulated overshoot reaches `order_size` (≥ `min_order_size`,
   $50), it instead **funds a buy outright** the next time a new-ATH bar occurs, via the
   "cash to core" buy-back path: the `new_ath_bar` branch (`strategy/ladder.py:909-917`) computes
   `available = inputs.cash - cash_target - tier1_reserve` and, when positive, spends it through
   `_schg_buy_intents` (`strategy/ladder.py:769`, `reason="cash to core"`). **Overshoot is a
   float, not a ratchet.** Verified by tracing both branches directly against the module, not
   assumed.

   The actual residual costs of a too-large buffer are real but different, and much smaller, than
   what this item originally claimed: (a) SCHG is sold marginally earlier than strictly
   necessary, so marginally out of the market for longer; and (b) **on a new-ATH bar**, excess
   cash above `cash_target` (net of the tier-1 reserve) round-trips SCHG → cash → SCHG through the
   same cash-to-core buy-back path (`strategy/ladder.py:769`, reached from `:909-917`), at the
   cost of the bid/ask spread on each leg — not a growing erosion of the core floor's headroom.
   This document proposes 1.0% (§4.8, §14 item 7) as a starting point, not a validated number,
   precisely because this trade-off has not been measured either way — and the asymmetry argument
   for keeping the buffer on the conservative side is, if anything, **stronger** now that the
   ratchet is retracted: overshoot is nearly free, while undershoot (the surviving half of this
   item, above) still costs a round-trip inside the T-30 window. Per-session telemetry logging
   `(actual_cash_delta - target) / target` (§11) is accordingly a nice-to-have for eventual
   calibration, not a safeguard against an ongoing erosion this document no longer believes
   exists.

   **OPERATOR-ENDORSED, 2026-09-06: this retraction.** The buffer value itself (1.0%, §4.8/§14
   item 7) is now **OPERATOR-DECIDED, 2026-09-06** rather than merely proposed — the asymmetry
   argument for staying conservative strengthened once this retraction landed, since overshoot is
   now nearly free while undershoot still costs a round-trip inside the T-30 window.
10. **NEW, 2026-09-06 — genuinely risky, stated plainly: this design's viability now rests on
    two numbers nobody has measured yet, at the same time.** Partial close's actual latency
    and the number of orders a growing book will require per session (§4.9) both feed directly
    into whether the sell-to-target loop (§4.8) fits inside the T-30 window at all, and Friday
    2026-09-11 is this project's only near-term chance to measure the first of the two (order
    latency, including a partial close specifically per this update — RUNBOOK §8(b)) before
    going live. The second (orders-per-session growth as the book matures) cannot be measured
    at all until the book itself has grown. **UPDATED, operator decision 2026-09-06: this no
    longer means a guessed integer needs setting and revisiting** — `max_orders_per_session` is
    retired (§14 item 5, DESIGN.md §6.C) in favor of a latency-derived time gate
    (`now + measured_p95_latency < hard_deadline`) that self-adjusts as orders-per-session grows,
    rather than a fixed count that would need re-tuning by hand as this section's own growth
    curve plays out. That gate still depends on the first of the two unmeasured numbers (Friday's
    order-latency measurement) to have a `p95_latency` to project with; it does not need the
    second (today's actual orders-per-session count) to be *set* correctly in advance, because it
    reasons about time directly rather than inferring time from a count. **DESIGN.md §6.C is
    explicit that this replacement is deliberately simple scaffolding, not a settled design** —
    stated here so this item does not read as fully resolved by the replacement; it narrows the
    risk from "a guessed constant may be wrong" to "the scaffolding's own shape may need to
    change," which this document does not consider the same risk, but does not consider zero
    either. This document does not consider either version disqualifying, but does consider it
    worth stating without softening: the design is sound
    on paper and degrades gracefully on paper (§3.2, §4.8 point 2/3), but "degrades gracefully"
    has itself never been exercised against a real broker, and Friday is the first, not the
    last, opportunity to find out whether the paper reasoning survives contact with actual
    round-trip times.
