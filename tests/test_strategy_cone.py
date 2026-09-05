"""Tests for nightwatch.strategy.cone (DESIGN.md §1.4, DESIGN_market_data.md
§12 build step 4).

Entirely offline -- pure arithmetic over hand-built lists, no DB, no
network, no fixture derived from any real SPY series (that would make the
expected value harder to hand-verify, not easier).
"""

from __future__ import annotations

import statistics

import pytest

from nightwatch.strategy.cone import (
    CONE_LOOKBACK_BARS,
    ConeSignalError,
    REQUIRED_HISTORY_BARS,
    cone_5_z,
)


def test_required_history_bars_is_lookback_plus_default_horizon():
    assert REQUIRED_HISTORY_BARS == CONE_LOOKBACK_BARS + 5


def test_returns_none_below_required_history():
    # 264 bars, one short of REQUIRED_HISTORY_BARS (265) for horizon=5.
    closes = [100.0] * (REQUIRED_HISTORY_BARS - 1)
    assert cone_5_z(closes, horizon=5) is None


def test_returns_none_for_empty_or_tiny_input():
    assert cone_5_z([], horizon=5) is None
    assert cone_5_z([100.0, 101.0], horizon=5) is None


def test_exact_value_hand_verified():
    # 265 flat closes at 100.0, then one final bar at 105.0 (n=266).
    # For horizon=5 the 260-return lookback window is i = n-260 .. n-1.
    # Every return is 0.0 (100/100 - 1) except the very last, which is
    # closes[-1]/closes[-6] - 1 = 105/100 - 1 = 0.05.
    closes = [100.0] * 265 + [105.0]
    assert len(closes) == 266

    # Independently re-derive the expected returns window the same way
    # cone_5_z's own docstring specifies (i = t-259..t, t = n-1), written
    # here from scratch rather than imported, so this test actually
    # exercises cone_5_z's indexing rather than restating it.
    n = len(closes)
    expected_returns = [
        closes[i] / closes[i - 5] - 1.0 for i in range(n - 260, n)
    ]
    expected_stdev = statistics.pstdev(expected_returns)
    expected_z = expected_returns[-1] / expected_stdev

    assert cone_5_z(closes, horizon=5) == pytest.approx(expected_z)
    # Sanity on the hand-derivation itself: 259 zero returns and one 0.05.
    assert expected_returns.count(0.0) == 259
    assert expected_returns[-1] == pytest.approx(0.05)


def test_negative_move_gives_negative_z():
    # A drop should read as a negative z -- the sign the -2 buy threshold
    # (config.StrategyConfig.buy_threshold) depends on.
    closes = [100.0] * 265 + [90.0]
    z = cone_5_z(closes, horizon=5)
    assert z is not None
    assert z < 0


def test_horizon_must_be_positive():
    with pytest.raises(ConeSignalError):
        cone_5_z([100.0] * 300, horizon=0)
    with pytest.raises(ConeSignalError):
        cone_5_z([100.0] * 300, horizon=-1)


def test_raises_on_nonpositive_close_rather_than_dividing_by_it():
    closes = [100.0] * 265 + [105.0]
    closes[10] = 0.0  # falls inside the 260-bar lookback window
    with pytest.raises(ConeSignalError):
        cone_5_z(closes, horizon=5)


def test_raises_on_non_finite_close():
    closes = [100.0] * 265 + [105.0]
    closes[200] = float("nan")
    with pytest.raises(ConeSignalError):
        cone_5_z(closes, horizon=5)


def test_raises_on_zero_variance_window_rather_than_dividing_by_zero():
    # Every close identical -> every return is 0.0 -> population stdev is
    # 0.0 -- a real division-by-zero the function must catch, not propagate
    # as a fabricated inf/nan z-score.
    closes = [100.0] * 266
    with pytest.raises(ConeSignalError):
        cone_5_z(closes, horizon=5)


def test_default_horizon_is_five():
    closes = [100.0] * 265 + [105.0]
    assert cone_5_z(closes) == cone_5_z(closes, horizon=5)
