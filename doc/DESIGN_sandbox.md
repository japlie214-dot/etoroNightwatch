# etoroNightwatch — Strategy Sandbox (design)

**Status:** design only. Nothing in this document is built. Supersedes the `forward_test/`
sketch in `DESIGN.md` §2 and §10 (`replay.py`, `fake_broker.py`) — that directory does not
exist on disk yet (checked 2026-09-05: `forward_test/` has no files), so nothing is being
deleted, only redirected before it is ever written.
**Written:** 2026-09-05, per the operator's verbatim request (quoted below) superseding
`board nightwatch/handoff-2026-09-05-evening.md` §6 item 7's narrower "forward-test harness and fake broker."
**Concurrent work, not touched by this doc:** an agent is editing `market/window.py`,
`market/adopt.py`, `config.py`, `db/repo.py` at the time of writing. Everything below cites
those modules' *documented contracts* (function names, docstrings, DESIGN_market_data.md),
never line numbers or current implementation details, because those will move under this
design. Re-verify the cited signatures once that work lands.

> "regarding the forward-test harness, redesign it as a sandbox. It's plug and play. We should
> be capable of plugging any new strategy to the sandbox for realistic simulation with all of
> the upside/downside metrics calculated cleanly and consistently."

---

## 1. What this sandbox is, and what it deliberately is not

**Is:** a strategy-agnostic simulator. Given (a) a historical price series and (b) an object
implementing the `Strategy` protocol (§2), it runs a day-by-day simulation, applies the
project's *measured* broker frictions (§4), and emits one identical metric report (§5) for
any strategy that plugs in.

**Is not:** a driver of `engine/session.py`. The original `forward_test/` idea (`DESIGN.md`
§10) was to run "the real live engine — the same session state machine, preflight, executor."
That framing is now actively wrong for a plug-and-play sandbox, and I am flagging the
disagreement rather than quietly resolving it (see §10.1): most of preflight (§5 of
`DESIGN.md`) is phone/Android/network-specific — battery, `SCHEDULE_EXACT_ALARM`, Wi-Fi
DNS, `x-request-id` idempotency against a real HTTP layer, environment-match probing. None of
that is meaningful in a simulation, and hardwiring the sandbox to `engine/preflight.py` /
`engine/session.py` would drag Android concerns into a desktop simulator and would also
couple "plug in a new strategy" to "also reimplement twelve Android preflight gates for it."
The sandbox instead depends only on:

- the strategy plug-in protocol (§2, new, sandbox-owned)
- the market-data contracts already documented in `DESIGN_market_data.md` (`fetch_symbol_window`,
  `validate_window_completeness`, `resolve_provisional_close`, `compute_fingerprint`,
  `assemble_spy_signal` — cited by name, per the note above)
- for the ladder specifically: `market/window.py`'s pure ATH/drawdown/tier derivation, reused
  by `ladder_adapter.py` rather than recomputed — see §8.1, added after the original draft of
  this document specified the adapter without it
- its own broker/fee/ledger/metrics code (§4, §5, all new)

`engine/session.py`'s orchestration (arm gate, Telegram, wake lock, reschedule) is simply not
sandbox-relevant; §10.1 records this as a considered, deliberate departure from the original
`DESIGN.md` §10 conception, not an oversight.

---

## 2. The strategy plug-in protocol

This is the concrete interface. Nothing about the ladder appears in it — the ladder is
strategy 1 of N, implemented as an *adapter* (§8) that sits behind this protocol like any
other future strategy would.

```python
# sandbox/protocol.py

from typing import Protocol, Literal, Sequence
from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class Bar:
    date: str        # YYYY-MM-DD
    close: float      # raw close  (HANDOFF §4: levels use raw close)
    adj_close: float  # dividend/split-adjusted close (returns use adj_close)

@dataclass(frozen=True, slots=True)
class OpenLot:
    """What the strategy is told about a position IT previously opened.
    Sandbox-internal identity only — never an eToro positionId, because the
    sandbox never talks to eToro. `book`/`tag` are strategy-owned free text,
    opaque to the sandbox (a place for "catapult"/"reentry"/whatever the
    ladder or any other strategy wants, without the protocol knowing what a
    "book" is)."""
    lot_ref: str
    symbol: str
    units: float
    entry_price: float
    cost_basis: float
    entry_date: str
    book: str | None
    tag: dict          # opaque strategy-defined metadata, round-tripped verbatim

@dataclass(frozen=True, slots=True)
class PortfolioView:
    """Sleeve-scoped, exactly like DESIGN.md §4.5 — never whole-account."""
    sleeve_cash: float
    sleeve_equity: float
    open_lots: Sequence[OpenLot]          # only lots in the strategy's own tradable set
    broker_min_exposure: dict[str, float] # symbol -> minPositionExposure, from §4 friction data
    min_order_size: float                 # the operator's config floor (DESIGN §4.3), separate
                                           # from broker_min_exposure — see §4.2 below

@dataclass(frozen=True, slots=True)
class SessionContext:
    """Everything a strategy receives once per simulated trading day. No
    field here is ladder-specific: no `tier`, no `cone_5_z`, no `ATH` --
    those are the ladder's OWN derived signals, computed inside the
    ladder adapter from `price_history`, never handed down by the sandbox.
    A future momentum, mean-reversion, or buy-and-hold strategy gets
    exactly the same four fields."""
    trading_date: str
    session_index: int                              # 0-based count of simulated trading days
                                                      # so far *including* this one -- lets a
                                                      # strategy replicate its own warmup counter
                                                      # without the sandbox knowing what "warmup"
                                                      # means to it
    price_history: dict[str, Sequence[Bar]]          # symbol -> full warm-up-converged history
                                                      # up to and including today, for exactly the
                                                      # symbols the strategy declared it needs (see
                                                      # Strategy.universe below) -- raw AND adjusted
                                                      # close both given; the strategy picks its own
                                                      # basis per HANDOFF §4, the sandbox does not
                                                      # pick for it
    portfolio: PortfolioView

@dataclass(frozen=True, slots=True)
class OrderIntent:
    """Symmetric with db.repo.Intent's spirit, deliberately smaller: no
    instrument_id, no position_id, no eToro-shaped fields at all -- those
    belong to the execution layer, which the sandbox's broker_sim (§4)
    supplies, not the strategy."""
    symbol: str
    side: Literal["open", "close"]
    amount_usd: float | None = None    # required for side="open"
    lot_ref: str | None = None         # required for side="close" -- must name a lot_ref the
                                       # strategy itself was previously given back in an OpenLot
    reason: str = ""
    book: str | None = None            # opaque, round-tripped into the trade log only

@dataclass(frozen=True, slots=True)
class StrategyUniverse:
    signal_symbols: frozenset[str]     # e.g. {"SPY"} -- fetched, never traded (mirrors DESIGN §1.4)
    tradable_symbols: frozenset[str]   # e.g. {"UPRO", "SCHG"} -- the only symbols OrderIntents
                                       # may reference; the sandbox rejects any intent outside it,
                                       # the same discipline as DESIGN.md §1.4's "managed set"
    required_history_bars: int         # the strategy's OWN warm-up requirement (§3) for its
                                       # BOUNDED, fixed-width rolling signals -- e.g. the ladder
                                       # declares 265 (cone.REQUIRED_HISTORY_BARS), its only
                                       # fixed-width requirement now that the ATH is unbounded
                                       # (see required_data_start_date below); a different
                                       # strategy states its own.
    required_data_start_date: str | None  # for a strategy with an UNBOUNDED running quantity
                                       # (the ladder's ATH: "max raw close since the data
                                       # started", mirroring market/window.py's full-history
                                       # design) -- the earliest calendar date ("YYYY-MM-DD")
                                       # the loaded feed must reach back to for that quantity to
                                       # be meaningfully converged before the first simulated
                                       # trade. The ladder declares "2006-01-01" (§3). `None` for
                                       # a strategy with no unbounded quantity -- for such a
                                       # strategy required_history_bars alone governs warm-up.

class Strategy(Protocol):
    name: str

    def universe(self) -> StrategyUniverse: ...

    def on_session(self, ctx: SessionContext) -> Sequence[OrderIntent]: ...

    def get_state(self) -> dict: ...       # JSON-serialisable; sandbox persists it verbatim
    def load_state(self, state: dict) -> None: ...  # restores from a previously returned dict,
                                                      # or is called with {} on a cold start
```

**Why state is carried this way.** `board nightwatch/handoff-2026-09-05-evening.md` §1.4 documents a real bug
class: a strategy instantiated fresh each day whose internal counter (`self._bars`) resets to
1 and never recovers — the "warmup trap." The sandbox's runner (§8) deliberately **never
holds a live strategy object across simulated days.** Each simulated session constructs a new
`Strategy` instance, calls `load_state()` with whatever `get_state()` returned the previous
day (or `{}` on day one), calls `on_session()` once, then discards the instance and persists
whatever `get_state()` now returns. This is slower than keeping one long-lived object, and
that cost is deliberate: it is the same discipline production runs under (`engine/session.py`
constructs one session's worth of everything, once, and exits), and it means the sandbox
*exercises* the exact bug class §1 of the handoff is written about, on every single simulated
day, rather than being unable to reproduce it because the harness itself is more forgiving
than production ever is.

**What is deliberately excluded from the protocol, and why:**

- **No `tier`, no `cone_5_z`, no `ATH` field.** These are the ladder's own derived quantities
  from SPY history (`DESIGN.md` §1.4, `strategy/cone.py`). Computing them in `SessionContext`
  would mean the sandbox has opinions about what a signal *is*, and the next strategy plugged
  in inherits fields it doesn't want and can't avoid depending on. The strategy receives raw
  `price_history` (both bases, per HANDOFF §4) and computes whatever it needs itself.
- **No broker order lifecycle** (`orderId`/`referenceId`/status polling). That is `broker_sim`'s
  job (§4), invisible to the strategy — a `Strategy` never sees an intent fail to fill; the
  sandbox's ledger (§8) records the realized fill and the *next* day's `PortfolioView` simply
  reflects reality, exactly as `engine/session.py`'s eventual step [9] persists fills before
  the strategy is asked anything again.
- **No `instrument_id`.** eToro instrument ids (SPY=3000, UPRO=3244, SCHG=1084,
  `DESIGN.md` §1.1) are an eToro API concept; the sandbox's fake broker maps symbol strings to
  its own friction parameters (§4) and never touches this at all.

---

## 3. The warm-up rule (2006-vs-2010 data range, generalised)

**Operator decision, 2026-09-05, deliberate:** the live app fetches SPY from 2010 (UPRO and
SCHG both begin ~2009-2010, so 2010 is the uniform live floor). **The sandbox fetches SPY
from 2006, but only starts trading in 2010.**

**Why, precisely.** A backtest that both starts its *data* and starts *trading* in 2010 would
see SPY's first bar at ≈$110 and treat that as the all-time high. The true 2007 peak was
≈$157 and was not regained until roughly March 2013 (`board nightwatch/handoff-2026-09-05-evening.md` §3's
"Known limit of the measurement"). Such a run reports ≈0% drawdown through the entire
2010–2013 recovery and never exercises tiers 1–5 during exactly the period the deep tiers
exist for — a correctness trap indistinguishable, from the run's own output, from a
genuinely calm market. Loading SPY from 2006 gives four years of warm-up before the first
simulated trade (2010), so the running ATH is already correct — the 2007 peak is already in
the window — when trading starts.

**Generalised rule, restated for full history (no fixed ATH window width).** Before the first
simulated trade of any run, the sandbox must have loaded enough prior history that **every
derived quantity any plugged-in strategy needs is fully converged** — not merely present, but as
accurate as it would be with unlimited history. As of 2026-09-05 this can no longer be stated as
a single bar count, because the live app's own ATH stopped being a fixed-width rolling window
(the 819-bar/39-month window was superseded the same day it was decided —
`board nightwatch/handoff-2026-09-05-evening.md` §3, §7) and became "the max raw close since
`market/window.py`'s `FULL_HISTORY_START_DATE`" — unbounded, not a window. `StrategyUniverse`
(§2) now states two different kinds of convergence explicitly, rather than collapsing them into
one integer:

- **Bounded, fixed-width quantities** (`required_history_bars`): converged the instant `N` bars
  precede the first trading day, same mechanism as before. For the ladder this is the cone
  signal's own **265**-bar requirement (`strategy/cone.py`'s `REQUIRED_HISTORY_BARS`) — its only
  remaining fixed-width requirement.
- **Unbounded, running quantities** (`required_data_start_date`): the ladder's ATH has no bar
  count after which it is "converged" in an absolute sense — a feed reaching further back could
  always in principle reveal an even earlier, higher peak. What actually matters is that the
  loaded feed reaches back at least as far as the highest pre-existing peak relevant to the
  simulated period. The ladder declares `required_data_start_date = "2006-01-01"`, matching the
  sandbox's own 2006 floor (four years ahead of the 2010 trading start) — chosen because it
  captures the true 2007 peak (≈$157, not regained until ≈March 2013,
  `board nightwatch/handoff-2026-09-05-evening.md` §3's "known limit of the measurement") that a feed starting
  only in 2010 would miss entirely. This is the *same* accepted, stated limit the live app
  itself still carries (its 2010 floor misses 2008-2009 and earlier, `HANDOFF §7`) — the
  sandbox's 2006 start is a documented *improvement* on live's fidelity for this one signal, not
  a discrepancy to paper over (see the fidelity caveat below).

**What the warm-up check actually verifies.** `data_feed.py` computes, for the configured run:

1. `bars_before_first_trade_date = count(SPY bars with date < first_trading_date)`, required to
   be `>= strategy.universe().required_history_bars` — the bounded-window check, mechanically
   unchanged from before (it now compares against 265, not 819).
2. If `strategy.universe().required_data_start_date` is not `None`: the feed's actual configured
   start date must be `<= required_data_start_date` — i.e. the feed was not narrowed past what
   the strategy declared it needs.

Either failing raises `WarmupInsufficientError` (analogous in spirit to `market/window.py`'s
`WindowAssemblyError`, per the same "no data means no trade" doctrine in
`DESIGN_market_data.md` §3.3). This is a hard stop, not a warning — a warm-up-starved run is
exactly as dangerous as the 2010-2010 trap this rule exists to prevent, and a warning that can be
ignored is no protection at all.

**What this gate is actually for (correcting an earlier, wrong justification for it).** An
earlier pass at this rationale used the example "someone runs a 2011 start and gets wrong
drawdowns" to justify this gate. **That is not a real failure**, and it should not be repeated:
the sandbox pins its data start at 2006 independently of the trading-start parameter, so any
`first_trading_date >= 2010` clears both checks above automatically and gets correct, converged
figures for free — no run starting at or after the live floor was ever at risk. The gate's real
purpose is catching:

- a `first_trading_date` set **earlier than the data can support** (e.g. 2007) — check 1 fails
  because too little history precedes it;
- **someone narrowing the 2006 data floor** (e.g. reconfiguring the sandbox's fetch start to,
  say, 2008, without updating anything else) — check 2 fails because the feed no longer reaches
  back to the ladder's declared `2006-01-01`;
- **a future strategy declaring more history than the loaded data provides** — either check can
  fail: a bounded requirement bigger than what is loaded (check 1), or an unbounded requirement
  reaching further back than the feed's configured start (check 2).

**Fidelity caveat, recorded and accepted, not hidden.** Live and sandbox use *different* SPY
start dates (2010 vs 2006) by design, so **the sandbox is not a byte-identical replica of
live.** For the ladder specifically this gap is narrow: the cone's 265-bar window is identical
in both configurations once enough history exists, and the ATH is unbounded (max-since-start) in
both — the two configurations compute the *same* running max once both are past 2010; the only
difference is that the sandbox's running max is already correct (the 2007 peak is already
folded in) from the first simulated trade in 2010 onward, while live's running max never
includes that peak at all, because live only ever fetches from 2010 — this is the exact, stated
2008-2009 blind-spot live already accepts (`HANDOFF §7`), not a new gap this design introduces.
State this caveat in every sandbox report's header (§6) so nobody mistakes a sandbox number for
a live-fidelity guarantee it does not carry.

---

## 4. Realistic simulation: the frictions

The brief asks for frictions the project has *already measured*, cited, plus explicit
flagging of anything unmeasured. These operate at different layers and the design keeps them
in different modules rather than one "frictions" blob, because conflating a signal-timing
effect (T-30) with a broker fee (carrying cost) with a bookkeeping convention (raw vs
adjusted basis) would make it unclear which module owns which number.

### 4.1 Fee model — `sandbox/fees.py`

**MEASURED** (`FINDINGS_fees_2026-09-05.md`, settled across all 120 closed trades and all 227
open positions in the live account, not a sample):

- The fee is **a carrying fee proportional to size × time**, not a commission and not a flat
  charge: `|fees|` correlates with `investment × holding_days` at Pearson r = **0.9918**,
  vs. r = 0.9157 for `investment` alone and r = -0.0611 for `holding_days` alone (§2).
- **SCHG: 0.320%/yr**, measured (`open $40,867.17`, `totalFees -$59.17`, §3). This is
  `board nightwatch/handoff-2026-09-05-evening.md` §6 item 11's "modelled nowhere" cost — the sandbox is where
  it first gets modelled.
- Not dividends, not an account-level charge, not a function of leverage (all fee-bearing
  rows are `leverage=1`; the 2 leveraged/short rows carry $0.00 fees) — §2.

**UNMEASURED, and the sandbox must say so, not assume a value:**

- **UPRO's annualized carrying rate.** The dataset's only UPRO positions were held ≤17 days —
  "too short to accrue" (§3) — so there is *no* measured UPRO rate at all. The sandbox must
  not default this to 0% (that silently understates cost on a strategy that holds UPRO longer
  than the historical sample ever did) and must not assume it equals SCHG's 0.320%/yr (a
  different instrument, no evidence they share a rate). This is a required, explicitly-labeled
  config input with no shipped default — the sandbox refuses to run without an operator-chosen
  value, and every report states which value was used.
- **The disclosed $0.15 `transactionFee`'s cash impact.** `get_costs` discloses this figure for
  SCHG; the live data shows **no corresponding reduction in position `amount`** at entry
  (`FINDINGS §4`), and whether it hits cash separately on close is unsettled — it needs a live
  order the operator has deferred to deployment (`HANDOFF §7`). The sandbox models this as an
  independent, off-by-default toggle, never silently combined with the measured carrying-fee
  model — the carrying fee is already the full measured `totalFees` figure from 120 real
  trades; adding an unverified transactionFee on top without checking for double-counting
  would fabricate a number more "precise" than any of its inputs.
- Instrument 4375's ~3.3%/yr (`FINDINGS §3`) is **excluded outright** — it is not in the
  managed set (SPY/UPRO/SCHG) and must never be borrowed as a stand-in for anything.

### 4.2 Order-size floors — two independent, stacking constraints

- **Broker floor (structural, measured):** `minPositionExposure = $10.00` for both SCHG and
  UPRO, live-verified 2026-09-05 (`DESIGN_market_data.md` §0.1, correcting an earlier wrong
  $50 reading). An order below this is rejected by the broker, full stop.
- **Operator floor (config, a choice, not a broker fact):** `min_order_size = 50`
  (`DESIGN.md` §4.3, `HANDOFF §5`). The sandbox implements both as **separate, stacking**
  checks — `effective_min = max(broker_min_exposure[symbol], min_order_size)` — because
  production stacks them the same way, and collapsing them into one number would silently
  hide the day `min_order_size` is changed and the broker floor becomes the binding
  constraint instead (or vice versa for a strategy with a different, lower `min_order_size`).

### 4.3 T-30 provisional-close timing — `sandbox/broker_sim.py`

**MEASURED**, on 4,184 SPY daily bars (`DESIGN.md` §4.2a):

| | share |
|---|---|
| closes at a new ATH | 13.24% |
| touched an ATH intraday but closed below | 8.89% |
| closes within ±0.137% of the prior ATH | 8.30% |

**40.2% of intraday ATH touches fail to close at an ATH** — the upper bound on the T-30 error
rate for the SCHG-ATH test. Tier-boundary exposure is **14x** smaller (1.27% of bars near a
tier boundary vs. 17.90% near the ATH boundary), which is *why* `DESIGN.md` §4.2a keeps the
provisional close for tiers but not for the SCHG ATH test.

The sandbox's fake broker fills orders against the **provisional close** the strategy actually
saw (mirroring `DESIGN.md` §4.2's "trade on what you saw, not what settles later" doctrine),
and separately advances a **settled** series that the *next* day's `price_history` reflects —
reproducing the exact provisional-vs-settled split production uses, so any divergence this
introduces (§4.2a's honestly-quantified signal-fidelity cost) is visible in the sandbox's own
output, not assumed away.

**UNMEASURED, explicitly:** whether eToro's execution engine treats an order placed 30 minutes
before the close any differently than any other time is **the project's own highest-risk
unverified item** (`DESIGN.md` §1.1: "no documented rule about rejecting orders near the
close... still the highest-risk unknown"). No live order has ever been placed at all
(`FINDINGS §4`; `HANDOFF §7`). **The sandbox cannot measure this — it can only assume fills
occur at the provisional close with no additional slippage or rejection risk.** Every report
must say this assumption was made; the only tool that can retire it is `DESIGN.md` §11 step
11's live demo run, not this sandbox.

### 4.4 Price basis split — raw close vs. `adjClose`

**MEASURED** (`HANDOFF §4`): the adjustment gap between Tiingo's raw `close` and `adjClose`
was 1.0932% at the oldest bar of a 276-bar window and 0.0000% at the most recent; Tiingo raw
vs. eToro's own candle across 272 common dates averaged 0.128% (max 0.640%). eToro's own
candles track Tiingo's **raw** close, not `adjClose`.

**Decision the sandbox must reproduce exactly** (this is not a friction to approximate, it is
a settled bookkeeping rule): `adj_close` for any **return** computation (the cone signal, and
any future strategy's own return-based signal), raw `close` for any **level** comparison (ATH,
drawdown, tier, and any simulated cross-check against a "what would eToro have shown"
figure). `SessionContext.price_history`'s `Bar` (§2) carries both fields so this decision
stays with the strategy, exactly as HANDOFF's own table assigns it per-use rather than as one
uniform rule — the sandbox does not pick a basis on the strategy's behalf.

### 4.5 SCHG carrying cost as a P&L line, not just a fee deduction

`HANDOFF §6` item 11 records this cost as "modelled nowhere." The sandbox's ledger (§8)
applies `fees.py`'s carrying-fee model to daily mark-to-market SCHG value, so it shows up both
in the trade-level `totalFees` a closed lot reports and, cumulatively, as a drag visible in
the cash-drag and exposure metrics (§5) — the same fee, seen from two angles, not two separate
numbers that could silently disagree.

---

## 5. The metric set

Computed **identically for every strategy**, off two ledgers `sandbox/ledger.py` maintains
during the run: (a) a daily **sleeve equity curve** — `sleeve_cash + market value of every
open lot in the strategy's tradable set`, marked to market at each simulated day's settled
close — and (b) a **trade log** — one row per **closed** lot, carrying entry/exit
date, cost basis, exit proceeds (read from realized cash, per `FINDINGS §4`'s "read the cash
balance, don't predict it" rule — carried into the sandbox exactly because it was proven
necessary on live data, not merely convenient), and `realized_pnl`.

**Annualisation convention, stated once, used everywhere:** **252 trading days per year**,
compounding via CAGR (geometric), volatility/Sharpe/Sortino via **simple daily returns**
`r[t] = equity[t]/equity[t-1] - 1` (never log returns — chosen for consistency with the
CAGR convention and because trade-level `realized_pnl` is already a simple, not log, quantity).
All metrics use the **sleeve** equity curve (`DESIGN.md` §4.5's sleeve, not whole-account),
same discipline production is built to keep.

### Returns

| Metric | Definition |
|---|---|
| Total return | `equity[-1] / equity[0] - 1` |
| CAGR | `(equity[-1] / equity[0]) ** (252 / N) - 1`, `N` = count of simulated trading days between the first and last equity observation (not calendar years — trading only happens on XNYS sessions) |

### Drawdown & time-under-water

| Metric | Definition |
|---|---|
| Drawdown[t] | `equity[t] / running_max(equity[0..t]) - 1` — on the strategy's OWN equity curve, **not** to be confused with the SPY-drawdown-from-ATH signal a ladder-like strategy computes internally (§2's deliberate separation) |
| Max drawdown | `min(Drawdown[t])` over the run |
| Max drawdown duration | trading days from the peak preceding the max drawdown to its trough |
| Time to recover | trading days from that trough to the first day equity exceeds the prior peak; `None`/"not yet recovered" if the run ends still underwater — reported explicitly, never silently omitted |
| Time under water (TUW) | `count(t where Drawdown[t] < 0) / N` |

### Volatility & risk-adjusted

| Metric | Definition |
|---|---|
| Annualised volatility | `stdev_sample(r) * sqrt(252)` |
| Sharpe | `mean(r - rf_daily) / stdev_sample(r) * sqrt(252)`; `rf_daily` defaults to **0** — an explicit, labeled assumption (the operator has never specified a risk-free rate; §10.2 flags this as unspecified, not measured), overridable in config |
| Sortino | `mean(r - rf_daily) / downside_deviation * sqrt(252)`, where `downside_deviation = sqrt(sum(min(r[t] - rf_daily, 0) ** 2 for all t) / N)` — the full-period (not losses-only-denominator) definition, stated explicitly to avoid the two competing conventions in common use |
| Calmar | `CAGR / abs(max_drawdown)`; `None` if max drawdown is 0 (never divide-by-zero silently) |

### Exposure & cash drag

| Metric | Definition |
|---|---|
| Exposure | daily `(market value of open lots) / sleeve_equity`; report mean and median over the run |
| Cash drag | daily `sleeve_cash / sleeve_equity`; report mean over the run |

### Per-trade statistics (over the closed-lot trade log)

| Metric | Definition |
|---|---|
| Trade count | `N_trades` = number of closed lots |
| Win rate | `count(realized_pnl > 0) / N_trades`; `None` if `N_trades == 0` |
| Average win / average loss | mean `realized_pnl` over winning / losing trades respectively; `None` if that side is empty |
| Profit factor | `sum(winning realized_pnl) / abs(sum(losing realized_pnl))`; `None` if there are no losing trades (never reported as `inf`) |
| Expectancy | `mean(realized_pnl) / mean(cost_basis)` across all closed trades — average return per trade |
| Holding period | mean and median `exit_date - entry_date` in calendar days, over closed trades |
| Largest win / largest loss | `max`/`min` single-trade `realized_pnl` |

### Tail / worst-case

| Metric | Definition |
|---|---|
| Worst single day | `min(r[t])` |
| Best single day | `max(r[t])` |
| Historical VaR95 / VaR99 | 5th / 1st percentile of `r[t]` (empirical, not parametric — no distributional assumption is made about daily returns) |
| CVaR95 | mean of `r[t]` for all `t` where `r[t] <=` the VaR95 threshold |
| Longest losing streak | longest run of consecutive **closed trades** (ordered by exit date) with `realized_pnl < 0` |
| Longest consecutive down-day streak | longest run of consecutive days with `r[t] < 0` |

**Undefined-ratio convention, stated once:** any ratio with an empty or zero denominator
(profit factor with no losses, Calmar with zero max drawdown, win rate with zero trades)
reports `None`, never `0` or `inf` — the same "never silently default to a fabricated number"
discipline `strategy/cone.py`'s docstring already states for this codebase (raising rather
than returning a fabricated z-score on a zero-variance window). A metrics report that hides
an undefined ratio behind a fabricated `0.0` is exactly the kind of "green suite, wrong
assumption" failure §6 exists to prevent.

---

## 6. Defending against the project's signature failure mode

`HANDOFF §1`: five parser bugs where the code and its test encoded the same wrong assumption
and the suite stayed green — "a green suite is evidence about your fixtures, not about
eToro." A strategy-agnostic sandbox is a large new surface for exactly this pattern (a metric
formula and its unit test both getting the same algebra wrong, forever agreeing with each
other). Concrete defences:

1. **Known-answer fixtures, not self-referential ones.** `sandbox/fixtures/` ships small,
   hand-computed synthetic price/trade series (a handful of days, worked by hand or in a
   spreadsheet, checked into the repo) whose expected metric values are written down *before*
   `metrics.py` is implemented against them, never derived by running the code and copying its
   output as "the answer." At least one fixture must be a **buy-and-hold strategy with zero
   trades**, specifically to force every per-trade statistic's `None`-handling path (§5's
   convention) to be exercised, not skipped because no test ever produces an empty trade log.
2. **Independently-sourced data, not only synthetic data invented to match the code.** At
   least one validation fixture must be drawn from the *already-measured* live 4,184-bar SPY
   series (`HANDOFF §3`'s dataset) rather than data invented for the test — reusing a series
   this project measured independently of the sandbox, so the sandbox's ATH/drawdown/tier
   arithmetic can be checked against numbers already published in `HANDOFF §3`'s own table —
   the **4,184-bar/full-history row** (0.00% drawdown-understatement, 0.00% tier-difference),
   matching the live app's actual full-history-from-2010 configuration; the now-superseded
   819-bar row in the same table shows the identical result and remains available as a
   secondary, bounded-window comparison point — rather than only against numbers the sandbox's
   own author computed.
3. **Parity against `etoro_trader`'s original replay, for the ladder specifically**
   (`DESIGN.md` §10 acceptance criterion 2, carried forward unchanged): every divergence
   between the sandbox's ladder-adapter output and the original backtest must be traced to one
   of the four approved bug fixes (`DESIGN.md` §1.4) — none left unexplained. This is the one
   validation check that is strategy-specific by necessity (only the ladder has a prior
   implementation to diff against); future strategies get checks 1, 2, 4, 5 but not this one,
   unless they too port from an existing backtest.
4. **A zero-trade run is a hard failure, not a quiet pass.** Mirrors `HANDOFF §7` rule 1 ("a
   check that passes because nothing happened"): a multi-year sandbox run that places zero
   orders must fail validation loudly and require an explicit override to report as
   successful, exactly as `DESIGN.md` §11 step 11 already treats "every session saw 0
   positions" as *not* satisfying its acceptance criterion.
5. **Provenance stamped on every report, refused if missing.** Every `sandbox/report.py`
   output is stamped with: `source: "sandbox"` (never omitted, never confusable with a
   live-measured figure), the sandbox code's own version/commit hash, and the exact friction
   parameter set used for the run (which fee model values, which UPRO carry-rate assumption,
   whether the transactionFee toggle was on, T-30 vs. some other timing offset, the warm-up
   bar count actually loaded). `report.py` refuses to render a report missing this header — a
   sandbox number with no stamp is indistinguishable from a live one exactly the way a
   reconstructed `entry_tier` with no `entry_context_source` was indistinguishable from a
   measured one (`HANDOFF §3a`'s Case 3) — the same pathology, one layer up.
6. **A second, independently-derived computation for the highest-risk numbers.** CAGR and
   max-drawdown are each checked two ways in `validation.py` — once via the formulas in §5,
   once via a deliberately differently-shaped computation (e.g. CAGR cross-checked via
   compounding the daily-return series forward from `equity[0]` and comparing to `equity[-1]`
   directly, rather than trusting the closed-form power formula alone) — so a single algebra
   mistake in one formula does not sail through unchallenged by an independent path that would
   have caught it.

---

## 7. Open questions this sandbox is positioned to answer

- **T-10 vs. T-30 provisional-close divergence** (`DESIGN.md` §10 acceptance criterion 9,
  `HANDOFF §7`): the timing decision itself is closed (T-30 stays), but replaying with
  `broker_sim`'s provisional-close offset set to T-10 and to T-30 against the same settled
  series, and diffing `cone_5_z` and the resulting `OrderIntent`s, is exactly what §4.3's
  design already supports — the offset is a `broker_sim` parameter, not a hardcoded T-30.
- **Should the strategy rebalance toward the core floor?** (`HANDOFF §7`, unspecified across
  three handoffs.) The sandbox cannot answer this by itself — it needs a *second* strategy
  plug-in implementing a rebalancing variant of the ladder to run side by side against the
  measured ladder adapter under identical frictions and identical metrics (§5). The plug-in
  protocol (§2) is what makes that A/B comparison possible without touching the sandbox core;
  building that second strategy is future work, not part of this design.

---

## 8. Module / file layout

```
sandbox/                        # replaces the never-built forward_test/ sketch in DESIGN.md §2
  __init__.py
  protocol.py                   # Strategy Protocol, SessionContext, OrderIntent, PortfolioView,
                                 # StrategyUniverse, Bar, OpenLot (§2) — the plug-in contract, and
                                 # the only file every strategy adapter must depend on
  data_feed.py                  # loads SPY/UPRO/SCHG history for the sim range; enforces the
                                 # warm-up rule (§3: bounded required_history_bars AND unbounded
                                 # required_data_start_date), raises WarmupInsufficientError;
                                 # wraps market/window.py's fetch_symbol_window + validate_window_
                                 # completeness (documented contract, §0's re-verify note)
  broker_sim.py                 # fake broker: min-size floors (§4.2), T-30 provisional/settled
                                 # fill timing (§4.3), no-slippage-beyond-that assumption stated
                                 # per report (§4.3)
  fees.py                       # the settled carrying-fee model (§4.1) + labeled UPRO-rate and
                                 # transactionFee assumptions, never silently defaulted
  ledger.py                     # daily sleeve equity curve + closed-lot trade log (§5's two
                                 # source-of-truth structures)
  runner.py                     # day-by-day loop: constructs a fresh Strategy instance each
                                 # simulated day, load_state()/get_state() round-trip (§2), calls
                                 # broker_sim to fill OrderIntents, updates ledger
  metrics.py                    # the full metric set (§5), computed once off ledger.py's output,
                                 # identically regardless of which strategy ran
  validation.py                 # known-answer fixtures, independently-sourced-data check,
                                 # etoro_trader parity harness, zero-trade hard-fail, the
                                 # second-computation cross-checks (§6)
  report.py                     # renders one run's metrics + trade log + provenance stamp (§6.5)
                                 # to a stable artifact (JSON, plus a short Markdown summary)
  strategies/
    ladder_adapter.py            # wraps strategy/ladder.py + strategy/cone.py + strategy/state.py
                                 # AND calls market/window.py's shared pure ATH/drawdown/tier
                                 # derivation (§8.1) -- it does NOT recompute ATH/drawdown/tier
                                 # itself. Behind Strategy (§2) — the FIRST plug-in, not a special
                                 # case; this is the only file in sandbox/ allowed to know the
                                 # ladder exists, and the only one blocked on the ladder.py/
                                 # state.py stubs (and, per §8.1, on the small window.py refactor
                                 # that makes the derivation function public)
  fixtures/
    known_answer_synthetic.json  # hand-computed small series (§6.1), incl. the zero-trade case
    spy_4184bar_subset.json      # slice of the already-measured HANDOFF §3 dataset (§6.2)
tests/
  sandbox/                      # unit tests for every module above, run on desktop CPython —
                                 # nothing here needs Android, Chaquopy, or a real eToro key
```

---

## 8.1 The derivation seam: reusing `market/window.py`, not recomputing it

**The problem this corrects.** §8's module layout, as first drafted, named
`strategy/ladder.py` + `strategy/cone.py` + `strategy/state.py` as everything
`ladder_adapter.py` wraps. That is incomplete in a way that matters: **ATH, drawdown, and tier
are derived in `market/window.py`**, not in any of those three modules — specifically in
`_spy_context_with_provisional`, the function `assemble_spy_signal` calls to turn a fetched SPY
window plus today's provisional close into a `SpyContext` (ath/close/drawdown/tier). A
`ladder_adapter.py` that only wraps ladder/cone/state has no path to that function at all, so as
specified it would have to **recompute** ATH/drawdown/tier itself from raw `price_history` —
a second implementation of the exact arithmetic that produced this project's two live `window.py`
bugs this week (the 276-bar-not-all-time ATH, and the `adjClose`-vs-raw-`close` basis mixup,
`board nightwatch/handoff-2026-09-05-evening.md` §3-§4). That is unacceptable for a reason specific to this
project, not a generic "don't repeat yourself": a second implementation would let the sandbox
validate the ladder against **its own copy** of the ATH/drawdown/tier logic rather than against
the code that actually trades real money, and any divergence between the two would surface as
exactly this project's signature pathology — a plausible number sitting next to a real one,
each agreeing with itself.

**The fix: share the pure derivation, replace only the network fetch.** Reading
`market/window.py` end to end shows it already separates cleanly into a fetch half and a derive
half, even though the derive half is not yet exposed for reuse:

- **Fetch (network-shaped, NOT reusable by the sandbox as-is):** `fetch_symbol_window` (one
  `TiingoClient.get_eod_history` call) and `resolve_provisional_close` (splices today's
  provisional close from a live `IexQuote`/`Candle`). Both take or produce values that only
  exist in a live session. The sandbox's `data_feed.py` already does not call these for its own
  historical replay (§3's warm-up rule instead governs how much history `data_feed.py` itself
  loads via its own historical source), and `broker_sim.py` (§4.3) is the sandbox's own analogue
  of `resolve_provisional_close` — it splices the **simulated** day's provisional close (T-30
  offset against the settled series) exactly the way live splices a real one. This half is
  naturally, and correctly, replaced by the sandbox's historical feed and fake broker — nothing
  about it should be shared.
- **Derive (already pure, and this is the part that must be shared):**
  `_spy_context_with_provisional(history, provisional_close, *, open_lot_entry_spy_ath)` takes
  only a `Sequence[EodBar]`, a `float`, and a `Sequence[float]` — no `TiingoClient`, no network
  object, nothing live-only. It is, right now, already a pure function of its arguments;
  the only thing standing between it and reuse is that it is **module-private** (a leading
  underscore) and not part of `market/window.py`'s `__all__`. `cone_5_z`
  (`strategy/cone.py`) is in the same position and already fully reusable as specified — it is
  already public and already wrapped by `ladder_adapter.py` per §8's original text.

**The refactor this requires — specified here, NOT performed** (`market/window.py` has
uncommitted changes from another agent as of this writing; this document only specifies the
change, it does not make it): rename `_spy_context_with_provisional` to a public name — e.g.
`spy_context_from_history` — add it to `market/window.py`'s `__all__`, and update
`assemble_spy_signal`'s one call site to the new name. **No signature change, no logic change** —
this is a visibility change only, because the function was already pure. `SpyContext` itself
(the return type, defined in `market/adopt.py` and already imported by `window.py`) needs no
change at all; it is already shared between `window.py` and `adopt.py` today.

**What `ladder_adapter.py` then does, concretely:**

1. Reads `ctx.price_history["SPY"]` (both bases, per §2) from `SessionContext` — supplied by the
   sandbox's own historical feed, not a live fetch.
2. Extracts each `OpenLot`'s stamped `entry_spy_ath` from its opaque `tag` dict (the adapter
   itself stamps `tag["entry_spy_ath"]` when it opens a lot, mirroring
   `lot_annotations.entry_spy_ath` in production) to build the `open_lot_entry_spy_ath` sequence
   — the protocol's opaque `tag` field (§2) is exactly the mechanism designed for this kind of
   ladder-private data, so this does not require adding any ladder-shaped field to `OpenLot`.
3. Calls the shared `market.window.spy_context_from_history(history, provisional_close,
   open_lot_entry_spy_ath=...)` — the identical function, same arguments, same code path
   production uses — to get `SpyContext.ath`/`.drawdown`/`.tier`, and calls the already-public
   `strategy.cone.cone_5_z` for the cone signal, both **inside the adapter**, never inside the
   sandbox core.
4. Feeds those into `strategy/ladder.py`'s decision logic (once built) to produce `OrderIntent`s.

**Why this does not reopen §2's "no `tier`/`cone_5_z`/`ATH` in the protocol" decision.** Reuse of
the live derivation and a ladder-free `SessionContext` are not in tension — they operate at
different layers, and a future reader must not "simplify" this by concluding the sandbox core
now needs to know what a tier is. The **sandbox core** (`protocol.py`, `runner.py`, `ledger.py`,
`metrics.py`) still never sees `tier`, `cone_5_z`, or `ATH` — those fields still do not exist
anywhere in `SessionContext`/`OrderIntent`/`PortfolioView`. The **adapter** — a ladder-specific
file that already, by design, is "the only file in sandbox/ allowed to know the ladder exists"
(§8) — is the layer that calls the shared derivation and turns its output into ladder decisions,
entirely on its own side of the `Strategy` protocol boundary. A different future strategy with
no ATH-shaped signal at all would never call `spy_context_from_history` and would never see a
`tier` anywhere; the reuse is opt-in per adapter, not a sandbox-core dependency.

**This is the same shape of decision as §10.1, applied one level down.** §10.1 correctly refuses
to drag the twelve Android-specific preflight gates into the sandbox, because they are
phone/network/session concerns with no meaning in a desktop simulation. That is a decision about
*orchestration* (`engine/preflight.py`, `engine/session.py`), not about *market-data math*
(`market/window.py`). "Don't simulate the phone" and "don't reuse the market-data math" are
different claims, and only the first is correct — §10.1 is updated below to say both halves
plainly, so a future reader does not conflate "the sandbox doesn't need Android preflight" with
"the sandbox doesn't need `window.py`."

---

## 9. Phased build order and dependencies

**What is blocked on the current stubs, and what is not.** `strategy/ladder.py` and
`strategy/state.py` are comment-only TODOs as of this writing; `engine/session.py` is too, but
§1 already establishes the sandbox does not depend on it at all. `strategy/cone.py` is **not**
a stub — it is a fully implemented, tested pure function (`cone_5_z`) — so anything that only
needs a SPY signal, not the full ladder, can use it today.

| Phase | Builds | Depends on | Can start now? |
|---|---|---|---|
| 1 | `protocol.py` | nothing | **yes** — pure interface definitions |
| 2 | `data_feed.py`, the warm-up check | `market/window.py`'s documented `fetch_symbol_window`/`validate_window_completeness` contract | **yes**, though re-verify against the concurrent edit once it lands (§0) |
| 3 | `broker_sim.py`, `fees.py` | phase 1 (protocol types); the measured/unmeasured friction data in §4, which is all already published | **yes** |
| 4 | `ledger.py`, `runner.py` | phases 1–3 | **yes** — can and should be tested end-to-end against a **trivial dummy strategy** (e.g. "always hold cash" or "buy-and-hold SPY-proxy-via-UPRO") written just for this phase, so the loop, warm-up gate, and fill mechanics are proven before the ladder exists |
| 5 | `metrics.py` | phase 4's ledger output shape | **yes**, developed against the dummy strategy's output and the known-answer fixtures (§6.1) |
| 6 | `validation.py` (fixtures 1–2, zero-trade check, second-computation cross-checks) | phases 4–5 | **yes** |
| 7 | `strategies/ladder_adapter.py` | **`strategy/ladder.py` and `strategy/state.py` must exist first**, AND the §8.1 refactor (making `market/window.py`'s `_spy_context_with_provisional` public as `spy_context_from_history`) must have landed — this is the one piece of the sandbox that is genuinely blocked | **no** — waits on those stubs and on the small window.py refactor |
| 8 | `validation.py`'s `etoro_trader` parity check (§6.3), `DESIGN.md` §10 acceptance criteria 2–3, 9 | phase 7 | **no** — waits on phase 7 |
| 9 | `report.py`'s full provenance stamping, first real ladder metrics report | phases 5, 7 | **no** — waits on phase 7 |

**Practical consequence:** roughly two-thirds of the sandbox (phases 1–6) can be built and
fully unit-tested today, in parallel with whoever ports `ladder.py`/`state.py` — the sandbox's
own correctness does not need to wait on the strategy port at all, only its *first realistic
result* does.

---

## 10. Disagreements, ambiguities, and doc contradictions

### 10.1 Disagreement with `DESIGN.md` §10's original conception

`DESIGN.md` §10 says the forward test "drives the real live engine — the same session state
machine, preflight, executor." I depart from this deliberately (§1): most of the twelve
preflight gates (power/permissions, network, clock, auth, environment match, calendar-as-a-
phone-concern) have no meaning in a desktop simulation, and coupling a plug-and-play sandbox
to the Android-shaped `engine/session.py` would mean every future strategy plug-in also has to
satisfy phone-specific gates it cannot fail or pass meaningfully. The operator's newer,
explicit request ("plug and play... any new strategy") is the more specific and more recent
instruction, so I am resolving the tension in its favor and recording the departure here
rather than silently reinterpreting §10.

**Stated plainly, because a future reader could otherwise "simplify" this into one rule that is
half wrong (recorded per the overseer's review, 2026-09-05): this is a decision about
orchestration, not about market-data math, and the two halves point in opposite directions:**

- **"Don't simulate the phone" is correct and stands.** `engine/preflight.py`'s twelve gates and
  `engine/session.py`'s orchestration (arm gate, Telegram, wake lock, reschedule) are
  phone/Android/network-specific and are correctly excluded (above).
- **"Don't reuse the market-data math" would NOT be correct, and this document does not do
  that.** ATH/drawdown/tier are derived in `market/window.py`, and §8.1 specifies
  `ladder_adapter.py` calling that derivation directly (via a small, specified-not-performed
  refactor exposing it publicly) rather than reimplementing it — because a second
  implementation of that exact logic would validate the ladder against its own copy of the code
  that produced this project's two live `window.py` bugs this week, not against the code that
  actually trades real money. Excluding Android preflight and reusing `market/window.py`'s pure
  derivation are both correct, simultaneously, for different reasons — see §8.1's closing
  paragraph for why they are not in tension.

### 10.2 Ambiguity: risk-free rate and other unstated modeling knobs

The operator has never specified a risk-free rate, and nothing in any doc addresses one. I
defaulted Sharpe/Sortino to `rf = 0` (§5) as the least-assumption choice, but this is a modeling
decision I made, not one derived from any of the source docs — flagged so it can be
overridden rather than mistaken for something the project decided.

### 10.3 Ambiguity: does the measured carrying fee already include what the $0.15 quote would add?

`FINDINGS_fees_2026-09-05.md` treats the carrying fee (settled, r=0.9918 against
investment×days) and the disclosed `transactionFee` quote as two separate things, and states
the latter's cash impact is unverified. It does not say whether, if `transactionFee` *does*
hit cash, it would be **additional** to the measured carrying fee or whether the measured
120-trade figure might already partially reflect something transaction-fee-shaped that the
correlation analysis simply didn't decompose. I designed `fees.py` (§4.1) to keep them as
independent, separately-toggleable knobs rather than silently summing them, specifically
because I cannot rule out double-counting from the docs as they stand — this should be
revisited the moment a real order settles and the question in `HANDOFF §7` is finally answered.

### 10.4 Doc contradiction, recorded for the corpse-table's sake

`DESIGN_market_data.md` §0.1 carries an explicit "superseded figures" table precisely because
this project has driven wrong estimated numbers into its own design as if they were measured,
twice. I did not find a *new* contradiction beyond what that table already tracks, but I am
citing its existence here deliberately: every friction figure in §4 above was checked against
the "replaced by" column of that table, not against the older prose it corrects (e.g. SCHG's
`minPositionExposure` is $10, not the earlier-stated $50; the sandbox's `broker_sim.py` must
be built against $10 and must never rediscover the superseded $50 from an old paragraph quoted
out of context).

### 10.5 A place I disagree with a literal reading of the brief

The brief's metric list could be read as asking for drawdown *tiers* (the ladder's 0–5
`TIER_CAPS` bands) as part of the universal metric set, since tiers are a "downside" concept
this project talks about constantly. I deliberately excluded tier/cone-signal metrics from
§5's universal set — they are the ladder's own internal signal buckets, not a
strategy-agnostic portfolio metric, and including them would reintroduce exactly the
ladder-leaking-into-the-interface problem §2 is designed to avoid. A ladder-specific report
can still show its own tier history; it just isn't part of the sandbox's cross-strategy
metric set.
