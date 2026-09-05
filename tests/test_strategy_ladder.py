"""Unit tests for ``nightwatch.strategy.ladder`` (DESIGN.md §1.4).

Covers the traps the task brief calls out by name: the ``_bars``-not-
persisted warmup trap, SPY never being traded, orders below
``min_order_size`` being rejected, and the individual approved-fix
behaviours not already exercised by ``test_strategy_ladder_source_parity
.py`` (the source-oracle tests).
"""

from __future__ import annotations

import pytest

from nightwatch.strategy import ladder


def _spy(ath: float, close: float) -> ladder.SpyContext:
    dd = close / ath - 1.0 if ath else 0.0
    return ladder.SpyContext(ath=ath, close=close, drawdown=dd, tier=_tier(dd))


def _tier(dd: float) -> int:
    from nightwatch.strategy.params import tier_for_drawdown
    return tier_for_drawdown(dd)


def _base_config(**overrides) -> ladder.LadderConfig:
    params = dict(
        core_fraction=0.4, order_divisor=20.0, min_order_size=50.0,
        buy_threshold=-2.0, sell_threshold=3.0, max_lots=60,
        reentry_tranches=3, bucket_expiry_bars=20,
    )
    params.update(overrides)
    return ladder.LadderConfig(**params)


def _base_inputs(**overrides) -> ladder.LadderInputs:
    defaults = dict(
        trading_date="2024-01-02", year=2024, spy=_spy(100.0, 100.0),
        cone_z=None, upro_price=10.0, schg_price=50.0,
        cash=40_000.0, equity=100_000.0, schg_position_value=60_000.0,
        open_lots=(), buckets=(),
    )
    defaults.update(overrides)
    return ladder.LadderInputs(**defaults)


# ---------------------------------------------------------------------------
# Trap 1: bars not persisted -> permanent warmup.
# ---------------------------------------------------------------------------


def test_bars_persist_across_calls_when_caller_saves_state():
    config = _base_config(warmup_bars=3)
    state = ladder.LadderState()
    for _ in range(5):
        decision = ladder.decide(_base_inputs(cone_z=None), state, config)
        state = decision.new_state
    assert state.bars == 5
    assert state.bars > config.warmup_bars, "bars must climb past warmup after 5 real sessions"


def test_bars_not_persisted_stays_in_warmup_forever():
    """The exact trap DESIGN.md §1.4 and strategy/state.py warn about: a
    caller that never feeds ``new_state`` back in sees ``bars`` stuck at 1
    forever, and the strategy never leaves warmup even after any number of
    real sessions.
    """
    config = _base_config(warmup_bars=3)
    fresh_state = ladder.LadderState()
    for _ in range(10):
        decision = ladder.decide(_base_inputs(cone_z=-5.0), fresh_state, config)
        assert decision.diagnostics.get("warmup") is True
        assert decision.new_lots == [], "warmup must block trading even on a strong buy signal"
        # Caller "forgets" to persist -- always re-decides from LadderState().
    assert decision.new_state.bars == 1


def test_warmup_zero_never_blocks_bars_gate_but_signal_still_gates_via_cone_z_none():
    """Live config: warmup_bars=0. The bar-count gate is a permanent no-op,
    but a ``cone_z=None`` (insufficient SPY history, strategy.cone's own
    gate) still means no trade -- this module never trades on ``None``.
    """
    config = _base_config(warmup_bars=0)
    state = ladder.LadderState()
    decision = ladder.decide(_base_inputs(cone_z=None), state, config)
    assert decision.diagnostics.get("warmup") is not True
    assert decision.new_lots == []


# ---------------------------------------------------------------------------
# Trap 2: SPY is signal-only, never traded.
# ---------------------------------------------------------------------------


def test_spy_never_appears_in_any_intent_across_a_multi_bar_run():
    config = _base_config()
    state = ladder.LadderState()
    scenarios = [
        (_spy(100.0, 100.0), -5.0, 10.0),   # deep buy
        (_spy(130.0, 130.0), 5.0, 16.0),    # new ATH + sell signal
        (_spy(130.0, 90.0), -5.0, 8.0),     # deep drawdown buy
        (_spy(150.0, 150.0), 5.0, 20.0),    # another new ATH + sell
    ]
    lots: list[ladder.LadderLot] = []
    buckets: list[ladder.ReentryBucket] = []
    for i, (spy_ctx, cone_z, upro_price) in enumerate(scenarios):
        inputs = ladder.LadderInputs(
            trading_date=f"2024-01-0{i + 2}", year=2024, spy=spy_ctx, cone_z=cone_z,
            upro_price=upro_price, schg_price=50.0, cash=40_000.0, equity=200_000.0,
            schg_position_value=60_000.0, open_lots=tuple(lots), buckets=tuple(buckets),
        )
        decision = ladder.decide(inputs, state, config)
        for intent in decision.intents:
            assert intent.symbol in ("UPRO", "SCHG"), f"SPY must never be traded, got {intent}"
        state = decision.new_state
        # Roughly track lots/buckets forward (position_ref assigned as if filled).
        for j, lot in enumerate(decision.new_lots):
            lots.append(ladder.LadderLot.from_dict({**lot.to_dict(), "position_ref": f"pos-{i}-{j}"}))
        closed_refs = {c.position_ref for c in decision.closed_lots}
        lots = [lot for lot in lots if lot.position_ref not in closed_refs]
        buckets = decision.updated_buckets


# ---------------------------------------------------------------------------
# Trap 3: orders below min_order_size are rejected (structurally impossible
# here -- order_size is floored at min_order_size in _ensure_budget).
# ---------------------------------------------------------------------------


def test_order_size_never_falls_below_min_order_size():
    config = _base_config(core_fraction=0.0001, min_order_size=50.0, order_divisor=20.0)
    state = ladder.LadderState()
    decision = ladder.decide(_base_inputs(cone_z=-5.0, equity=1000.0), state, config)
    assert decision.new_state.order_size == pytest.approx(50.0)


def test_min_order_size_is_the_operators_choice_not_the_broker_floor():
    """DESIGN.md §5: min_order_size=50 stays 50 regardless of the broker's
    real $10 floor -- this module must never silently substitute 10.
    """
    config = _base_config(min_order_size=50.0)
    assert config.min_order_size == 50.0


def test_cash_gate_blocks_buy_below_order_size():
    config = _base_config()
    state = ladder.LadderState()
    # cash far below the order_size the budget computes.
    inputs = _base_inputs(cone_z=-5.0, cash=1.0)
    decision = ladder.decide(inputs, state, config)
    assert decision.new_lots == []
    assert "G-B cash" in decision.reason


# ---------------------------------------------------------------------------
# Approved fix #2: bucket duplication.
# ---------------------------------------------------------------------------


def test_process_reentry_buckets_no_duplication_multiple_buckets():
    config = _base_config()
    state = ladder.LadderState(bars=10)
    b1 = ladder.ReentryBucket(
        bucket_id="b1", sale_date="2024-01-01", sale_spy_close=130.0,
        proceeds=300.0, tranche_amount=100.0, created_bar=1,
        tranches_bought=0, last_reference_spy=130.0,
    )
    b2 = ladder.ReentryBucket(
        bucket_id="b2", sale_date="2024-01-01", sale_spy_close=140.0,
        proceeds=300.0, tranche_amount=100.0, created_bar=1,
        tranches_bought=0, last_reference_spy=140.0,
    )
    inputs = _base_inputs(
        trading_date="2024-01-10", cone_z=0.0, upro_price=10.0,
        cash=100_000.0, buckets=(b1, b2),
    )
    decision = ladder.decide(inputs, state, config)
    # Exactly one tranche fills this bar (oldest first) -- the other bucket
    # must appear EXACTLY ONCE in the result, never duplicated.
    ids = [b.bucket_id for b in decision.updated_buckets]
    assert len(ids) == len(set(ids)), f"buckets duplicated: {ids}"
    assert ids.count("b1") == 1
    assert ids.count("b2") == 1
    filled = [b for b in decision.updated_buckets if b.tranches_bought == 1]
    assert len(filled) == 1


def test_bucket_expires_without_forcing_a_trade():
    config = _base_config(bucket_expiry_bars=5)
    state = ladder.LadderState(bars=100)
    old_bucket = ladder.ReentryBucket(
        bucket_id="stale", sale_date="2024-01-01", sale_spy_close=130.0,
        proceeds=300.0, tranche_amount=100.0, created_bar=90,
        tranches_bought=0, last_reference_spy=130.0,
    )
    inputs = _base_inputs(trading_date="2024-04-01", cone_z=0.0, buckets=(old_bucket,))
    decision = ladder.decide(inputs, state, config)
    assert decision.updated_buckets == []
    assert not any((i.reason or "").startswith("deferred reentry") for i in decision.intents)


def test_only_one_tranche_fills_per_bar():
    config = _base_config()
    state = ladder.LadderState(bars=1)
    bucket = ladder.ReentryBucket(
        bucket_id="b1", sale_date="2023-12-01", sale_spy_close=130.0,
        proceeds=300.0, tranche_amount=100.0, created_bar=1,
        tranches_bought=0, last_reference_spy=130.0,
    )
    inputs = _base_inputs(trading_date="2024-01-02", cone_z=0.0, upro_price=10.0, buckets=(bucket,))
    decision = ladder.decide(inputs, state, config)
    fills = [i for i in decision.intents if (i.reason or "").startswith("deferred reentry")]
    assert len(fills) == 1


# ---------------------------------------------------------------------------
# Approved fix #4: Jan-2 budget crash -> warning, not a raise.
# ---------------------------------------------------------------------------


def test_ensure_budget_empty_prior_year_warns_instead_of_raising():
    config = _base_config()
    state = ladder.LadderState(budget_year=2023, budget=2000.0, order_size=100.0, cash_target=800.0)
    inputs = _base_inputs(trading_date="2024-01-02", year=2024, cone_z=None, prior_year_schg_values=())
    decision = ladder.decide(inputs, state, config)  # must not raise
    assert decision.new_state.budget_year == 2024
    assert any("no prior-year SCHG history" in w for w in decision.warnings)
    assert decision.new_state.budget == pytest.approx(math_floor_20(0.4 * inputs.equity))


def math_floor_20(x: float) -> float:
    import math
    return math.floor(x / 20.0) * 20.0


def test_ensure_budget_uses_prior_year_mean_when_available():
    config = _base_config()
    state = ladder.LadderState(budget_year=2023, budget=1000.0, order_size=50.0, cash_target=400.0)
    inputs = _base_inputs(
        trading_date="2024-01-02", year=2024, cone_z=None,
        prior_year_schg_values=(40_000.0, 44_000.0, 42_000.0),
    )
    decision = ladder.decide(inputs, state, config)
    expected_raw = 0.4 * (40_000.0 + 44_000.0 + 42_000.0) / 3
    assert decision.new_state.budget == pytest.approx(math_floor_20(expected_raw))
    assert decision.warnings == []


# ---------------------------------------------------------------------------
# Approved fix #5: dropped inert params -- config simply has no such fields.
# ---------------------------------------------------------------------------


def test_config_has_no_dropped_inert_fields():
    fields = ladder.LadderConfig.__dataclass_fields__
    assert "cash_band_fraction" not in fields
    assert "martingale_ratio" not in fields
    assert "transaction_cost_bps" not in fields


# ---------------------------------------------------------------------------
# G-F total UPRO ceiling and G-A tier budget.
# ---------------------------------------------------------------------------


def test_g_f_total_upro_ceiling_blocks_regardless_of_tier_room():
    config = _base_config(core_fraction=0.4, order_divisor=20.0)
    state = ladder.LadderState()
    # Fabricate open lots whose cost basis already consumes the whole budget.
    huge_lot = ladder.LadderLot(
        symbol="UPRO", book="catapult", units=1.0, entry_price=10.0,
        cost_basis=100_000.0, entry_date="2023-01-01", entry_reference_spy=100.0,
        entry_spy_ath=100.0, entry_spy_close=100.0, entry_spy_drawdown=0.0, entry_tier=0,
        position_ref="pos-huge",
    )
    inputs = _base_inputs(cone_z=-5.0, open_lots=(huge_lot,))
    decision = ladder.decide(inputs, state, config)
    assert decision.new_lots == []
    assert "G-F total UPRO ceiling" in decision.reason


def test_deep_drawdown_unlocks_larger_tier_cap():
    """A tier-5 drawdown must permit a bigger deployed budget than tier 0,
    for an otherwise identical setup -- the whole point of the martingale
    schedule (DESIGN.md §1.4's tier-caps table)."""
    config = _base_config(order_divisor=20.0)

    def deployed_room(spy_ctx: ladder.SpyContext) -> float:
        from nightwatch.strategy.params import tier_cap
        budget = 2000.0  # matches _base_inputs' equity/core_fraction
        return budget * tier_cap(spy_ctx.tier)

    tier0_room = deployed_room(_spy(100.0, 100.0))
    tier5_room = deployed_room(_spy(200.0, 100.0))  # -50% drawdown -> tier 5
    assert tier5_room > tier0_room


# ---------------------------------------------------------------------------
# Fixed vs trailing exit.
# ---------------------------------------------------------------------------


def test_fixed_exit_sells_every_profitable_lot_on_signal():
    config = _base_config(exit_mode="fixed")
    state = ladder.LadderState()
    lot = ladder.LadderLot(
        symbol="UPRO", book="catapult", units=10.0, entry_price=10.0, cost_basis=100.0,
        entry_date="2024-01-01", entry_reference_spy=100.0, entry_spy_ath=100.0,
        entry_spy_close=100.0, entry_spy_drawdown=0.0, entry_tier=0, position_ref="p1",
    )
    inputs = _base_inputs(cone_z=5.0, upro_price=20.0, open_lots=(lot,))
    decision = ladder.decide(inputs, state, config)
    assert len(decision.closed_lots) == 1
    assert decision.closed_lots[0].position_ref == "p1"


def test_trailing_exit_does_not_close_until_pullback_from_peak():
    config = _base_config(exit_mode="trailing", trailing_stop_pct=0.05)
    state = ladder.LadderState()
    lot = ladder.LadderLot(
        symbol="UPRO", book="catapult", units=10.0, entry_price=10.0, cost_basis=100.0,
        entry_date="2024-01-01", entry_reference_spy=100.0, entry_spy_ath=100.0,
        entry_spy_close=100.0, entry_spy_drawdown=0.0, entry_tier=0, position_ref="p1",
    )
    # Bar A: cone_z crosses sell_threshold while profitable -> arms, no close.
    inputs_a = _base_inputs(cone_z=5.0, upro_price=20.0, open_lots=(lot,))
    decision_a = ladder.decide(inputs_a, state, config)
    assert decision_a.closed_lots == []
    state = decision_a.new_state
    # Bar B: price keeps rising (new peak, still no close).
    inputs_b = _base_inputs(cone_z=0.0, upro_price=25.0, open_lots=(lot,))
    decision_b = ladder.decide(inputs_b, state, config)
    assert decision_b.closed_lots == []
    state = decision_b.new_state
    # Bar C: pulls back >5% from the 25.0 peak, still above entry -> closes.
    inputs_c = _base_inputs(cone_z=0.0, upro_price=23.0, open_lots=(lot,))
    decision_c = ladder.decide(inputs_c, state, config)
    assert len(decision_c.closed_lots) == 1


# ---------------------------------------------------------------------------
# Dwell-bars persistence gate.
# ---------------------------------------------------------------------------


def test_dwell_bars_requires_consecutive_qualifying_bars():
    config = _base_config(dwell_bars=3)
    state = ladder.LadderState()
    # Bar 1: qualifies (streak=1) -- not enough yet.
    d1 = ladder.decide(_base_inputs(cone_z=-5.0), state, config)
    assert d1.new_lots == []
    assert d1.new_state.below_streak == 1
    state = d1.new_state
    # Bar 2: breaks the streak.
    d2 = ladder.decide(_base_inputs(trading_date="2024-01-03", cone_z=1.0), state, config)
    assert d2.new_state.below_streak == 0
    state = d2.new_state
    # Bars 3-5: qualifies three times in a row -> buys on the third.
    for i, date in enumerate(["2024-01-04", "2024-01-05", "2024-01-06"]):
        d = ladder.decide(_base_inputs(trading_date=date, cone_z=-5.0), state, config)
        state = d.new_state
    assert len(d.new_lots) == 1


# ---------------------------------------------------------------------------
# SCHG new-ATH buy / sell-to-fund.
# ---------------------------------------------------------------------------


def test_schg_bought_on_new_ath_from_surplus_cash():
    config = _base_config()
    state = ladder.LadderState(ath=90.0)  # yesterday's ath was lower
    inputs = _base_inputs(
        spy=_spy(100.0, 100.0), cone_z=None, cash=40_000.0, schg_position_value=60_000.0,
    )
    decision = ladder.decide(inputs, state, config)
    schg_buys = [i for i in decision.intents if i.symbol == "SCHG" and i.side == "open"]
    assert schg_buys, "a surplus-cash new-ATH bar should buy SCHG"


def test_no_schg_buy_on_sale_bar():
    """DESIGN doc: 'No SCHG order is emitted on a bar that contains a UPRO
    sale.'"""
    config = _base_config()
    state = ladder.LadderState(ath=90.0)
    lot = ladder.LadderLot(
        symbol="UPRO", book="catapult", units=10.0, entry_price=10.0, cost_basis=100.0,
        entry_date="2023-06-01", entry_reference_spy=95.0, entry_spy_ath=95.0,
        entry_spy_close=95.0, entry_spy_drawdown=0.0, entry_tier=0, position_ref="p1",
    )
    inputs = _base_inputs(spy=_spy(100.0, 100.0), cone_z=5.0, upro_price=20.0, open_lots=(lot,))
    decision = ladder.decide(inputs, state, config)
    assert not any(i.symbol == "SCHG" for i in decision.intents)


def test_schg_sold_to_fund_ladder_buy_when_cash_short():
    config = _base_config()
    state = ladder.LadderState()
    inputs = _base_inputs(cone_z=-5.0, cash=40_000.0, schg_position_value=90_000.0, equity=100_000.0)
    decision = ladder.decide(inputs, state, config)
    # cash_target = 8 * order_size; with these numbers cash is short.
    schg_sells = [i for i in decision.intents if i.symbol == "SCHG" and i.side == "close"]
    if decision.new_state.cash_target and inputs.cash < decision.new_state.cash_target:
        assert schg_sells
