"""The ported Std Band Ladder Cash Recycle strategy (DESIGN.md §1.4).

Ported from ``etoro_trader/harness/strategies/std_band_ladder_cash_recycle.py``
(and the chain it derives from: ``std_band_ladder_martingale.py`` ->
``std_band_ladder_v4/v3/v2.py`` -> ``std_band_ladder.py``), against
``doc/strategy_std_band_ladder_cash_recycle.md``, with the six approved fixes
(DESIGN.md §1.4 "Bug fixes carried into etoroNightwatch (approved)" and the
task brief that added #5/#6):

1. **The duplicate ``_buy_if_allowed``.** The source defines this method
   TWICE (L99 dead, L143 wins in Python -- last definition shadows the
   first). The dead L99 copy is the one that applies the G-F total-UPRO
   ceiling *and* stamps ``entry_spy_ath``/``entry_spy_close``/
   ``entry_spy_drawdown`` on the new lot *and* decrements
   ``_recyclable_cash``; the L143 copy that actually runs only applies the
   G-F ceiling and calls the parent gate -- it never stamps anything or
   touches ``_recyclable_cash``. Concretely, this means every lot the source
   strategy opens has ``entry_spy_ath=None``, so
   ``_sell_profitable_lots``'s ``new_ath_sale`` test
   (``(lot.entry_spy_ath or self._ath_snapshot) < self._ath_snapshot``)
   always compares ``self._ath_snapshot < self._ath_snapshot``, which is
   never true -- **no re-entry bucket is ever created**, silently disabling
   the strategy's entire deferred re-entry mechanic
   (``REENTRY_TRANCHES``/``BUCKET_EXPIRY_BARS`` become inert). This module's
   :func:`_buy_if_allowed` is the merged, single definition: it applies the
   G-F ceiling, calls the tier-aware gate (the martingale/cash-recycle
   ``_deployed_amount`` override), and on a fill stamps
   ``entry_spy_ath``/``entry_spy_close``/``entry_spy_drawdown``/
   ``entry_tier`` and decrements ``recyclable_cash`` -- exactly what the
   source's *comment* describes but its *code* never executes.

2. **Bucket duplication in ``_process_reentry_buckets``.** The source's loop
   builds ``active`` incrementally as it walks ``self._buckets`` (appending
   ineligible/blocked buckets as it goes), then on a FILL does
   ``active.extend(b for b in self._buckets if b is not bucket and
   b.tranches_bought < 3)`` -- which re-adds every bucket already appended to
   ``active`` earlier in the same loop (a duplicate), AND adds every
   still-unvisited bucket later in ``self._buckets`` without the eligibility
   checks the loop would otherwise have applied to it. This module's
   :func:`_process_reentry_buckets` is a genuine single pass: each bucket is
   classified exactly once (expired / not-yet-eligible / blocked / filled),
   at most one bucket fills per call, and the result list contains each
   surviving bucket exactly once.

3. **Lot reconciliation (``_reconcile_lots``).** The source fabricates a
   ``LadderLot`` for any unmatched broker position it cannot pair to a
   pending local lot -- acceptable for a backtest replaying its own fills,
   wrong for a live app that restarts and would silently lose ``book``/
   vintage/entry-context metadata for a real position. This module has **no**
   reconciliation logic at all: :func:`decide` takes ``open_lots`` as an
   already-reconciled, already-trustworthy input. Reconciling broker
   positions against ``lot_annotations`` is ``engine/reconcile.py``'s job
   (unbuilt, gate 11, DESIGN_market_data.md §4.5) -- an unmatched broker
   position there is a **preflight failure**, never a fabricated lot.

4. **The Jan-2 budget crash (``_ensure_budget``).** The source raises
   ``ValueError`` when a new calendar year starts with no prior-year SCHG
   history (``self._prior_year_schg_values.get(self._budget_year, [])`` is
   empty) -- a live app restarting on Jan 2 with a genuinely empty history
   bucket would crash outright. :func:`_ensure_budget` here falls back to
   ``core_fraction * current sleeve equity`` and returns a warning string
   instead of raising; the caller (engine/session.py, unbuilt) is expected to
   relay that warning to Telegram.

5. **``CASH_BAND_FRACTION`` and ``MARTINGALE_RATIO`` are dropped as inert**
   (DESIGN.md §1.4 "Dropped parameters"): ``CASH_BAND_FRACTION`` is read only
   by a parent ``_cash_management`` this strategy's own cash management fully
   overrides; ``MARTINGALE_RATIO`` was baked into ``tier_caps`` at import
   time in the source and never read again. Neither appears in
   :class:`LadderConfig`. The six tier boundaries/caps come from
   ``strategy/params.py`` (``nightwatch.config.TIER_CAPS``/
   ``TIER_DRAWDOWN_BOUNDS``), which are the hardcoded constants DESIGN.md
   says they actually are, not a ratio to reproduce.

6. **``WARMUP_BARS`` -> 0 for live, with ``_bars`` persisted.** ``bars`` is
   part of :class:`~nightwatch.strategy.state.LadderState`, is incremented
   exactly once per :func:`decide` call, and is returned in
   :class:`LadderDecision.new_state` for the caller to persist every session
   (``strategy/state.py``). ``config.warmup_bars`` defaults to 0 in live
   config -- the *bar-count* gate is then a permanent no-op -- but the
   ``cone_5_z`` signal itself still returns ``None`` until >=265 SPY bars are
   available (``strategy/cone.py``), so trading is still correctly gated on
   real history even with the counter disabled. **The trap this fixes:** if
   a caller reconstructs the strategy fresh every session and does NOT
   persist/restore ``bars`` (or sets a nonzero ``warmup_bars``), ``bars``
   is 1 on every single call and the strategy sits in warmup forever, even
   with plenty of real history -- see ``tests/test_strategy_ladder.py``'s
   ``test_bars_not_persisted_stays_in_warmup_forever``.

Dropped beyond the brief's four/six items, and disclosed rather than silently
carried over -- see this task's final report for the reasoning:

* **``transaction_cost_bps`` is dropped.** The source deducts a flat 5bps
  from UPRO sale proceeds before sizing a re-entry bucket. ``FINDINGS_fees_
  2026-09-05.md`` measured eToro's real fee as a *carrying* fee proportional
  to size x holding time (r=0.9918 against ``investment x holding_days``),
  not a commission deducted from sale proceeds -- modelling a flat bps
  commission here would fabricate a fee mechanism the live account does not
  actually charge. Bucket/recyclable-cash sizing here uses **gross** sale
  proceeds (``upro_price * lot.units``, no deduction); the carrying fee
  belongs on the daily mark-to-market P&L surface (a sandbox/ledger concern,
  ``DESIGN_sandbox.md`` §4.5), not here.
* **Entry-context stamp uses today's fully-resolved ``SpyContext``, not a
  pre-bar snapshot.** The source snapshots ``self._ath`` BEFORE folding in
  today's price (``self._ath_snapshot = self._ath``) and stamps/derives
  against that snapshot, updating ``self._ath`` only afterward. This module
  is handed one ``SpyContext`` per call (``market.window.
  spy_context_from_history``, called by the caller/adapter) that already
  incorporates today's provisional close -- there is no separate "before
  today" value to snapshot, because nightwatch recomputes the full-history
  ATH fresh every session rather than carrying an incrementally-updated
  running value bar to bar. This module therefore stamps ``entry_spy_ath``/
  ``entry_spy_close``/``entry_spy_drawdown``/``entry_tier`` from the SAME
  ``SpyContext`` used for every other decision this call, and uses it (not
  a separate pre-bar value) for the ``new_ath_sale`` classification in
  :func:`_sell_profitable_lots`. The only day this can differ from the
  source's behaviour is one where SPY makes a brand-new ATH *and* a lot is
  bought on that same bar: the source would stamp a slightly-positive
  ``entry_spy_drawdown`` off yesterday's ATH, this module stamps exactly 0
  (tier 0) off today's. Flagged in the final report as a minor,
  deliberate divergence, not an oversight.

Kept faithful to the source, deliberately: the six-tier martingale schedule
(``strategy/params.py``), the SCHG core floor/new-ATH buy rule, the fixed vs
trailing exit modes, the dwell-bars persistence gate, the tier-progressive
z-score thresholds, and the "one tranche per bucket per bar" / "no same-bar
SCHG redeployment on a sale bar" invariants from
``doc/strategy_std_band_ladder_cash_recycle.md``.

**Design constraints honoured (task brief):** this module has no Android,
eToro-client, network, or database import. Every function here is a pure
function of its arguments -- data in, intents + new state out -- so
``DESIGN_sandbox.md``'s ``Strategy`` protocol adapter (``ladder_adapter.py``,
unbuilt) can wrap :func:`decide` directly; see the final report for exactly
how the protocol's ``on_session``/``get_state``/``load_state`` map onto it.
``market/window.py``'s ATH/drawdown/tier derivation is reused, not
recomputed: callers pass its output in as :class:`SpyContext` (duck-type
compatible with ``market.adopt.SpyContext``/``market.window.AssembledSignal
.spy_context`` -- same field names, deliberately, so either can be passed
directly).

**Scope boundary, stated plainly.** This module decides UPRO lot opens/
closes at lot granularity (each :class:`LadderLot` is one broker position in
production) and SCHG buys/sells at DOLLAR-AMOUNT granularity only -- it does
not track individual SCHG lots or pick "smallest lot first" the way the
source's ``_schg_sell_intents`` does. Resolving an SCHG dollar-amount close
into specific eToro ``position_id`` closes is the executor's job
(``engine/executor.py``, unbuilt), mirroring how DESIGN.md's own order
mapping (§4.3) already separates strategy intent from broker-shaped order
construction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Literal, Sequence

from .params import tier_cap, tier_for_drawdown

__all__ = [
    "SpyContext",
    "LadderConfig",
    "LadderLot",
    "ReentryBucket",
    "LadderIntent",
    "ClosedLot",
    "LadderInputs",
    "LadderState",
    "LadderDecision",
    "decide",
]

_EPS = 1e-9


# ---------------------------------------------------------------------------
# Data model -- every dataclass here is JSON-roundtrippable (to_dict/from_dict)
# so a caller (state.py, or the sandbox's ladder_adapter.py) can persist and
# restore it verbatim, per DESIGN_sandbox.md §2's Strategy.get_state()/
# load_state() contract.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpyContext:
    """SPY ATH/close/drawdown/tier for one trading date.

    Duck-type compatible with ``market.adopt.SpyContext`` and
    ``market.window.AssembledSignal.spy_context`` (same field names) --
    deliberately, so a caller can pass either object straight through without
    an adapter class. This module never imports ``market.window`` itself
    (design constraint: no market/db/network dependency here); the caller is
    responsible for calling ``market.window.spy_context_from_history`` and
    handing the result in.
    """

    ath: float
    close: float
    drawdown: float
    tier: int


@dataclass(frozen=True, slots=True)
class LadderConfig:
    """The strategy's tunable parameters (DESIGN.md §1.4's "Live parameter
    set" table), independent of ``nightwatch.config`` so this module has no
    import on it (keeps it reusable, unmodified, by a sandbox run with its
    own config surface) -- see :meth:`from_strategy_config` for the live
    bridge.
    """

    core_fraction: float = 0.4
    core_floor_pct: float = 0.5
    order_divisor: float = 60.0
    min_order_size: float = 50.0
    buy_threshold: float = -2.0
    sell_threshold: float = 3.0
    max_lots: int = 60
    warmup_bars: int = 0
    cash_target_multiplier: float = 8.0
    exit_mode: Literal["fixed", "trailing"] = "fixed"
    trailing_stop_pct: float = 0.05
    dwell_bars: int = 1
    tier_z_thresholds: tuple[float, ...] | None = None
    reentry_tranches: int = 3
    bucket_expiry_bars: int = 20

    def __post_init__(self) -> None:
        if self.exit_mode not in ("fixed", "trailing"):
            raise ValueError("LadderConfig.exit_mode must be 'fixed' or 'trailing'")
        if self.dwell_bars < 1:
            raise ValueError("LadderConfig.dwell_bars must be >= 1")
        if self.reentry_tranches != 3:
            # Matches the source's own v1 restriction (std_band_ladder_cash_
            # recycle.py: "v1 requires exactly three re-entry tranches") --
            # the tranche-fill/expiry logic below assumes three.
            raise ValueError("LadderConfig.reentry_tranches must be exactly 3 (v1)")
        if self.bucket_expiry_bars < 1:
            raise ValueError("LadderConfig.bucket_expiry_bars must be >= 1")
        if self.tier_z_thresholds is not None and len(self.tier_z_thresholds) != 6:
            raise ValueError("LadderConfig.tier_z_thresholds must have exactly 6 entries")
        if self.order_divisor <= 0:
            raise ValueError("LadderConfig.order_divisor must be positive")
        if self.min_order_size <= 0:
            raise ValueError("LadderConfig.min_order_size must be positive")

    @staticmethod
    def from_strategy_config(cfg: object) -> "LadderConfig":
        """Build a :class:`LadderConfig` from a live
        ``nightwatch.config.StrategyConfig`` by attribute name (duck typing
        -- no import of ``config.py`` here, per this module's no-network/
        no-DB design constraint; ``config.py`` is not itself DB/network,
        but importing it would still couple this module to nightwatch's own
        config schema instead of the sandbox's independent one).
        """
        return LadderConfig(
            core_fraction=cfg.core_fraction,  # type: ignore[attr-defined]
            core_floor_pct=cfg.core_floor_pct,  # type: ignore[attr-defined]
            order_divisor=cfg.order_divisor,  # type: ignore[attr-defined]
            min_order_size=cfg.min_order_size,  # type: ignore[attr-defined]
            buy_threshold=cfg.buy_threshold,  # type: ignore[attr-defined]
            sell_threshold=cfg.sell_threshold,  # type: ignore[attr-defined]
            max_lots=cfg.max_lots,  # type: ignore[attr-defined]
            warmup_bars=cfg.warmup_bars,  # type: ignore[attr-defined]
            cash_target_multiplier=cfg.cash_target_multiplier,  # type: ignore[attr-defined]
            exit_mode=cfg.exit_mode,  # type: ignore[attr-defined]
            trailing_stop_pct=cfg.trailing_stop_pct,  # type: ignore[attr-defined]
            dwell_bars=cfg.dwell_bars,  # type: ignore[attr-defined]
            tier_z_thresholds=cfg.tier_z_thresholds,  # type: ignore[attr-defined]
            reentry_tranches=cfg.reentry_tranches,  # type: ignore[attr-defined]
            bucket_expiry_bars=cfg.bucket_expiry_bars,  # type: ignore[attr-defined]
        )


@dataclass(frozen=True, slots=True)
class LadderLot:
    """One open (or just-closed) UPRO position, at the granularity ``decide``
    reasons about it.

    ``position_ref`` is the broker ``position_id`` in production (``str``,
    per ``db.repo.LotAnnotation.position_id``) or a sandbox-internal
    ``lot_ref`` (``DESIGN_sandbox.md`` §2's ``OpenLot.lot_ref``) in
    simulation -- this module never inspects its shape. It is ``None`` on a
    freshly-opened lot returned in :class:`LadderDecision.new_lots`: the
    caller learns the real id only once the broker fill is confirmed, and
    must fill it in itself when persisting (mirrors ``LotAnnotation`` being
    keyed on ``position_id``, unknown until the order settles).
    """

    symbol: str
    book: Literal["catapult", "reentry"]
    units: float
    entry_price: float
    cost_basis: float
    entry_date: str
    entry_reference_spy: float
    entry_spy_ath: float | None = None
    entry_spy_close: float | None = None
    entry_spy_drawdown: float | None = None
    entry_tier: int | None = None
    bucket_id: str | None = None
    position_ref: str | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol, "book": self.book, "units": self.units,
            "entry_price": self.entry_price, "cost_basis": self.cost_basis,
            "entry_date": self.entry_date,
            "entry_reference_spy": self.entry_reference_spy,
            "entry_spy_ath": self.entry_spy_ath,
            "entry_spy_close": self.entry_spy_close,
            "entry_spy_drawdown": self.entry_spy_drawdown,
            "entry_tier": self.entry_tier, "bucket_id": self.bucket_id,
            "position_ref": self.position_ref,
        }

    @staticmethod
    def from_dict(data: dict) -> "LadderLot":
        return LadderLot(**data)


@dataclass(frozen=True, slots=True)
class ReentryBucket:
    """A deferred re-entry eligibility record (mirrors ``db.repo.Bucket``)."""

    bucket_id: str
    sale_date: str
    sale_spy_close: float
    proceeds: float
    tranche_amount: float
    created_bar: int
    tranches_bought: int = 0
    last_reference_spy: float = 0.0

    @property
    def remaining(self) -> float:
        return max(0.0, self.proceeds - self.tranches_bought * self.tranche_amount)

    def to_dict(self) -> dict:
        return {
            "bucket_id": self.bucket_id, "sale_date": self.sale_date,
            "sale_spy_close": self.sale_spy_close, "proceeds": self.proceeds,
            "tranche_amount": self.tranche_amount,
            "created_bar": self.created_bar,
            "tranches_bought": self.tranches_bought,
            "last_reference_spy": self.last_reference_spy,
        }

    @staticmethod
    def from_dict(data: dict) -> "ReentryBucket":
        return ReentryBucket(**data)


@dataclass(frozen=True, slots=True)
class LadderIntent:
    """An order the caller must place. Deliberately shaped like
    ``DESIGN_sandbox.md`` §2's ``OrderIntent`` (``symbol``/``side``/
    ``amount_usd``/``lot_ref``/``reason``/``book``) rather than
    ``common.contracts.decision.OrderIntent`` (the etoro_trader backtest
    type) or ``db.repo.Intent`` (the live eToro-shaped type) -- neither
    belongs in a module with no eToro/network dependency.
    """

    symbol: str
    side: Literal["open", "close"]
    amount: float | None = None
    position_ref: str | None = None
    reason: str = ""
    book: str | None = None


@dataclass(frozen=True, slots=True)
class ClosedLot:
    """Exit facts for one lot closed this call, for the caller to persist
    (mirrors the exit_* columns ``db.repo.LotAnnotation`` reserves, and the
    source's ``closed_trade_metadata`` entries)."""

    position_ref: str | None
    exit_date: str
    exit_price: float
    exit_spy_close: float
    exit_spy_ath: float
    exit_spy_drawdown: float
    exit_proceeds: float
    realized_pnl: float
    sale_batch_id: str
    cost_basis: float
    entry_spy_ath: float | None


@dataclass(frozen=True, slots=True)
class LadderInputs:
    """Everything :func:`decide` needs for one trading day, besides state."""

    trading_date: str
    year: int
    spy: SpyContext
    cone_z: float | None
    upro_price: float
    schg_price: float
    cash: float
    equity: float
    schg_position_value: float
    open_lots: Sequence[LadderLot]
    buckets: Sequence[ReentryBucket]
    prior_year_schg_values: Sequence[float] = ()


@dataclass(frozen=True, slots=True)
class LadderState:
    """Everything :func:`decide` must carry across calls (mirrors
    ``db.repo.StrategyState`` field-for-field so ``strategy/state.py``'s
    bridge is a straight mapping). JSON-roundtrippable via
    :meth:`to_dict`/:meth:`from_dict`.
    """

    bars: int = 0
    ath: float | None = None
    below_streak: int = 0
    recyclable_cash: float = 0.0
    budget_year: int | None = None
    budget: float | None = None
    order_size: float | None = None
    cash_target: float | None = None
    sale_batch_sequence: int = 0
    sale_batch_date: str | None = None
    # Trailing-exit peak/armed state, keyed by lot position_ref -- only
    # populated while config.exit_mode == "trailing"; empty dict otherwise.
    trail_state: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "bars": self.bars, "ath": self.ath,
            "below_streak": self.below_streak,
            "recyclable_cash": self.recyclable_cash,
            "budget_year": self.budget_year, "budget": self.budget,
            "order_size": self.order_size, "cash_target": self.cash_target,
            "sale_batch_sequence": self.sale_batch_sequence,
            "sale_batch_date": self.sale_batch_date,
            "trail_state": self.trail_state,
        }

    @staticmethod
    def from_dict(data: dict) -> "LadderState":
        return LadderState(
            bars=data.get("bars", 0), ath=data.get("ath"),
            below_streak=data.get("below_streak", 0),
            recyclable_cash=data.get("recyclable_cash", 0.0),
            budget_year=data.get("budget_year"), budget=data.get("budget"),
            order_size=data.get("order_size"),
            cash_target=data.get("cash_target"),
            sale_batch_sequence=data.get("sale_batch_sequence", 0),
            sale_batch_date=data.get("sale_batch_date"),
            trail_state=dict(data.get("trail_state") or {}),
        )


@dataclass(frozen=True, slots=True)
class LadderDecision:
    """The result of one :func:`decide` call."""

    intents: list[LadderIntent]
    new_lots: list[LadderLot]
    updated_buckets: list[ReentryBucket]
    closed_lots: list[ClosedLot]
    new_state: LadderState
    reason: str
    warnings: list[str] = field(default_factory=list)
    diagnostics: dict = field(default_factory=dict)


def _floor_cents(amount: float) -> float:
    return math.floor(max(0.0, amount) * 100.0 + _EPS) / 100.0


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------


def _ensure_budget(
    *, year: int, state: LadderState, prior_year_schg_values: Sequence[float],
    equity: float, config: LadderConfig,
) -> tuple[float, float, float, int, str | None]:
    """Roll the annual budget over on a calendar-year change.

    Returns ``(budget, order_size, cash_target, budget_year, warning)``.
    Fix #4: falls back to ``core_fraction * equity`` with a warning instead
    of raising when the prior year has no recorded SCHG history (the Jan-2
    restart crash).
    """
    if state.budget_year == year:
        return state.budget or 0.0, state.order_size or 0.0, state.cash_target or 0.0, year, None

    warning: str | None = None
    if state.budget_year is None:
        raw = config.core_fraction * equity
    else:
        if not prior_year_schg_values:
            raw = config.core_fraction * equity
            warning = (
                f"budget rollover {state.budget_year}->{year}: no prior-year SCHG "
                f"history recorded; falling back to core_fraction * current equity "
                f"(fix #4 -- the source strategy raises here)"
            )
        else:
            raw = config.core_fraction * (sum(prior_year_schg_values) / len(prior_year_schg_values))
    budget = math.floor(max(0.0, raw) / 20.0) * 20.0
    order_size = max(budget / config.order_divisor, config.min_order_size)
    cash_target = config.cash_target_multiplier * order_size
    return budget, order_size, cash_target, year, warning


# ---------------------------------------------------------------------------
# Entry gate
# ---------------------------------------------------------------------------


def _entry_threshold(dd: float, config: LadderConfig) -> float:
    tier = tier_for_drawdown(dd)
    if config.tier_z_thresholds is None:
        return config.buy_threshold
    return config.tier_z_thresholds[tier]


def _deployed_amount(open_lots: Sequence[LadderLot], ath: float, current_tier: int) -> float:
    """Cost basis counted against the current tier's budget cap.

    Mirrors ``StdBandLadderCashRecycleHarness._deployed_amount``: only
    ``catapult``-book lots count (deferred re-entry is a separate book,
    §"Deferred re-entry has a separate UPRO budget book"), and each lot is
    reclassified against the LIVE ath every call -- a lot bought at a
    shallow tier is dynamically counted against a deeper tier's cap once the
    market has fallen that far, and drops back out once the market recovers.
    This uses the lot's continuous ``entry_reference_spy``, never the
    stamped discrete ``entry_tier`` field -- see the final report for why
    that distinction is exactly the answer to the entry_tier question.
    """
    total = 0.0
    for lot in open_lots:
        if lot.book == "reentry":
            continue
        lot_dd = lot.entry_reference_spy / ath - 1.0 if ath else 0.0
        if tier_for_drawdown(lot_dd) <= current_tier:
            total += lot.cost_basis
    return total


def _buy_if_allowed(
    *, inputs: LadderInputs, config: LadderConfig, budget: float, order_size: float,
    recyclable_cash: float,
) -> tuple[list[LadderIntent], list[LadderLot], float, list[str]]:
    """Fix #1: the merged buy gate -- G-F ceiling, then the tier-aware gate,
    then (only on a fill) the entry-context stamp and recyclable-cash
    decrement.
    """
    reasons: list[str] = []
    total_deployed = sum(lot.cost_basis for lot in inputs.open_lots)
    if total_deployed + order_size > budget + _EPS:
        return [], [], recyclable_cash, ["G-F total UPRO ceiling"]

    ath = inputs.spy.ath
    current_tier = inputs.spy.tier
    deployed = _deployed_amount(inputs.open_lots, ath, current_tier)
    tier_cap_amount = budget * tier_cap(current_tier)
    effective_cap = min(
        tier_cap_amount,
        deployed + max(0.0, inputs.schg_position_value - config.core_floor_pct * inputs.equity),
    )
    if deployed + order_size > effective_cap + _EPS:
        reasons.append("G-A budget")
    if inputs.cash + _EPS < order_size:
        reasons.append("G-B cash")
    if inputs.schg_position_value - config.core_floor_pct * inputs.equity < order_size:
        reasons.append("G-C core floor")
    if len(inputs.open_lots) >= config.max_lots:
        reasons.append("G-E lot cap")
    if reasons:
        return [], [], recyclable_cash, reasons

    amount = _floor_cents(order_size)
    units = amount / inputs.upro_price
    dd = inputs.spy.drawdown
    lot = LadderLot(
        symbol="UPRO", book="catapult", units=units, entry_price=inputs.upro_price,
        cost_basis=amount, entry_date=inputs.trading_date,
        entry_reference_spy=inputs.spy.close, entry_spy_ath=ath,
        entry_spy_close=inputs.spy.close, entry_spy_drawdown=dd,
        entry_tier=current_tier,
    )
    new_recyclable = _floor_cents(max(0.0, recyclable_cash - amount)) if recyclable_cash > 0.0 else recyclable_cash
    intent = LadderIntent(symbol="UPRO", side="open", amount=amount, reason="ladder buy", book="catapult")
    return [intent], [lot], new_recyclable, []


# ---------------------------------------------------------------------------
# Exits
# ---------------------------------------------------------------------------


def _fixed_exit(inputs: LadderInputs) -> list[LadderLot]:
    """Lots eligible for a fixed-rule close: profitable, any book."""
    candidates = [
        lot for lot in inputs.open_lots
        if inputs.upro_price * lot.units > lot.cost_basis
    ]
    return sorted(candidates, key=lambda lot: lot.entry_date)


def _trailing_exit(
    inputs: LadderInputs, cone_z: float | None, config: LadderConfig, trail_state: dict,
) -> tuple[list[LadderLot], dict]:
    """Mirrors ``_trailing_exit``: arms on the same first-crossing condition
    as the fixed rule, then closes only after a pullback from the lot's own
    post-arming peak while still profitable overall. ``trail_state`` is
    keyed by ``position_ref`` (falls back to ``entry_date`` for a
    just-opened, not-yet-reconciled lot, matching the source's use of
    ``lot.date`` as a key -- unique among concurrently open lots since at
    most one UPRO buy fires per bar).
    """
    new_state = dict(trail_state)
    to_close: list[LadderLot] = []
    live_keys = {lot.position_ref or lot.entry_date for lot in inputs.open_lots}
    for key in list(new_state.keys()):
        if key not in live_keys:
            del new_state[key]
    for lot in sorted(inputs.open_lots, key=lambda item: item.entry_date):
        if lot.position_ref is None:
            continue
        key = lot.position_ref
        state = new_state.setdefault(key, {"peak": lot.entry_price, "armed": 0.0})
        if not state["armed"]:
            profitable = inputs.upro_price * lot.units > lot.cost_basis
            if cone_z is not None and cone_z > config.sell_threshold and profitable:
                state["armed"] = 1.0
                state["peak"] = inputs.upro_price
            continue
        state["peak"] = max(state["peak"], inputs.upro_price)
        pulled_back = inputs.upro_price <= state["peak"] * (1.0 - config.trailing_stop_pct)
        still_profitable = inputs.upro_price > lot.entry_price
        if pulled_back and still_profitable:
            to_close.append(lot)
    return to_close, new_state


def _sell_lots(
    *, inputs: LadderInputs, candidates: list[LadderLot], state: LadderState, config: LadderConfig,
) -> tuple[list[LadderIntent], list[ClosedLot], float, int, str | None, bool, bool, str | None]:
    """Close ``candidates``, aggregate proceeds into one same-day sale
    batch, and classify it new-ATH (deferred re-entry bucket) or
    no-new-ATH (recyclable cash). Returns ``(intents, closed_lots,
    proceeds, sale_batch_sequence, sale_batch_date, made_a_sale,
    new_ath_sale, batch_id)``.
    """
    if not candidates:
        return [], [], state.recyclable_cash, state.sale_batch_sequence, state.sale_batch_date, False, False, None

    ath = inputs.spy.ath
    spy_close = inputs.spy.close
    sale_batch_sequence = state.sale_batch_sequence
    sale_batch_date = state.sale_batch_date
    if sale_batch_date != inputs.trading_date:
        sale_batch_sequence += 1
        sale_batch_date = inputs.trading_date
    batch_id = f"{inputs.trading_date}-{sale_batch_sequence}"

    intents = [
        LadderIntent(
            symbol="UPRO", side="close", position_ref=lot.position_ref,
            reason="profitable lot above sell threshold", book=lot.book,
        )
        for lot in candidates
    ]
    gross_proceeds = sum(inputs.upro_price * lot.units for lot in candidates)
    proceeds = _floor_cents(gross_proceeds)  # gross -- see module docstring on transaction_cost_bps

    new_ath_sale = any((lot.entry_spy_ath or ath) < ath - _EPS for lot in candidates)

    closed: list[ClosedLot] = []
    for lot in candidates:
        exit_proceeds = _floor_cents(inputs.upro_price * lot.units)
        closed.append(ClosedLot(
            position_ref=lot.position_ref, exit_date=inputs.trading_date,
            exit_price=inputs.upro_price, exit_spy_close=spy_close,
            exit_spy_ath=ath, exit_spy_drawdown=inputs.spy.drawdown,
            exit_proceeds=exit_proceeds, realized_pnl=exit_proceeds - lot.cost_basis,
            sale_batch_id=batch_id, cost_basis=lot.cost_basis,
            entry_spy_ath=lot.entry_spy_ath,
        ))

    return intents, closed, proceeds, sale_batch_sequence, sale_batch_date, True, new_ath_sale, batch_id


# ---------------------------------------------------------------------------
# Deferred re-entry
# ---------------------------------------------------------------------------


def _process_reentry_buckets(
    *, inputs: LadderInputs, config: LadderConfig, budget: float, bars: int,
) -> tuple[list[LadderIntent], list[LadderLot], list[ReentryBucket]]:
    """Fix #2: a genuine single pass. Drop expired buckets (recording the
    event is the caller's job via the returned diff, not this function's --
    it simply omits them), fill at most ONE bucket, then return every
    surviving bucket exactly once.
    """
    today = inputs.trading_date
    spy_close = inputs.spy.close
    filled = False
    active: list[ReentryBucket] = []
    intents: list[LadderIntent] = []
    new_lots: list[LadderLot] = []
    total_deployed_both_books = sum(lot.cost_basis for lot in inputs.open_lots)

    for bucket in inputs.buckets:
        if bars - bucket.created_bar >= config.bucket_expiry_bars:
            continue  # expired -- dropped, never forced
        if filled:
            active.append(bucket)
            continue
        if bucket.sale_date >= today or spy_close >= bucket.last_reference_spy:
            active.append(bucket)
            continue
        if (
            inputs.cash < bucket.tranche_amount - _EPS
            or len(inputs.open_lots) >= config.max_lots
            or total_deployed_both_books + bucket.tranche_amount > budget + _EPS
        ):
            active.append(bucket)
            continue
        units = bucket.tranche_amount / inputs.upro_price
        filled_bucket = replace(
            bucket, tranches_bought=bucket.tranches_bought + 1, last_reference_spy=spy_close,
        )
        ath = inputs.spy.ath
        lot = LadderLot(
            symbol="UPRO", book="reentry", units=units, entry_price=inputs.upro_price,
            cost_basis=bucket.tranche_amount, entry_date=today,
            entry_reference_spy=spy_close, entry_spy_ath=ath, entry_spy_close=spy_close,
            entry_spy_drawdown=inputs.spy.drawdown, entry_tier=inputs.spy.tier,
            bucket_id=bucket.bucket_id,
        )
        new_lots.append(lot)
        intents.append(LadderIntent(
            symbol="UPRO", side="open", amount=bucket.tranche_amount, book="reentry",
            reason=f"deferred reentry {bucket.bucket_id} {filled_bucket.tranches_bought}/3",
        ))
        filled = True
        if filled_bucket.tranches_bought < 3:
            active.append(filled_bucket)
        # else: fully filled -- dropped, not re-appended.

    return intents, new_lots, active


# ---------------------------------------------------------------------------
# Cash management
# ---------------------------------------------------------------------------


def _schg_buy_intents(amount: float, order_size: float) -> list[LadderIntent]:
    intents: list[LadderIntent] = []
    remaining = _floor_cents(amount)
    while remaining > 0.0:
        lot_amount = _floor_cents(min(order_size, remaining))
        if lot_amount <= 0.0:
            break
        intents.append(LadderIntent(symbol="SCHG", side="open", amount=lot_amount, reason="cash to core"))
        remaining = _floor_cents(remaining - lot_amount)
    return intents


# ---------------------------------------------------------------------------
# The public entry point
# ---------------------------------------------------------------------------


def decide(inputs: LadderInputs, state: LadderState, config: LadderConfig) -> LadderDecision:
    """One trading day's decision -- the sole public entry point.

    Mirrors ``StdBandLadderCashRecycleHarness.decide``'s sequence (module
    docstring's "Decision order" from the strategy doc), with fixes #1-#6
    applied. Pure: raises nothing (invalid inputs -- e.g. a non-positive
    price -- are a bug in the caller, not something this module is asked to
    validate; ``strategy/cone.py`` and ``market/window.py`` already enforce
    "no fabricated signal off bad data" at their own layer).
    """
    warnings: list[str] = []
    bars = state.bars + 1

    budget, order_size, cash_target, budget_year, budget_warning = _ensure_budget(
        year=inputs.year, state=state, prior_year_schg_values=inputs.prior_year_schg_values,
        equity=inputs.equity, config=config,
    )
    if budget_warning:
        warnings.append(budget_warning)

    ath = inputs.spy.ath
    # First run (no saved ATH): seed only. Comparing against 0 would count every
    # first bar as a new ATH and sweep spare cash into SCHG (T025, 2026-10-09 demo).
    new_ath_bar = state.ath is not None and ath > state.ath + _EPS

    base_state = LadderState(
        bars=bars, ath=ath, below_streak=state.below_streak,
        recyclable_cash=state.recyclable_cash, budget_year=budget_year, budget=budget,
        order_size=order_size, cash_target=cash_target,
        sale_batch_sequence=state.sale_batch_sequence, sale_batch_date=state.sale_batch_date,
        trail_state=state.trail_state,
    )

    if bars <= config.warmup_bars:
        reentry_intents, reentry_lots, updated_buckets = _process_reentry_buckets(
            inputs=inputs, config=config, budget=budget, bars=bars,
        )
        reason = "warmup: signal trades disabled"
        return LadderDecision(
            intents=reentry_intents, new_lots=reentry_lots, updated_buckets=updated_buckets,
            closed_lots=[], new_state=base_state, reason=reason,
            warnings=warnings,
            diagnostics={"bars": bars, "ath": ath, "new_ath_bar": new_ath_bar, "warmup": True},
        )

    cone_z = inputs.cone_z
    intents: list[LadderIntent] = []
    new_lots: list[LadderLot] = []
    closed_lots: list[ClosedLot] = []
    recyclable_cash = state.recyclable_cash
    made_a_sale = False
    new_bucket: ReentryBucket | None = None

    if config.exit_mode == "trailing":
        sell_candidates, trail_state = _trailing_exit(inputs, cone_z, config, state.trail_state)
    else:
        trail_state = state.trail_state
        sell_candidates = _fixed_exit(inputs) if (cone_z is not None and cone_z > config.sell_threshold) else []

    sale_batch_sequence = state.sale_batch_sequence
    sale_batch_date = state.sale_batch_date
    if sell_candidates:
        result = _sell_lots(inputs=inputs, candidates=sell_candidates, state=state, config=config)
        (sell_intents, closed_lots, proceeds, sale_batch_sequence, sale_batch_date,
         made_a_sale, new_ath_sale, batch_id) = result
        intents.extend(sell_intents)
        if made_a_sale:
            if new_ath_sale:
                tranche_amount = _floor_cents(proceeds / config.reentry_tranches)
                new_bucket = ReentryBucket(
                    bucket_id=batch_id, sale_date=inputs.trading_date,
                    sale_spy_close=inputs.spy.close, proceeds=proceeds,
                    tranche_amount=tranche_amount, created_bar=bars,
                    last_reference_spy=inputs.spy.close,
                )
            else:
                recyclable_cash = _floor_cents(recyclable_cash + proceeds)

    dd = inputs.spy.drawdown
    threshold = _entry_threshold(dd, config)
    below = cone_z is not None and cone_z < threshold
    below_streak = state.below_streak + 1 if below else 0
    if below and below_streak >= config.dwell_bars:
        buy_intents, buy_lots, recyclable_cash, buy_skip_reasons = _buy_if_allowed(
            inputs=inputs, config=config, budget=budget, order_size=order_size,
            recyclable_cash=recyclable_cash,
        )
        intents.extend(buy_intents)
        new_lots.extend(buy_lots)
    else:
        buy_skip_reasons = ["below threshold not met"] if not below else ["dwell not satisfied"]

    # Open lots as the reentry-bucket processor should see them: the ones
    # already open, plus any this same call just opened (mirrors the
    # source's single in-memory self._lots list being mutated in place
    # before _cash_management -> _process_reentry_buckets runs).
    lots_for_reentry = list(inputs.open_lots) + new_lots
    buckets_for_reentry = list(inputs.buckets)
    if new_bucket is not None:
        buckets_for_reentry.append(new_bucket)
    reentry_inputs = replace(inputs, open_lots=lots_for_reentry, buckets=buckets_for_reentry)
    reentry_intents, reentry_lots, updated_buckets = _process_reentry_buckets(
        inputs=reentry_inputs, config=config, budget=budget, bars=bars,
    )
    intents = reentry_intents + intents
    new_lots = new_lots + reentry_lots

    signal = "cone_z=none" if cone_z is None else f"cone_z={cone_z:.3f}"
    reason = f"{signal}; skips={', '.join(buy_skip_reasons)}" if buy_skip_reasons else signal

    diagnostics = {
        "bars": bars, "ath": ath, "new_ath_bar": new_ath_bar,
        "cash": inputs.cash, "recyclable_cash": recyclable_cash,
        "pending_reentry_amount": sum(b.remaining for b in updated_buckets),
        "open_lots": len(lots_for_reentry),
        "tier": inputs.spy.tier, "drawdown": dd,
    }

    if made_a_sale:
        new_state = replace(
            base_state, below_streak=below_streak, recyclable_cash=recyclable_cash,
            sale_batch_sequence=sale_batch_sequence, sale_batch_date=sale_batch_date,
            trail_state=trail_state,
        )
        return LadderDecision(
            intents=intents, new_lots=new_lots, updated_buckets=updated_buckets,
            closed_lots=closed_lots, new_state=new_state, reason=reason,
            warnings=warnings, diagnostics=diagnostics,
        )

    # No same-day sale batch: SCHG steps may run.
    tier1_reserve = _floor_cents(budget * tier_cap(0))
    if new_ath_bar:
        available = max(0.0, inputs.cash - cash_target - tier1_reserve)
        if recyclable_cash > 0.0:
            eligible_cash = max(0.0, recyclable_cash - tier1_reserve)
            amount = _floor_cents(min(eligible_cash, available))
        else:
            amount = _floor_cents(available)
        if amount > 0.0:
            intents.extend(_schg_buy_intents(amount, order_size))
            if recyclable_cash > 0.0:
                recyclable_cash = _floor_cents(recyclable_cash - amount)

    normal_ladder_buy = any(
        intent.symbol == "UPRO" and intent.side == "open"
        and not (intent.reason or "").startswith("deferred reentry")
        for intent in intents
    )
    if normal_ladder_buy and inputs.cash < cash_target:
        need = cash_target - inputs.cash
        available = max(0.0, inputs.schg_position_value - config.core_floor_pct * inputs.equity)
        sell_amount = _floor_cents(min(need, available))
        if sell_amount > 0.0:
            intents.append(LadderIntent(symbol="SCHG", side="close", amount=sell_amount, reason="fund ladder buy"))

    new_state = replace(
        base_state, below_streak=below_streak, recyclable_cash=recyclable_cash,
        sale_batch_sequence=sale_batch_sequence, sale_batch_date=sale_batch_date,
        trail_state=trail_state,
    )
    return LadderDecision(
        intents=intents, new_lots=new_lots, updated_buckets=updated_buckets,
        closed_lots=closed_lots, new_state=new_state, reason=reason,
        warnings=warnings, diagnostics=diagnostics,
    )
