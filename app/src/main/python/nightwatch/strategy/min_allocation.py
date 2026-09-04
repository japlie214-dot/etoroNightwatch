"""Minimum-viable sleeve cash: calculator, cache, and the two-tier UI warning
(DESIGN §4.3, §4.5, §4.6).

**Product decision, implemented as stated, not re-litigated here:** the
sleeve is now defined by **exclusion** (DESIGN §4.5, operator decision
2026-09-04, superseding the earlier inclusion model)::

    sleeve_cash = max(0.0, broker_available_cash - cash_reserve_usd)

``cash_reserve_usd`` (``nightwatch.config.StrategyConfig``) is the USD the
operator withholds from the app; ``sleeve_cash`` is only knowable at runtime,
once the broker's available cash has actually been read. That is the load-
bearing change this module makes relative to the version that shipped
against ``cash_allocation_usd``: the thresholds below are unchanged
arithmetic, but they are now requirements on **``sleeve_cash``** -- a runtime
quantity -- not on a config figure that was knowable at load time. Both a
runtime check (given actual available cash) and a config-time statement
("this reserve requires available cash of at least reserve + $X") are
provided; see :func:`allocation_warning` and :func:`reserve_requirement`.

If the sleeve is too small, the app **warns** on the UI/status/snapshot
payload -- it never blocks a session, never aborts a preflight check, never
raises. The one thing that stays a hard failure, unchanged, is a *single
order* that itself falls below an instrument's ``minPositionExposure`` at the
moment it would be placed
(``nightwatch.etoro.client.EtoroClient.check_eligibility_for_order``, and
gate 8 once ``engine/preflight.py`` is built, per its own header comment) --
that is a per-order broker rule ("blocked, not resized"), not the
allocation-level judgment call this module makes.

---------------------------------------------------------------------------
Derivation -- tier 1, "cannot trade at all"
---------------------------------------------------------------------------

Consider the first session: no open lots, no ``budget_override_usd``. This is
the case the warning exists for -- "am I even big enough to start" -- and the
one in which the sleeve terms of DESIGN §4.5 collapse to plain numbers:

    sleeve_equity == sleeve_cash == A          (no lots yet, so equity has no
                                                 position-value component)
    B  = core_fraction * A                     (§4.6 first-year budget rule)
    OS = max(B / order_divisor, min_order_size)
    CT = cash_target_multiplier * OS
    tier1_reserve = B * TIER_CAPS[0]           (the ladder's own protected
                                                 reserve -- see
                                                 std_band_ladder_cash_recycle.py
                                                 in etoro_trader: "Keep the
                                                 current tier-1 UPRO budget in
                                                 cash")

Two orders must be placeable out of A (i.e. out of ``sleeve_cash``) for the
strategy to do anything at all:

(1) **UPRO ladder order.** The ported strategy's own budget ceiling
    (``_total_upro_deployed() + order_size > budget`` -> Hold, never a broker
    call) requires ``OS <= B``; gate 8 requires ``OS >= upro_min``. While
    ``B / order_divisor <= min_order_size`` (the "floor regime" -- true for
    any config where the floor dominates, checked below rather than assumed),
    ``OS == min_order_size`` and the two conditions reduce to:

        OS >= upro_min   -->  automatically true whenever min_order_size >=
                               upro_min (true today: 50 >= 10); if it were not,
                               no A alone could fix it in the floor regime, so
                               the binding form there is instead
                               A >= upro_min * order_divisor / core_fraction.
        OS <= B          -->  A >= min_order_size / core_fraction

(2) **SCHG core order.** On a new-ATH bar the strategy buys SCHG only from
    ``available = cash - cash_target - tier1_reserve`` (same source file,
    "SCHG is bought only from the surplus above both the operating target and
    that protected ladder reserve"). For that buy to clear SCHG's
    ``minPositionExposure``:

        A - CT - B * TIER_CAPS[0] >= schg_min

    Substituting the floor-regime ``CT`` and ``B``:

        A * (1 - core_fraction * TIER_CAPS[0])
            >= schg_min + cash_target_multiplier * min_order_size

        A >= (schg_min + cash_target_multiplier * min_order_size)
             / (1 - core_fraction * TIER_CAPS[0])

The minimum viable sleeve is the larger of the two. Plugging in the live
config (core_fraction=0.4, order_divisor=60, min_order_size=50,
cash_target_multiplier=8, TIER_CAPS[0]≈0.100706, schg_min=50, upro_min=10):

    A_upro = 50 / 0.4                                      = 125.00
    A_schg = (50 + 8*50) / (1 - 0.4*0.100706)
           = 450 / 0.959718                                ≈ 468.89

    minimum_sleeve_cash_usd ≈ $468.89, binding on SCHG.

This disagrees with the brief's ~$5,000 sanity-check estimate, and the
derivation is trusted over it, per instruction -- see tier 2 below, which is
exactly that estimate, formalized and reported as its own number rather than
conflated with tier 1.

---------------------------------------------------------------------------
Derivation -- tier 2, "ladder cannot reach max_lots rungs"
---------------------------------------------------------------------------

``order_size = max(B / order_divisor, min_order_size)`` sits on the
``min_order_size`` floor for every ``A`` (sleeve_cash) up to the breakpoint at
which ``B / order_divisor`` itself reaches ``min_order_size``:

    A* = min_order_size * order_divisor / core_fraction

Below ``A*``, ``order_size`` is pinned at ``min_order_size`` no matter how
much bigger the sleeve gets within that range, and the number of ladder
rungs (UPRO lots) the budget can actually fund is
``floor(B / order_size) = floor(core_fraction * A / order_size)``, capped at
``max_lots`` -- never a bigger number than the strategy is designed to use.
At the live config this ratio is ``60 * 50 / 0.4 = 7500``: a $2,000 sleeve
funds only ``floor(0.4*2000 / 50) = 16`` of the 60 design rungs. This is the
"a $2,000 inclusion allocation funds only 16 of 60 ladder rungs" example that
motivated moving to the exclusion model in the first place (DESIGN §4.5) --
tier 2 turns that observation into a standing, config-driven check instead of
a one-off hand calculation.

---------------------------------------------------------------------------
Gate 8, once step 9 builds it (engine/preflight.py)
---------------------------------------------------------------------------

The current header comment specifies gate 8 as a single check,
"minPositionExposure <= order_size", which is correct for UPRO but does not
describe how SCHG is ever sized -- SCHG's buy amount is the new-ATH surplus
sweep above, never ``order_size`` directly (DESIGN §4.3 already flags this:
"SCHG's core buys are sized off core_floor_pct x sleeve_equity / the
surplus-sweep calculation ... not directly off order_size"). Gate 8 should
therefore check, for each managed symbol, the quantity that symbol will
actually be ordered in: ``order_size`` for UPRO, and the projected new-ATH
surplus-sweep amount (or, equivalently, this module's tier-1 threshold
compared against the runtime ``sleeve_cash``) for SCHG. Whatever gate 8
becomes, it must stay a **pass/fail gate on the actual order about to be
placed**, not a re-statement of this module's warning -- this module's job
stops at telling the operator ahead of time that a session is likely to find
nothing orderable (tier 1) or under-deployed (tier 2); gate 8's job is to
block the one broken order if it still gets that far.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..config import StrategyConfig, SymbolConfig, TIER_CAPS
from ..db.repo import Instrument, Repo

__all__ = [
    "MinAllocationResult",
    "MinAllocationCache",
    "compute_minimum_allocation",
    "affordable_rungs",
    "reserve_requirement",
    "allocation_warning",
]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class MinAllocationResult:
    """The two sleeve-cash thresholds plus everything needed to explain why
    -- a warning that doesn't say what to change is useless, which is the
    whole reason this carries more than one number.
    """

    #: Tier 1: below this, sleeve_cash cannot fund a first UPRO ladder order
    #: *and* a first SCHG core order -- the strategy can place nothing at all.
    minimum_sleeve_cash_usd: float
    #: Tier 2: below this, order_size sits on the min_order_size floor and
    #: the ladder cannot reach max_lots rungs, even though trading is
    #: possible. Equals ``min_order_size * order_divisor / core_fraction``.
    full_ladder_sleeve_cash_usd: float
    binding_symbol: str  # "SCHG" or "UPRO" -- whichever tier-1 constraint bound
    schg_min_position_exposure: float
    upro_min_position_exposure: float
    #: The strategy's own design capacity -- ``strategy.max_lots`` -- carried
    #: alongside the thresholds so callers never have to thread it through
    #: separately to compute a shortfall.
    design_rungs: int
    computed_at: str

    def as_dict(self) -> dict[str, Any]:
        """JSON-safe shape for the status/snapshot payload (DESIGN §9)."""
        return {
            "minimum_sleeve_cash_usd": self.minimum_sleeve_cash_usd,
            "full_ladder_sleeve_cash_usd": self.full_ladder_sleeve_cash_usd,
            "binding_symbol": self.binding_symbol,
            "schg_min_position_exposure": self.schg_min_position_exposure,
            "upro_min_position_exposure": self.upro_min_position_exposure,
            "design_rungs": self.design_rungs,
            "computed_at": self.computed_at,
        }


def _upro_minimum(
    core_fraction: float, order_divisor: int, min_order_size: float, upro_min: float
) -> float:
    """See derivation (1). ``min_order_size >= upro_min`` is the normal case
    (today: 50 >= 10); the other branch is a defensive fallback for a future
    config where the floor no longer dominates UPRO's own minimum.
    """
    if min_order_size >= upro_min:
        return min_order_size / core_fraction
    return upro_min * order_divisor / core_fraction


def _schg_minimum_floor_regime(
    core_fraction: float,
    min_order_size: float,
    cash_target_multiplier: float,
    tier1_cap: float,
    schg_min: float,
) -> float:
    """See derivation (2), floor-regime closed form."""
    denominator = 1.0 - core_fraction * tier1_cap
    return (schg_min + cash_target_multiplier * min_order_size) / denominator


def _schg_minimum_non_floor_regime(
    core_fraction: float,
    order_divisor: int,
    cash_target_multiplier: float,
    tier1_cap: float,
    schg_min: float,
) -> float:
    """SCHG's bound past the floor-regime breakpoint, where ``order_size ==
    core_fraction * A / order_divisor`` instead of sitting on
    ``min_order_size`` (see module docstring, tier-1 section).
    """
    denominator = (
        1.0
        - (cash_target_multiplier * core_fraction / order_divisor)
        - core_fraction * tier1_cap
    )
    return schg_min / denominator


def affordable_rungs(strategy: StrategyConfig, sleeve_cash: float) -> int:
    """How many UPRO ladder rungs a given ``sleeve_cash`` can actually fund.

    First-year, no-lots approximation (the same one tier 1 and tier 2 use):
    ``budget = core_fraction * sleeve_cash``, ``order_size =
    max(budget / order_divisor, min_order_size)``. The number of rungs the
    budget can cover is ``floor(budget / order_size)``, capped at
    ``max_lots`` -- the strategy is never designed to use more than that many
    regardless of how large the sleeve is.
    """
    if sleeve_cash <= 0:
        return 0
    budget = strategy.core_fraction * sleeve_cash
    order_size = max(budget / strategy.order_divisor, strategy.min_order_size)
    if order_size <= 0:
        return 0
    return min(strategy.max_lots, math.floor(budget / order_size))


def compute_minimum_allocation(
    strategy: StrategyConfig,
    schg_instrument: Instrument,
    upro_instrument: Instrument,
) -> MinAllocationResult | None:
    """Derive both sleeve-cash thresholds (module docstring has the full
    derivation for each).

    Returns ``None`` when the cached per-instrument minimums are not
    available yet -- first run, nothing probed against the live eligibility
    endpoint. Degrading gracefully here (never inventing a number, never
    raising) matters because this is a warning-producing path: a crash here
    would be a worse outcome than the warning it is trying to prevent.
    """
    schg_min = schg_instrument.min_position_exposure
    upro_min = upro_instrument.min_position_exposure
    if schg_min is None or upro_min is None:
        return None

    core_fraction = strategy.core_fraction
    order_divisor = strategy.order_divisor
    min_order_size = strategy.min_order_size
    cash_target_multiplier = strategy.cash_target_multiplier
    tier1_cap = TIER_CAPS[0]

    a_upro = _upro_minimum(core_fraction, order_divisor, min_order_size, upro_min)
    a_schg = _schg_minimum_floor_regime(
        core_fraction, min_order_size, cash_target_multiplier, tier1_cap, schg_min
    )

    # Self-consistency check: the floor-regime formulas assume
    # order_size == min_order_size, true only up to this breakpoint (also
    # tier 2's own threshold). If the winning candidate falls past it,
    # order_size would actually be scaling with budget by then -- recompute
    # the SCHG bound with the matching form instead of silently misapplying
    # the floor-regime one.
    full_ladder = min_order_size * order_divisor / core_fraction
    minimum = max(a_upro, a_schg)
    if minimum > full_ladder:
        a_schg = _schg_minimum_non_floor_regime(
            core_fraction, order_divisor, cash_target_multiplier, tier1_cap, schg_min
        )
        minimum = max(a_upro, a_schg)

    binding_symbol = "UPRO" if a_upro >= a_schg else "SCHG"

    return MinAllocationResult(
        minimum_sleeve_cash_usd=minimum,
        full_ladder_sleeve_cash_usd=full_ladder,
        binding_symbol=binding_symbol,
        schg_min_position_exposure=schg_min,
        upro_min_position_exposure=upro_min,
        design_rungs=strategy.max_lots,
        computed_at=_utcnow(),
    )


def _signature(
    strategy: StrategyConfig, schg: Instrument, upro: Instrument
) -> tuple[Any, ...]:
    """Everything the arithmetic reads, in one tuple. Two independent sources
    can change these: eToro's own eligibility minimums (probed live,
    persisted onto ``instruments`` by ``db.repo.Repo.upsert_instrument``) and
    ``config.toml`` between runs -- a cache keyed on time or on nothing would
    silently go stale under either.

    Deliberately excludes ``cash_reserve_usd``: the thresholds this module
    caches (tier 1 and tier 2) do not depend on the reserve at all -- only
    the runtime comparison against ``sleeve_cash`` does, and that comparison
    happens fresh on every call to :func:`allocation_warning`, never cached.
    """
    return (
        strategy.core_fraction,
        strategy.order_divisor,
        strategy.min_order_size,
        strategy.cash_target_multiplier,
        strategy.max_lots,
        TIER_CAPS[0],
        schg.min_position_exposure,
        upro.min_position_exposure,
    )


class MinAllocationCache:
    """Caches :func:`compute_minimum_allocation`, invalidated the instant any
    input changes.

    The arithmetic is cheap; this exists so the status/snapshot layer can
    call it on every poll without re-deriving the number by hand each time,
    while making a stale answer structurally impossible rather than merely
    unlikely: instead of a TTL, the cache is keyed on a signature of the
    exact inputs (per-instrument minimums plus the handful of strategy
    config scalars the formula reads). Any change to either -- eToro raising
    a minimum on its side, or an operator editing ``config.toml`` -- changes
    the signature and forces a fresh computation on the next call.

    One cache instance is meant to be held for the process lifetime (e.g. one
    per running session or one on the FastAPI app state), not recreated per
    call -- recreating it defeats the point.
    """

    def __init__(self) -> None:
        self._signature: tuple[Any, ...] | None = None
        self._result: MinAllocationResult | None = None

    def get(
        self, strategy: StrategyConfig, repo: Repo, symbols: SymbolConfig
    ) -> MinAllocationResult | None:
        """Return the cached result, recomputing first if anything changed.

        Returns ``None`` (and clears any prior cached value) when either
        instrument has not been probed yet -- see
        :func:`compute_minimum_allocation`.
        """
        schg = repo.get_instrument(symbols.core)
        upro = repo.get_instrument(symbols.risk)
        if schg is None or upro is None:
            self._signature = None
            self._result = None
            return None

        signature = _signature(strategy, schg, upro)
        if signature != self._signature:
            self._result = compute_minimum_allocation(strategy, schg, upro)
            self._signature = signature
        return self._result


def reserve_requirement(
    strategy: StrategyConfig,
    repo: Repo,
    symbols: SymbolConfig,
    cache: MinAllocationCache | None = None,
) -> dict[str, Any] | None:
    """Config-time statement of what a reserve requires, with no live
    ``broker_available_cash`` needed.

    Because ``sleeve_cash = max(0, broker_available_cash - cash_reserve_usd)``
    is only known once the broker is actually read, this restates both
    thresholds as a requirement on *available cash*: "this reserve requires
    available cash of at least reserve + $X to trade at all / to reach full
    ladder depth." Useful for a config-editing UI that wants to warn before a
    session ever runs. Returns ``None`` when eligibility data is not cached
    yet (same graceful-degradation contract as everything else here).
    """
    cache = cache or MinAllocationCache()
    result = cache.get(strategy, repo, symbols)
    if result is None:
        return None

    reserve = strategy.cash_reserve_usd
    return {
        "cash_reserve_usd": reserve,
        "required_available_cash_to_trade_usd": reserve + result.minimum_sleeve_cash_usd,
        "required_available_cash_for_full_ladder_usd": (
            reserve + result.full_ladder_sleeve_cash_usd
        ),
        "binding_symbol": result.binding_symbol,
        "design_rungs": result.design_rungs,
    }


def allocation_warning(
    strategy: StrategyConfig,
    repo: Repo,
    symbols: SymbolConfig,
    broker_available_cash: float | None,
    cache: MinAllocationCache | None = None,
) -> dict[str, Any] | None:
    """Build the two-tier warning payload for the status/snapshot layer
    (DESIGN §9), or ``None`` when there is nothing to warn about.

    This is now a **runtime** check: ``sleeve_cash`` (DESIGN §4.5, exclusion
    model) only exists once ``broker_available_cash`` has actually been read
    from the broker, so the caller must supply it -- there is no config-time
    substitute (see :func:`reserve_requirement` for the config-time
    statement instead).

    "Nothing to warn about" covers: no eligibility data cached yet (first
    run), ``broker_available_cash`` not known yet, and a sleeve at or above
    the full-ladder threshold (tier 2, the higher bar -- clearing it implies
    clearing tier 1 too). All indistinguishable to the caller, all resulting
    in ``None``.

    Two tiers, both **warnings only** -- this function never raises and
    never signals failure, per the operator's explicit instruction ("give
    warning on the UI but not hard block on the backend"):

    - ``"level": "critical"``, ``"code": "sleeve_cash_cannot_trade"`` -- the
      sleeve cannot fund a first UPRO order *and* a first SCHG order at all.
    - ``"level": "warning"``, ``"code": "sleeve_cash_limits_ladder_depth"`` --
      trading is possible, but ``order_size`` is stuck on the
      ``min_order_size`` floor and the ladder cannot reach ``max_lots``
      rungs.

    Wire it into whatever step 8 (embedded FastAPI, DESIGN §9.5) assembles as
    the ``/status`` JSON and whatever step 10 pushes as the portfolio
    snapshot to the Cloudflare Worker (DESIGN §9.1): call this once per
    snapshot/status build (with the just-computed ``broker_available_cash``)
    and, if it returns non-``None``, add the dict verbatim under a
    `"warnings"` list (or a single `"allocation_warning"` key -- whichever
    the step-8/9/10 payload shape settles on) so both the LAN debug view and
    the pushed snapshot (and therefore the Android status UI and the static
    web app that reads it) show the same message.
    """
    cache = cache or MinAllocationCache()
    result = cache.get(strategy, repo, symbols)
    if result is None:
        return None
    if broker_available_cash is None:
        return None

    reserve = strategy.cash_reserve_usd
    sleeve_cash = max(0.0, broker_available_cash - reserve)
    rungs = affordable_rungs(strategy, sleeve_cash)
    shortfall = result.design_rungs - rungs

    if sleeve_cash < result.minimum_sleeve_cash_usd:
        return {
            "level": "critical",
            "code": "sleeve_cash_cannot_trade",
            "tier": 1,
            "cash_reserve_usd": reserve,
            "broker_available_cash_usd": broker_available_cash,
            "sleeve_cash_usd": sleeve_cash,
            "threshold_usd": result.minimum_sleeve_cash_usd,
            "binding_symbol": result.binding_symbol,
            "affordable_rungs": rungs,
            "design_rungs": result.design_rungs,
            "rung_shortfall": shortfall,
            "message": (
                f"Tier 1 (cannot trade at all): with a ${reserve:,.2f} reserve "
                f"and ${broker_available_cash:,.2f} available, sleeve_cash is "
                f"${sleeve_cash:,.2f}, below the ${result.minimum_sleeve_cash_usd:,.2f} "
                f"needed to place a first order at all, bound by {result.binding_symbol}'s "
                f"minimum order size. Affords {rungs} of {result.design_rungs} ladder "
                "rungs. Lower strategy.cash_reserve_usd or add available cash. "
                "Trading is not blocked -- this is a warning, not an error."
            ),
        }

    if sleeve_cash < result.full_ladder_sleeve_cash_usd:
        return {
            "level": "warning",
            "code": "sleeve_cash_limits_ladder_depth",
            "tier": 2,
            "cash_reserve_usd": reserve,
            "broker_available_cash_usd": broker_available_cash,
            "sleeve_cash_usd": sleeve_cash,
            "threshold_usd": result.full_ladder_sleeve_cash_usd,
            "binding_symbol": result.binding_symbol,
            "affordable_rungs": rungs,
            "design_rungs": result.design_rungs,
            "rung_shortfall": shortfall,
            "message": (
                f"Tier 2 (ladder capped): with a ${reserve:,.2f} reserve and "
                f"${broker_available_cash:,.2f} available, sleeve_cash is "
                f"${sleeve_cash:,.2f}, below the ${result.full_ladder_sleeve_cash_usd:,.2f} "
                f"needed to reach full ladder depth. Affords {rungs} of "
                f"{result.design_rungs} ladder rungs (short by {shortfall}), bound by "
                f"{result.binding_symbol}'s minimum order size. Lower "
                "strategy.cash_reserve_usd or add available cash to deploy more of the "
                "strategy's design capacity. Trading is not blocked -- this is a "
                "warning, not an error."
            ),
        }

    return None
