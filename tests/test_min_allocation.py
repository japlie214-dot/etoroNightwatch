"""Minimum-viable sleeve cash: arithmetic, cache invalidation, and the
two-tier warning-never-block contract (DESIGN §4.3, §4.5, §4.6).

Hand-worked numbers against the live config values (core_fraction=0.4,
order_divisor=60, min_order_size=50.0, cash_target_multiplier=8.0,
max_lots=60, TIER_CAPS[0]=0.10070574586703619, SCHG min=50.0, UPRO min=10.0):

    A_upro = min_order_size / core_fraction = 50 / 0.4 = 125.0
    A_schg = (schg_min + cash_target_multiplier * min_order_size)
             / (1 - core_fraction * TIER_CAPS[0])
           = (50 + 8*50) / (1 - 0.4*0.10070574586703619)
           = 450 / 0.9597177020651821
           ~= 468.8878815352... (SCHG binds) -- tier 1

    A_full_ladder = min_order_size * order_divisor / core_fraction
                  = 50 * 60 / 0.4 = 7500.0                      -- tier 2
"""

from __future__ import annotations

import math

import pytest

from nightwatch.config import TIER_CAPS, StrategyConfig, SymbolConfig
from nightwatch.db.repo import Instrument, Repo
from nightwatch.strategy.min_allocation import (
    MinAllocationCache,
    affordable_rungs,
    allocation_warning,
    compute_minimum_allocation,
    reserve_requirement,
)

EXPECTED_MINIMUM = (50.0 + 8.0 * 50.0) / (1 - 0.4 * TIER_CAPS[0])
EXPECTED_FULL_LADDER = 50.0 * 60 / 0.4


def _strategy(**overrides) -> StrategyConfig:
    return StrategyConfig(**overrides)


def _instrument(symbol: str, instrument_id: int, min_exposure: float | None) -> Instrument:
    return Instrument(
        symbol=symbol, instrument_id=instrument_id, min_position_exposure=min_exposure
    )


def _seed(repo: Repo, schg_min: float = 50.0, upro_min: float = 10.0) -> None:
    repo.upsert_instrument(_instrument("SCHG", 1084, schg_min))
    repo.upsert_instrument(_instrument("UPRO", 3244, upro_min))


# -- arithmetic ------------------------------------------------------------


def test_matches_hand_worked_minimum() -> None:
    strategy = _strategy()
    schg = _instrument("SCHG", 1084, 50.0)
    upro = _instrument("UPRO", 3244, 10.0)

    result = compute_minimum_allocation(strategy, schg, upro)

    assert result is not None
    assert math.isclose(result.minimum_sleeve_cash_usd, EXPECTED_MINIMUM, rel_tol=1e-9)
    assert math.isclose(
        result.full_ladder_sleeve_cash_usd, EXPECTED_FULL_LADDER, rel_tol=1e-9
    )
    assert result.binding_symbol == "SCHG"
    assert result.schg_min_position_exposure == 50.0
    assert result.upro_min_position_exposure == 10.0
    assert result.design_rungs == 60


def test_disagrees_with_the_rough_ladder_capacity_estimate() -> None:
    """Tier 1 ("can it place a first order of each kind") and tier 2 ("can it
    reach full 60-lot depth") are deliberately different numbers -- the
    brief's ~$7,500 estimate is tier 2, not tier 1. See the module
    docstring."""
    strategy = _strategy()
    schg = _instrument("SCHG", 1084, 50.0)
    upro = _instrument("UPRO", 3244, 10.0)

    result = compute_minimum_allocation(strategy, schg, upro)

    assert result is not None
    assert result.minimum_sleeve_cash_usd < 1000.0
    assert result.full_ladder_sleeve_cash_usd > 5000.0
    assert result.minimum_sleeve_cash_usd < result.full_ladder_sleeve_cash_usd


def test_upro_binds_when_its_minimum_exceeds_the_order_size_floor() -> None:
    """If a future config ever let UPRO's own minimum exceed min_order_size,
    the UPRO branch must be the one that grows to compensate."""
    strategy = _strategy(min_order_size=5.0)
    schg = _instrument("SCHG", 1084, 5.0)
    upro = _instrument("UPRO", 3244, 100.0)

    result = compute_minimum_allocation(strategy, schg, upro)

    assert result is not None
    assert result.binding_symbol == "UPRO"
    # A >= upro_min * order_divisor / core_fraction in this branch.
    expected_upro = 100.0 * strategy.order_divisor / strategy.core_fraction
    assert math.isclose(result.minimum_sleeve_cash_usd, expected_upro, rel_tol=1e-9)


def test_no_eligibility_data_cached_yet_degrades_gracefully() -> None:
    """First run, nothing probed against the live endpoint: the calculator
    must return None, never crash and never invent a number."""
    strategy = _strategy()
    schg = _instrument("SCHG", 1084, None)
    upro = _instrument("UPRO", 3244, None)

    assert compute_minimum_allocation(strategy, schg, upro) is None

    schg_partial = _instrument("SCHG", 1084, 50.0)
    upro_missing = _instrument("UPRO", 3244, None)
    assert compute_minimum_allocation(strategy, schg_partial, upro_missing) is None


# -- affordable_rungs --------------------------------------------------------


def test_affordable_rungs_zero_when_sleeve_cash_is_zero() -> None:
    strategy = _strategy()
    assert affordable_rungs(strategy, 0.0) == 0


def test_affordable_rungs_cannot_trade_case() -> None:
    """Below tier 1, the sleeve can fund very few (here, zero) UPRO rungs at
    the floored order size."""
    strategy = _strategy()
    # $100 sleeve: budget = 0.4*100 = 40, order_size = max(40/60, 50) = 50,
    # floor(40/50) = 0 rungs.
    assert affordable_rungs(strategy, 100.0) == 0


def test_affordable_rungs_truncated_ladder_case() -> None:
    """Between tier 1 and tier 2: some rungs afforded, but short of design
    capacity -- the $2,000 example from the exclusion-model rationale."""
    strategy = _strategy()
    # budget = 0.4*2000 = 800, order_size = max(800/60, 50) = 50,
    # floor(800/50) = 16 of 60 design rungs.
    assert affordable_rungs(strategy, 2000.0) == 16


def test_affordable_rungs_full_ladder_case() -> None:
    """At or above tier 2's threshold, the full design capacity is
    affordable (capped at max_lots, never more)."""
    strategy = _strategy()
    assert affordable_rungs(strategy, EXPECTED_FULL_LADDER) == strategy.max_lots
    assert affordable_rungs(strategy, EXPECTED_FULL_LADDER * 10) == strategy.max_lots


# -- cache invalidation ------------------------------------------------------


def test_cache_returns_none_before_instruments_are_cached(repo: Repo) -> None:
    strategy = _strategy()
    symbols = SymbolConfig()
    cache = MinAllocationCache()

    assert cache.get(strategy, repo, symbols) is None


def test_cache_reuses_result_when_nothing_changed(repo: Repo) -> None:
    strategy = _strategy()
    symbols = SymbolConfig()
    _seed(repo)
    cache = MinAllocationCache()

    first = cache.get(strategy, repo, symbols)
    second = cache.get(strategy, repo, symbols)

    assert first is not None
    # Same object -- proof the second call did not recompute.
    assert first is second


def test_cache_invalidates_when_a_cached_instrument_minimum_changes(repo: Repo) -> None:
    """eToro can raise a minimum on its own schedule; a cached number that
    silently survives that would be worse than no cache at all."""
    strategy = _strategy()
    symbols = SymbolConfig()
    _seed(repo)
    cache = MinAllocationCache()

    before = cache.get(strategy, repo, symbols)
    assert before is not None

    repo.upsert_instrument(_instrument("SCHG", 1084, 200.0))
    after = cache.get(strategy, repo, symbols)

    assert after is not None
    assert after is not before
    assert after.schg_min_position_exposure == 200.0
    assert after.minimum_sleeve_cash_usd > before.minimum_sleeve_cash_usd


def test_cache_invalidates_when_config_changes(repo: Repo) -> None:
    """The operator can edit config.toml between runs; the cache must not
    hand back a number computed against the previous strategy config."""
    symbols = SymbolConfig()
    _seed(repo)
    cache = MinAllocationCache()

    before = cache.get(_strategy(min_order_size=50.0), repo, symbols)
    after = cache.get(_strategy(min_order_size=75.0), repo, symbols)

    assert before is not None and after is not None
    assert after is not before
    assert after.minimum_sleeve_cash_usd > before.minimum_sleeve_cash_usd


def test_cache_invalidates_when_max_lots_changes(repo: Repo) -> None:
    """design_rungs rides along in the cached result -- a config change to
    max_lots must not leave a stale design_rungs behind."""
    symbols = SymbolConfig()
    _seed(repo)
    cache = MinAllocationCache()

    before = cache.get(_strategy(max_lots=60), repo, symbols)
    after = cache.get(_strategy(max_lots=30), repo, symbols)

    assert before is not None and after is not None
    assert before.design_rungs == 60
    assert after.design_rungs == 30


# -- reserve_requirement (config-time statement) -----------------------------


def test_reserve_requirement_none_without_eligibility_data(repo: Repo) -> None:
    strategy = _strategy(cash_reserve_usd=500.0)
    symbols = SymbolConfig()

    assert reserve_requirement(strategy, repo, symbols) is None


def test_reserve_requirement_states_both_thresholds(repo: Repo) -> None:
    strategy = _strategy(cash_reserve_usd=500.0)
    symbols = SymbolConfig()
    _seed(repo)

    req = reserve_requirement(strategy, repo, symbols)

    assert req is not None
    assert req["cash_reserve_usd"] == 500.0
    assert math.isclose(
        req["required_available_cash_to_trade_usd"], 500.0 + EXPECTED_MINIMUM, rel_tol=1e-9
    )
    assert math.isclose(
        req["required_available_cash_for_full_ladder_usd"],
        500.0 + EXPECTED_FULL_LADDER,
        rel_tol=1e-9,
    )
    assert req["binding_symbol"] == "SCHG"
    assert req["design_rungs"] == 60


# -- the runtime warning, and the never-block contract -----------------------


def test_warning_absent_when_no_eligibility_data_cached_yet(repo: Repo) -> None:
    strategy = _strategy(cash_reserve_usd=0.0)
    symbols = SymbolConfig()

    assert allocation_warning(strategy, repo, symbols, broker_available_cash=10_000.0) is None


def test_warning_absent_when_available_cash_unknown(repo: Repo) -> None:
    strategy = _strategy(cash_reserve_usd=0.0)
    symbols = SymbolConfig()
    _seed(repo)

    assert allocation_warning(strategy, repo, symbols, broker_available_cash=None) is None


def test_warning_absent_when_reserve_zero_means_all_available_cash_and_it_is_plenty(
    repo: Repo,
) -> None:
    strategy = _strategy(cash_reserve_usd=0.0)
    symbols = SymbolConfig()
    _seed(repo)

    warning = allocation_warning(strategy, repo, symbols, broker_available_cash=10_000.0)

    assert warning is None


def test_tier1_fires_when_sleeve_cash_cannot_trade_at_all(repo: Repo) -> None:
    strategy = _strategy(cash_reserve_usd=0.0)
    symbols = SymbolConfig()
    _seed(repo)

    warning = allocation_warning(strategy, repo, symbols, broker_available_cash=100.0)

    assert warning is not None
    assert warning["level"] == "critical"
    assert warning["code"] == "sleeve_cash_cannot_trade"
    assert warning["tier"] == 1
    assert warning["sleeve_cash_usd"] == 100.0
    assert math.isclose(warning["threshold_usd"], EXPECTED_MINIMUM, rel_tol=1e-9)
    assert warning["binding_symbol"] == "SCHG"
    assert warning["affordable_rungs"] == 0
    assert warning["design_rungs"] == 60
    assert warning["rung_shortfall"] == 60
    assert "not blocked" in warning["message"].lower()
    assert "tier 1" in warning["message"].lower()


def test_tier2_fires_when_ladder_is_truncated_but_trading_is_possible(repo: Repo) -> None:
    strategy = _strategy(cash_reserve_usd=0.0)
    symbols = SymbolConfig()
    _seed(repo)

    warning = allocation_warning(strategy, repo, symbols, broker_available_cash=2000.0)

    assert warning is not None
    assert warning["level"] == "warning"
    assert warning["code"] == "sleeve_cash_limits_ladder_depth"
    assert warning["tier"] == 2
    assert warning["sleeve_cash_usd"] == 2000.0
    assert math.isclose(warning["threshold_usd"], EXPECTED_FULL_LADDER, rel_tol=1e-9)
    assert warning["affordable_rungs"] == 16
    assert warning["design_rungs"] == 60
    assert warning["rung_shortfall"] == 44
    assert "not blocked" in warning["message"].lower()
    assert "tier 2" in warning["message"].lower()


def test_no_warning_when_sleeve_cash_reaches_full_ladder_depth(repo: Repo) -> None:
    strategy = _strategy(cash_reserve_usd=0.0)
    symbols = SymbolConfig()
    _seed(repo)

    warning = allocation_warning(
        strategy, repo, symbols, broker_available_cash=EXPECTED_FULL_LADDER
    )

    assert warning is None


def test_tier1_boundary_exactly_at_threshold_does_not_fire(repo: Repo) -> None:
    strategy = _strategy(cash_reserve_usd=0.0)
    symbols = SymbolConfig()
    _seed(repo)

    warning = allocation_warning(
        strategy, repo, symbols, broker_available_cash=EXPECTED_MINIMUM
    )

    # At exactly the tier-1 threshold, tier 1 must not fire; tier 2 may
    # (sleeve is nowhere near full-ladder depth yet).
    assert warning is not None
    assert warning["tier"] == 2


def test_reserve_reduces_sleeve_cash(repo: Repo) -> None:
    """A nonzero reserve is subtracted from available cash before either
    threshold is checked -- the core exclusion-model arithmetic."""
    strategy = _strategy(cash_reserve_usd=1800.0)
    symbols = SymbolConfig()
    _seed(repo)

    # available - reserve = 2000 - 1800 = 200 -> tier 1 fires (< 468.89).
    warning = allocation_warning(strategy, repo, symbols, broker_available_cash=2000.0)

    assert warning is not None
    assert warning["tier"] == 1
    assert warning["sleeve_cash_usd"] == 200.0


def test_reserve_larger_than_available_cash_clamps_to_zero_never_negative(repo: Repo) -> None:
    """The zero-clamp: a reserve bigger than what's actually available means
    the app has nothing to trade -- a tier-1 warning, never a crash and
    never a negative sleeve."""
    strategy = _strategy(cash_reserve_usd=5000.0)
    symbols = SymbolConfig()
    _seed(repo)

    warning = allocation_warning(strategy, repo, symbols, broker_available_cash=1000.0)

    assert warning is not None
    assert warning["sleeve_cash_usd"] == 0.0
    assert warning["tier"] == 1
    assert warning["affordable_rungs"] == 0


@pytest.mark.parametrize(
    "reserve,available",
    [
        (0.0, 0.0),
        (0.0, 1.0),
        (0.0, 100.0),
        (0.0, 468.0),
        (0.0, 2000.0),
        (1800.0, 2000.0),
        (5000.0, 1000.0),
        (100.0, 50.0),
        (0.0, EXPECTED_MINIMUM),
        (0.0, EXPECTED_FULL_LADDER),
        (0.0, EXPECTED_FULL_LADDER * 5),
    ],
)
def test_neither_tier_ever_raises_or_blocks(
    repo: Repo, reserve: float, available: float
) -> None:
    """The core contract: however the reserve and available cash combine,
    this must either return None or a warning dict -- never an exception,
    never anything resembling a gate failure."""
    strategy = _strategy(cash_reserve_usd=reserve)
    symbols = SymbolConfig()
    _seed(repo)

    warning = allocation_warning(
        strategy, repo, symbols, broker_available_cash=available
    )  # must not raise

    assert warning is None or warning["level"] in ("critical", "warning")
