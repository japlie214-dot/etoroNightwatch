"""Tests for ``nightwatch.strategy.state`` -- the bridge between
``db.repo.StrategyState`` and ``strategy.ladder.LadderState``, and the
specific warmup-trap failure mode this bridge exists to prevent
(DESIGN.md §1.4, ``strategy/state.py``'s own module docstring).
"""

from __future__ import annotations

import json

import pytest

from nightwatch.db.repo import Repo
from nightwatch.strategy import ladder, state


def test_load_on_a_fresh_db_returns_defaults(repo: Repo) -> None:
    loaded = state.load(repo)
    assert loaded == ladder.LadderState()


def test_save_then_load_round_trips_every_field(repo: Repo) -> None:
    original = ladder.LadderState(
        bars=42, ath=511.23, below_streak=2, recyclable_cash=123.45,
        budget_year=2026, budget=19_600.0, order_size=326.67, cash_target=2613.36,
        sale_batch_sequence=3, sale_batch_date="2026-08-01",
    )
    state.save(repo, original)
    loaded = state.load(repo)
    assert loaded.bars == original.bars
    assert loaded.ath == original.ath
    assert loaded.below_streak == original.below_streak
    assert loaded.recyclable_cash == original.recyclable_cash
    assert loaded.budget_year == original.budget_year
    assert loaded.budget == original.budget
    assert loaded.order_size == original.order_size
    assert loaded.cash_target == original.cash_target
    assert loaded.sale_batch_sequence == original.sale_batch_sequence
    assert loaded.sale_batch_date == original.sale_batch_date


def test_ladder_state_json_round_trip() -> None:
    original = ladder.LadderState(
        bars=7, ath=500.0, below_streak=1, recyclable_cash=50.0,
        budget_year=2026, budget=1000.0, order_size=100.0, cash_target=800.0,
        sale_batch_sequence=1, sale_batch_date="2026-01-05",
        trail_state={"pos-1": {"peak": 12.5, "armed": 1.0}},
    )
    blob = json.dumps(original.to_dict())
    restored = ladder.LadderState.from_dict(json.loads(blob))
    assert restored == original


def test_ladder_lot_and_bucket_json_round_trip() -> None:
    lot = ladder.LadderLot(
        symbol="UPRO", book="catapult", units=10.0, entry_price=10.0, cost_basis=100.0,
        entry_date="2024-01-01", entry_reference_spy=100.0, entry_spy_ath=100.0,
        entry_spy_close=100.0, entry_spy_drawdown=0.0, entry_tier=0, position_ref="p1",
    )
    restored_lot = ladder.LadderLot.from_dict(json.loads(json.dumps(lot.to_dict())))
    assert restored_lot == lot

    bucket = ladder.ReentryBucket(
        bucket_id="b1", sale_date="2024-01-01", sale_spy_close=130.0,
        proceeds=300.0, tranche_amount=100.0, created_bar=1,
        tranches_bought=1, last_reference_spy=125.0,
    )
    restored_bucket = ladder.ReentryBucket.from_dict(json.loads(json.dumps(bucket.to_dict())))
    assert restored_bucket == bucket


# ---------------------------------------------------------------------------
# The warmup trap, driven through the REAL repo/state bridge this time (not
# just the in-memory LadderState the ladder.py-level test already covers).
# ---------------------------------------------------------------------------


def test_forgetting_to_call_save_replays_bar_one_forever(repo: Repo) -> None:
    """If a caller runs a session, gets a ``LadderDecision``, but never
    calls ``state.save(repo, decision.new_state)``, the NEXT session's
    ``state.load(repo)`` reads back stale (here: default/zero) state --
    ``bars`` never advances past whatever was last actually saved, which is
    nothing. This is the live-app shape of the same trap
    ``test_strategy_ladder.py::test_bars_not_persisted_stays_in_warmup_forever``
    exercises purely in-memory.
    """
    config = ladder.LadderConfig(warmup_bars=3, order_divisor=20.0, min_order_size=50.0)
    spy = ladder.SpyContext(ath=100.0, close=100.0, drawdown=0.0, tier=0)

    for _ in range(5):
        loaded = state.load(repo)  # always reads the same never-saved defaults
        inputs = ladder.LadderInputs(
            trading_date="2024-01-02", year=2024, spy=spy, cone_z=-5.0,
            upro_price=10.0, schg_price=50.0, cash=40_000.0, equity=100_000.0,
            schg_position_value=60_000.0, open_lots=(), buckets=(),
        )
        decision = ladder.decide(inputs, loaded, config)
        assert decision.diagnostics.get("warmup") is True
        # BUG: state.save(repo, decision.new_state) is never called.

    assert state.load(repo).bars == 0  # nothing was ever persisted


def test_calling_save_every_session_clears_warmup(repo: Repo) -> None:
    config = ladder.LadderConfig(warmup_bars=3, order_divisor=20.0, min_order_size=50.0)
    spy = ladder.SpyContext(ath=100.0, close=100.0, drawdown=0.0, tier=0)

    for i in range(5):
        loaded = state.load(repo)
        inputs = ladder.LadderInputs(
            trading_date=f"2024-01-0{i + 2}", year=2024, spy=spy, cone_z=-5.0,
            upro_price=10.0, schg_price=50.0, cash=40_000.0, equity=100_000.0,
            schg_position_value=60_000.0, open_lots=(), buckets=(),
        )
        decision = ladder.decide(inputs, loaded, config)
        state.save(repo, decision.new_state)

    final = state.load(repo)
    assert final.bars == 5
    assert final.bars > config.warmup_bars
