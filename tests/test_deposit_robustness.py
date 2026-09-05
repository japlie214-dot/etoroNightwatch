"""Pins the intended behaviour of an unannounced cash deposit (or withdrawal)
under the exclusion sleeve model (DESIGN.md §4.4, §4.5; operator decision
2026-09-04, reserve-only, no ceiling -- "not an open question; do not
re-propose it").

Companion to `doc/AUDIT_deposit_robustness_2026-09-05.md`. That audit's
conclusion in one line: **gate-level and order-sizing code that exists today
(`strategy/min_allocation.py`, `config.py`) absorbs a cash jump in either
direction without raising or blocking** -- it only ever *warns*. The core-
floor question (DESIGN §4.5's `0.5 * sleeve_equity`) and most of the twelve
preflight gates are comment-only stubs (`engine/preflight.py`) and are
therefore NOT covered here -- see the audit doc's gate-by-gate table for the
forward-looking constraints that code must respect once built.

Every test in this file exercises code that exists today. Nothing here tests
against a stub.
"""

from __future__ import annotations

import dataclasses
import math

import pytest

from nightwatch.config import StrategyConfig, SymbolConfig
from nightwatch.db.repo import Instrument, Repo
from nightwatch.strategy.min_allocation import (
    MinAllocationCache,
    affordable_rungs,
    allocation_warning,
)

EXPECTED_MINIMUM = (50.0 + 8.0 * 50.0) / (1 - 0.4 * 0.10070574586703619)  # tier 1, live config
EXPECTED_FULL_LADDER = 50.0 * 60 / 0.4  # tier 2, live config


def _seed(repo: Repo, schg_min: float = 50.0, upro_min: float = 10.0) -> None:
    repo.upsert_instrument(
        Instrument(symbol="SCHG", instrument_id=1084, min_position_exposure=schg_min)
    )
    repo.upsert_instrument(
        Instrument(symbol="UPRO", instrument_id=3244, min_position_exposure=upro_min)
    )


# -- deposits: a cash jump flows through with nothing raising ---------------


def test_deposit_sequence_never_raises_and_sleeve_cash_tracks_it_exactly(repo: Repo) -> None:
    """The operator's actual question: 'I will deposit more cash without
    notice, I hope nightwatch won't be startled by it.' Simulate a deposit
    landing mid-session by replaying increasing `broker_available_cash`
    values against the same strategy config and cache, exactly as the
    session's status/snapshot layer would poll it. Nothing may raise, and
    sleeve_cash (surfaced in the warning payload) must equal
    broker_available_cash - cash_reserve_usd at every step, never anything
    else."""
    strategy = StrategyConfig(cash_reserve_usd=0.0)
    symbols = SymbolConfig()
    _seed(repo)
    cache = MinAllocationCache()

    # A deposit landing: available cash jumps step by step, unannounced.
    deposits = [1_000.0, 1_500.0, 5_000.0, 50_000.0, 500_000.0]
    previous_sleeve_cash = None
    for available in deposits:
        warning = allocation_warning(
            strategy, repo, symbols, broker_available_cash=available, cache=cache
        )  # must not raise
        observed_sleeve_cash = available  # reserve is 0.0
        if warning is not None:
            assert warning["sleeve_cash_usd"] == observed_sleeve_cash
        if previous_sleeve_cash is not None:
            assert observed_sleeve_cash > previous_sleeve_cash
        previous_sleeve_cash = observed_sleeve_cash


def test_deposit_moves_warning_from_critical_through_warning_to_none(repo: Repo) -> None:
    """A deposit crossing both thresholds in turn: tier-1 critical (cannot
    trade), then tier-2 warning (ladder capped), then no warning at all
    (full design capacity affordable) -- the exact three-stage absorption
    the design promises, with `affordable_rungs` climbing monotonically."""
    strategy = StrategyConfig(cash_reserve_usd=0.0)
    symbols = SymbolConfig()
    _seed(repo)
    cache = MinAllocationCache()

    before = allocation_warning(strategy, repo, symbols, broker_available_cash=100.0, cache=cache)
    assert before is not None and before["level"] == "critical"

    middle = allocation_warning(
        strategy, repo, symbols, broker_available_cash=2_000.0, cache=cache
    )
    assert middle is not None and middle["level"] == "warning"

    after = allocation_warning(
        strategy, repo, symbols, broker_available_cash=EXPECTED_FULL_LADDER, cache=cache
    )
    assert after is None

    rungs_before = affordable_rungs(strategy, 100.0)
    rungs_middle = affordable_rungs(strategy, 2_000.0)
    rungs_after = affordable_rungs(strategy, EXPECTED_FULL_LADDER)
    assert rungs_before <= rungs_middle <= rungs_after
    assert rungs_after == strategy.max_lots


def test_mid_run_deposit_absorbed_by_the_same_warm_cache_no_special_handling(
    repo: Repo,
) -> None:
    """DESIGN.md §10 acceptance criterion 8: 'a mid-run deposit is absorbed
    correctly with no special handling.' Pin what that means for the code
    that exists: the eligibility-derived thresholds (tier 1 / tier 2) are
    cached and keyed only on strategy config + per-instrument minimums
    (`_signature`) -- a deposit changes only `broker_available_cash`, which
    is deliberately excluded from that signature (module docstring). So the
    *same* cache object, never invalidated or rebuilt, must correctly
    reflect a deposit landing between two calls -- there is no 'deposit
    occurred, please recompute' hook anywhere, and there must not need to
    be one."""
    strategy = StrategyConfig(cash_reserve_usd=0.0)
    symbols = SymbolConfig()
    _seed(repo)
    cache = MinAllocationCache()

    before_result = cache.get(strategy, repo, symbols)
    before_warning = allocation_warning(
        strategy, repo, symbols, broker_available_cash=100.0, cache=cache
    )

    # The deposit happens here -- outside the app entirely, on eToro's side.
    after_result = cache.get(strategy, repo, symbols)
    after_warning = allocation_warning(
        strategy, repo, symbols, broker_available_cash=50_000.0, cache=cache
    )

    # Same cached thresholds object -- proof no special-cased recompute was
    # needed to notice the deposit.
    assert before_result is after_result
    assert before_warning is not None and before_warning["level"] == "critical"
    assert after_warning is None


# -- the reserve: withholding a deposit from the strategy --------------------


def test_reserve_withholds_a_deposit_that_lands_below_it(repo: Repo) -> None:
    """DESIGN.md §4.4: 'raise cash_reserve_usd by the deposited amount before
    the deposit lands ... to keep a deposit out of the strategy's reach.'
    A deposit that lands entirely within the reserve must not move
    sleeve_cash at all."""
    strategy = StrategyConfig(cash_reserve_usd=10_000.0)
    symbols = SymbolConfig()
    _seed(repo)

    before = allocation_warning(strategy, repo, symbols, broker_available_cash=1_000.0)
    # Deposit of $2,000 lands; reserve (10,000) already covers it entirely.
    after = allocation_warning(strategy, repo, symbols, broker_available_cash=3_000.0)

    assert before is not None and after is not None
    assert before["sleeve_cash_usd"] == 0.0
    assert after["sleeve_cash_usd"] == 0.0  # unchanged: reserve still exceeds available cash


def test_reserve_withholds_only_the_covered_part_of_a_deposit(repo: Repo) -> None:
    """A deposit that partially exceeds the reserve raises sleeve_cash by
    exactly the excess over the reserve, never by the full deposit amount --
    the exclusion-model arithmetic, `sleeve_cash = max(0, available - reserve)`,
    pinned against a concrete deposit scenario rather than only abstract
    numbers."""
    strategy = StrategyConfig(cash_reserve_usd=5_000.0)
    symbols = SymbolConfig()
    _seed(repo)

    before = allocation_warning(strategy, repo, symbols, broker_available_cash=5_000.0)
    assert before is not None
    assert before["sleeve_cash_usd"] == 0.0

    # A $3,000 deposit lands on top of the $5,000 already in the account.
    after = allocation_warning(strategy, repo, symbols, broker_available_cash=8_000.0)
    assert after is not None
    assert after["sleeve_cash_usd"] == 3_000.0  # only the excess over the reserve, not $8,000


# -- withdrawals: the mirror case --------------------------------------------


def test_withdrawal_to_near_zero_cash_degrades_to_a_warning_never_an_error(repo: Repo) -> None:
    """The mirror case, arguably the more dangerous direction (cash going
    DOWN unannounced): sleeve_cash falling to near zero must produce a
    graceful tier-1 warning with zero affordable rungs, never an exception,
    never a negative number."""
    strategy = StrategyConfig(cash_reserve_usd=0.0)
    symbols = SymbolConfig()
    _seed(repo)

    for available in (0.0, 0.01, 1.0, 10.0):
        warning = allocation_warning(
            strategy, repo, symbols, broker_available_cash=available
        )  # must not raise
        assert warning is not None
        assert warning["level"] == "critical"
        assert warning["sleeve_cash_usd"] == available
        assert warning["affordable_rungs"] == 0
        assert warning["sleeve_cash_usd"] >= 0.0


def test_withdrawal_below_the_reserve_clamps_sleeve_cash_to_zero_not_negative(
    repo: Repo,
) -> None:
    """A withdrawal that drops available cash below the standing reserve must
    clamp sleeve_cash to zero -- never go negative, never raise -- exactly
    the same clamp DESIGN.md §4.5 specifies for an oversized reserve, now
    exercised from the withdrawal side instead of the reserve-editing side."""
    strategy = StrategyConfig(cash_reserve_usd=2_000.0)
    symbols = SymbolConfig()
    _seed(repo)

    # Available cash was $2,500 (above the reserve); a withdrawal drops it
    # to $500, below the reserve.
    before = allocation_warning(strategy, repo, symbols, broker_available_cash=2_500.0)
    after = allocation_warning(strategy, repo, symbols, broker_available_cash=500.0)

    assert before is not None
    assert after is not None
    assert after["sleeve_cash_usd"] == 0.0
    assert after["level"] == "critical"


def test_affordable_rungs_never_raises_across_a_wide_sweep_including_negative_inputs() -> None:
    """`affordable_rungs` is the one function on this path that takes a bare
    float rather than going through the reserve clamp -- pin that it stays
    well-behaved (no exception, no negative rung count) across zero and
    even a hypothetically negative sleeve_cash, since a future caller must
    not be able to violate the never-raise contract by skipping the clamp
    upstream."""
    strategy = StrategyConfig()
    for sleeve_cash in (-100.0, -0.01, 0.0, 0.01, 100.0, 1_000_000.0):
        rungs = affordable_rungs(strategy, sleeve_cash)  # must not raise
        assert rungs >= 0
        assert rungs <= strategy.max_lots


# -- no upper bound: the operator's declined-ceiling decision, pinned -------


def test_no_sleeve_or_order_size_ceiling_field_exists() -> None:
    """DESIGN.md §4.4/§12: a `max_sleeve_cash_usd` ceiling was put to the
    operator on 2026-09-04 and explicitly declined -- 'not an open question;
    do not re-propose it.' Pin that decision structurally: no config field
    anywhere on `StrategyConfig` imposes an upper bound on sleeve cash,
    equity, or order size. If this test starts failing because such a field
    was added, that is a reopening of a closed question and needs an
    explicit fresh operator decision -- it is not a routine config change."""
    field_names = {f.name for f in dataclasses.fields(StrategyConfig)}
    ceiling_like = {
        name
        for name in field_names
        if "max_sleeve" in name or "sleeve_cap" in name or "ceiling" in name
    }
    assert ceiling_like == set()


def test_a_very_large_deposit_still_only_warns_never_blocks(repo: Repo) -> None:
    """There is no ceiling anywhere on this path (see the structural test
    above): a deposit two orders of magnitude past the full-ladder threshold
    must behave exactly like one that just clears it -- no warning, no
    exception, rungs pinned at the strategy's own design capacity
    (`max_lots`), never higher."""
    strategy = StrategyConfig(cash_reserve_usd=0.0)
    symbols = SymbolConfig()
    _seed(repo)

    huge = EXPECTED_FULL_LADDER * 1_000  # a $7.5M-scale deposit
    warning = allocation_warning(strategy, repo, symbols, broker_available_cash=huge)
    rungs = affordable_rungs(strategy, huge)

    assert warning is None
    assert rungs == strategy.max_lots  # capped by design (MAX_LOTS), not by any cash ceiling


# -- config-level: cash_reserve_usd itself never rejects a deposit-sized value --


@pytest.mark.parametrize("reserve", [0.0, 1.0, 10_000.0, 10_000_000.0])
def test_cash_reserve_usd_accepts_any_nonnegative_value_including_deposit_scale(
    reserve: float,
) -> None:
    """`StrategyConfig.__post_init__` only rejects a negative reserve
    (config.py). Any nonnegative figure, including one at the scale of a
    large real-world deposit, must construct without error -- an operator
    raising the reserve in response to an incoming deposit (DESIGN.md §4.4's
    documented mitigation) must never itself be blocked by validation."""
    strategy = StrategyConfig(cash_reserve_usd=reserve)
    assert strategy.cash_reserve_usd == reserve


def test_cash_reserve_usd_still_rejects_negative() -> None:
    """Unrelated to deposits directly, but worth pinning alongside the above
    so the one real validation rule on this field doesn't silently erode:
    negative is the only value that must still raise."""
    from nightwatch.config import ConfigError

    with pytest.raises(ConfigError):
        StrategyConfig(cash_reserve_usd=-1.0)
