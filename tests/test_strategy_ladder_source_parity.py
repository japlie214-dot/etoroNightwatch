"""Tests the ported ``nightwatch.strategy.ladder`` against the REAL,
unmodified ``etoro_trader`` source strategy -- a real external oracle, not a
self-referential fixture (board nightwatch/handoff-2026-09-05-evening.md §1: "a green suite
is evidence about your fixtures, not about reality").

Two kinds of check:

1. Numeric parity that MUST agree (tier caps/boundaries) -- imported
   directly from the real ``etoro_trader`` source, not retyped by hand.
2. A synthetic price/cone_z scenario driven through BOTH the real,
   unmodified source harness (via a minimal synchronous fill simulator,
   below) and the ported ``ladder.decide`` with equivalent inputs --
   demonstrating that the source's documented bug #1 (deferred re-entry
   silently disabled) is real, reproducible, unmodified source behaviour,
   and that this port's fix actually creates and fills a bucket where the
   source does not.

Requires ``etoro_trader`` checked out as a sibling of this repo
(``../etoro_trader`` relative to the nightwatch repo root, matching this
task's actual environment) -- skipped if not present, so this file does not
break in an environment that only has ``etoroNightwatch`` checked out.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import ModuleType

import pytest

_ETORO_TRADER = Path(__file__).resolve().parents[2] / "etoro_trader"

if not _ETORO_TRADER.is_dir():
    pytest.skip(f"etoro_trader not found at {_ETORO_TRADER}", allow_module_level=True)

# Load the unmodified pure modules without package initializers: common's
# initializer imports common.config, which reads the sibling's real .env.
_SOURCE_ROOTS = ("common", "harness", "indicators")
_saved_modules = {
    name: module for name, module in sys.modules.copy().items()
    if name.split(".")[0] in _SOURCE_ROOTS
}
try:
    for name in _saved_modules:
        del sys.modules[name]
    for name in (*_SOURCE_ROOTS, "harness.strategies", "indicators.volatility"):
        package = ModuleType(name)
        package.__path__ = [str(_ETORO_TRADER.joinpath(*name.split(".")))]
        sys.modules[name] = package

    from common.contracts.account import AccountState
    from common.contracts.decision import Hold, OrderIntents
    from common.contracts.etoro_schema import EtoroAction, EtoroPosition
    from harness.context import HarnessContext
    from harness.strategies.std_band_ladder_cash_recycle import (
        StdBandLadderCashRecycleConfig, StdBandLadderCashRecycleHarness,
    )
    from harness.strategies.std_band_ladder_martingale import (
        MARTINGALE_RATIO, _N_TIERS, _TIER_BOUNDARIES, _martingale_tier_caps,
    )
    from indicators.base import IndicatorSnapshot
finally:
    for name in tuple(sys.modules):
        if name.split(".")[0] in _SOURCE_ROOTS:
            del sys.modules[name]
    sys.modules.update(_saved_modules)

from nightwatch.config import TIER_CAPS, TIER_DRAWDOWN_BOUNDS  # noqa: E402
from nightwatch.strategy import ladder  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Tier caps/boundaries: numeric parity against the real source constants.
# ---------------------------------------------------------------------------


def test_tier_caps_match_source_martingale_derivation():
    """DESIGN.md §1.4's hardcoded TIER_CAPS must equal the source's own
    ``_martingale_tier_caps(MARTINGALE_RATIO, 6)`` -- imported and computed
    from the real ``std_band_ladder_martingale`` module, not retyped.
    """
    source_caps = _martingale_tier_caps(MARTINGALE_RATIO, _N_TIERS)
    assert len(source_caps) == len(TIER_CAPS)
    for expected, actual in zip(source_caps, TIER_CAPS):
        assert expected == pytest.approx(actual, rel=1e-12)
    assert tuple(_TIER_BOUNDARIES) == tuple(TIER_DRAWDOWN_BOUNDS)


# ---------------------------------------------------------------------------
# 2. A minimal synchronous driver for the REAL source harness.
# ---------------------------------------------------------------------------


class _SourceDriver:
    """Fills OrderIntents synchronously against a hand-fed price series --
    a minimal stand-in for the etoro_trader backtest's execution layer, only
    as much as this specific scenario needs. Real source code
    (``StdBandLadderCashRecycleHarness``) drives every decision; this class
    only applies the resulting intents to cash/positions.
    """

    def __init__(self, harness: StdBandLadderCashRecycleHarness, cash: float):
        self.harness = harness
        self.cash = cash
        self.positions: dict[int, EtoroPosition] = {}
        self._next_id = 1

    def _equity(self, prices: dict[str, float]) -> float:
        return self.cash + sum(
            p.units * prices[p.symbol] for p in self.positions.values() if p.isOpen
        )

    def run_bar(self, *, timestamp, prices: dict[str, float], cone_z: float | None):
        account = AccountState(
            timestamp=timestamp, equity=self._equity(prices), cash=self.cash,
            positions=tuple(p for p in self.positions.values() if p.isOpen),
        )
        indicators = IndicatorSnapshot(
            timestamp=timestamp,
            values={"cone": {f"SPY_cone_{self.harness.cfg.cone_horizon}_z": cone_z}} if cone_z is not None else {},
        )
        ctx = HarnessContext(
            timestamp=timestamp, account=account, indicators=indicators,
            market_data={"prices": prices},
        )
        decision = self.harness.decide(ctx)
        if isinstance(decision, OrderIntents):
            for intent in decision.intents:
                self._apply(intent, prices)
        return decision

    def _apply(self, intent, prices: dict[str, float]) -> None:
        price = prices[intent.symbol]
        if intent.action == EtoroAction.OPEN:
            amount = intent.amount
            units = intent.units if intent.units is not None else amount / price
            pos_id = self._next_id
            self._next_id += 1
            self.positions[pos_id] = EtoroPosition(
                positionID=pos_id, instrumentId=0, symbol=intent.symbol, isBuy=True,
                openDateTime="", openRate=price, units=units, initialUnits=units,
                amount=amount, leverage=intent.leverage, isOpen=True,
            )
            self.cash -= amount
            # Let the harness discover its own position_id on the next bar's
            # _reconcile_lots (matches how the real backtest runner works).
        else:
            pos = self.positions[intent.position_id]
            proceeds = pos.units * price
            self.cash += proceeds
            self.positions[intent.position_id] = pos.__class__(
                **{**pos.__dict__, "isOpen": False},
            )


def _make_source_harness(**overrides) -> StdBandLadderCashRecycleHarness:
    cfg = StdBandLadderCashRecycleConfig(
        warmup_bars=0, core_fraction=0.4, order_divisor=20.0, min_order_size=1.0,
        buy_threshold=-2.0, sell_threshold=3.0, max_lots=60, reentry_tranches=3,
        bucket_expiry_bars=260, **overrides,
    )
    return StdBandLadderCashRecycleHarness(cfg)


def test_source_reproduces_documented_bug_no_reentry_bucket_ever_created():
    """board nightwatch/handoff-2026-09-05-evening.md §1's bug #1, reproduced against the
    REAL source code: a lot sold profitably after SPY has genuinely made a
    new ATH since the lot was bought should classify as a new-ATH sale and
    create a deferred re-entry bucket. It never does, because the winning
    (second) definition of ``_buy_if_allowed`` never stamps
    ``entry_spy_ath``, so ``lot.entry_spy_ath`` is always ``None`` and the
    ``new_ath_sale`` test always compares ``ath_snapshot < ath_snapshot``.
    """
    import pandas as pd

    harness = _make_source_harness()
    driver = _SourceDriver(harness, cash=40_000.0)
    # Seed an already-established SCHG core position -- the core-floor gate
    # (G-C) requires schg_value >= core_floor_pct * equity before the ladder
    # may deploy anything at all, mirroring nightwatch's real adopted SCHG
    # book (DESIGN_market_data.md §4.7), not a cold-start-from-zero account.
    driver.positions[0] = EtoroPosition(
        positionID=0, instrumentId=0, symbol="SCHG", isBuy=True, openDateTime="",
        openRate=50.0, units=1200.0, initialUnits=1200.0, amount=60_000.0,
        leverage=1, isOpen=True,
    )
    driver._next_id = 1

    # Bar 1: SPY=100, deep buy signal -> opens a catapult UPRO lot.
    driver.run_bar(
        timestamp=pd.Timestamp("2020-01-02"),
        prices={"SPY": 100.0, "UPRO": 10.0, "SCHG": 50.0},
        cone_z=-5.0,
    )
    assert len(harness.open_lots) == 1, "source strategy should have opened one lot"

    # Bar 2: SPY makes a genuine new ATH (100 -> 130); UPRO rallies so the
    # lot is profitable; sell signal fires.
    decision = driver.run_bar(
        timestamp=pd.Timestamp("2020-01-03"),
        prices={"SPY": 130.0, "UPRO": 16.0, "SCHG": 50.0},
        cone_z=5.0,
    )
    assert isinstance(decision, (OrderIntents, Hold))
    assert harness._buckets == [], (
        "source strategy never creates a re-entry bucket -- this is the "
        "documented bug, reproduced here against the real, unmodified source"
    )


def test_port_fixes_reentry_bucket_creation_and_fill():
    """Same scenario, run through the ported ``ladder.decide`` -- unlike the
    real source above, a bucket IS created after the new-ATH sale, and a
    tranche fills on the very next bar's qualifying lower close.
    """
    config = ladder.LadderConfig(
        core_fraction=0.4, order_divisor=20.0, min_order_size=1.0,
        buy_threshold=-2.0, sell_threshold=3.0, max_lots=60,
        reentry_tranches=3, bucket_expiry_bars=260,
    )
    state = ladder.LadderState()

    # Bar 1: SPY=100, deep buy signal. An already-established $60k SCHG core
    # position clears the core-floor gate (G-C), mirroring nightwatch's real
    # adopted book rather than a cold-start-from-zero account.
    inputs1 = ladder.LadderInputs(
        trading_date="2020-01-02", year=2020,
        spy=ladder.SpyContext(ath=100.0, close=100.0, drawdown=0.0, tier=0),
        cone_z=-5.0, upro_price=10.0, schg_price=50.0,
        cash=40_000.0, equity=100_000.0, schg_position_value=60_000.0,
        open_lots=(), buckets=(),
    )
    decision1 = ladder.decide(inputs1, state, config)
    assert len(decision1.new_lots) == 1
    lot = decision1.new_lots[0]
    assert lot.entry_spy_ath == 100.0
    # Assign a position_ref as the caller would once the fill is confirmed.
    open_lot = ladder.LadderLot.from_dict({**lot.to_dict(), "position_ref": "pos-1"})
    state = decision1.new_state

    # Bar 2: SPY makes a new ATH (130); UPRO rallies to profitability; sell
    # signal fires -- must classify as a new-ATH sale and create a bucket.
    inputs2 = ladder.LadderInputs(
        trading_date="2020-01-03", year=2020,
        spy=ladder.SpyContext(ath=130.0, close=130.0, drawdown=0.0, tier=0),
        cone_z=5.0, upro_price=16.0, schg_price=50.0,
        cash=40_000.0 - lot.cost_basis, equity=100_060.0, schg_position_value=60_000.0,
        open_lots=(open_lot,), buckets=(),
    )
    decision2 = ladder.decide(inputs2, state, config)
    assert len(decision2.closed_lots) == 1
    assert decision2.updated_buckets, "port must create a bucket after a new-ATH sale"
    bucket = decision2.updated_buckets[0]
    assert bucket.tranches_bought == 0
    assert bucket.proceeds == pytest.approx(16.0 * lot.units, rel=1e-9)
    state = decision2.new_state

    # Bar 3: SPY dips below the bucket's reference close -- tranche 1 fires.
    inputs3 = ladder.LadderInputs(
        trading_date="2020-01-06", year=2020,
        spy=ladder.SpyContext(ath=130.0, close=125.0, drawdown=125.0 / 130.0 - 1.0, tier=0),
        cone_z=0.0, upro_price=15.0, schg_price=50.0,
        cash=(40_000.0 - lot.cost_basis) + bucket.proceeds, equity=100_060.0,
        schg_position_value=60_000.0, open_lots=(), buckets=(bucket,),
    )
    decision3 = ladder.decide(inputs3, state, config)
    assert any(intent.symbol == "UPRO" and intent.side == "open" and (intent.reason or "").startswith("deferred reentry")
               for intent in decision3.intents), "tranche 1 should fire on a qualifying lower close"
    assert decision3.updated_buckets and decision3.updated_buckets[0].tranches_bought == 1
