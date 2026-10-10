"""Submission limits preserve whole buys in ladder order and always pass closes."""

from dataclasses import replace
from pathlib import Path

import pytest

from nightwatch.config import load_config
from nightwatch.db.repo import LotAnnotation
from nightwatch.engine.limits import Dropped, apply_submission_limits
from nightwatch.engine.preflight import gate_12_risk_caps
from nightwatch.strategy.ladder import LadderIntent


def _limits(intents, **overrides):
    kwargs = dict(cap_usd=5000.0, reserved_usd=0.0, cash_usd=100000.0,
                  open_lots=0, max_open_lots=100,
                  min_order_usd={"SCHG": 50.0, "UPRO": 10.0})
    kwargs.update(overrides)
    return apply_submission_limits(intents, **kwargs)


def test_session_cap_and_reserved():
    buys = [LadderIntent("SCHG", "open", 666.66, reason=str(i)) for i in range(10)]
    accepted, dropped = _limits(buys)
    assert accepted == buys[:7]
    assert sum(i.amount for i in accepted) == pytest.approx(4666.62)
    assert dropped == [
        Dropped(buys[7], "session buy cap $5,000 (reserved $0.00 + accepted $4,666.62)"),
        Dropped(buys[8], "after cap stop"), Dropped(buys[9], "after cap stop"),
    ]
    accepted, dropped = _limits(buys, reserved_usd=4500.0)
    assert accepted == []
    assert dropped == [
        Dropped(buys[0], "session buy cap $5,000 (reserved $4,500.00 + accepted $0.00)"),
        *[Dropped(i, "after cap stop") for i in buys[1:]],
    ]


@pytest.mark.parametrize("overrides, reason", [
    ({"cash_usd": 1000.0}, "insufficient cash $333.34"),
    ({"open_lots": 99}, "max_open_lots 100"),
])
def test_cash_and_lot_limits_stop_later_buys(overrides, reason):
    buys = [LadderIntent("SCHG", "open", 666.66),
            LadderIntent("UPRO", "open", 666.66),
            LadderIntent("UPRO", "open", 10.0)]
    accepted, dropped = _limits(buys, **overrides)
    assert accepted == buys[:1]
    assert dropped == [Dropped(buys[1], reason), Dropped(buys[2], "after cap stop")]


def test_closes_always_pass_without_crediting_cash_or_lots():
    closes = [LadderIntent("UPRO", "close", position_ref=str(i)) for i in range(3)]
    buy = LadderIntent("SCHG", "open", 50.0)
    smaller = LadderIntent("UPRO", "open", 10.0)
    intents = [closes[0], buy, closes[1], smaller, closes[2]]
    for overrides, reason in [
        ({"cap_usd": 40.0}, "session buy cap $40 (reserved $0.00 + accepted $0.00)"),
        ({"cash_usd": 0.0}, "insufficient cash $0.00"),
        ({"open_lots": 100}, "max_open_lots 100"),
    ]:
        accepted, dropped = _limits(intents, **overrides)
        assert accepted == closes
        assert dropped == [Dropped(buy, reason), Dropped(smaller, "after cap stop")]
    assert intents == [closes[0], buy, closes[1], smaller, closes[2]]


@pytest.mark.parametrize("amount, reason", [
    (40.0, "below broker minimum $50.00"),
    (None, "buy without amount"), (0.0, "buy without amount"),
    (-1.0, "buy without amount"),
])
def test_invalid_buy_does_not_stop_prefix(amount, reason):
    invalid = LadderIntent("SCHG", "open", amount)
    valid = LadderIntent("SCHG", "open", 50.0)
    assert _limits([invalid, valid]) == ([valid], [Dropped(invalid, reason)])
    stop = LadderIntent("SCHG", "open", 6000.0)
    accepted, dropped = _limits([stop, invalid, valid])
    assert accepted == []
    assert dropped[1:] == [Dropped(invalid, reason), Dropped(valid, "after cap stop")]


@pytest.mark.parametrize("extra, accepted_count", [(0.0, 1), (5e-10, 1), (2e-9, 0)])
def test_float_tolerance(extra, accepted_count):
    buy = LadderIntent("UPRO", "open", 50.0 + extra)
    for overrides in [{"cap_usd": 50.0}, {"cash_usd": 50.0}]:
        accepted, dropped = _limits([buy], **overrides)
        assert len(accepted) == accepted_count
        assert len(dropped) == 1 - accepted_count


def test_missing_minimum_defaults_to_zero():
    buy = LadderIntent("UPRO", "open", 1.0)
    assert _limits([buy], min_order_usd={}) == ([buy], [])


def test_empty():
    assert _limits([]) == ([], [])


def test_shipped_lot_cap_allows_adopted_book_and_growth(repo, tmp_path):
    risk = load_config(
        config_path=Path(__file__).resolve().parents[1] / "config" / "config.toml",
        secrets_path=tmp_path / "absent.env", env={},
    ).risk
    for i in range(221):
        repo.insert_lot_annotation(LotAnnotation(
            position_id=f"adopted-{i}", symbol="UPRO" if i % 2 else "SCHG",
            book="catapult", entry_date="2026-01-02", entry_amount=50.0,
            origin="adopted",
        ))
    old_gate = gate_12_risk_caps(risk=replace(risk, max_open_lots=100), repo=repo, positions=[])
    assert not old_gate.passed
    assert old_gate.reason == "221 open lots exceed max_open_lots 100"
    gate = gate_12_risk_caps(risk=risk, repo=repo, positions=[])
    assert gate.passed
    assert gate.details["open_lots"] == 221
    assert gate.details["max_open_lots"] == 1000
    buy = LadderIntent("UPRO", "open", 50.0)
    assert _limits([buy], open_lots=221) == ([], [Dropped(buy, "max_open_lots 100")])
    assert _limits([buy], open_lots=221, max_open_lots=risk.max_open_lots) == ([buy], [])
