"""Gate 12: configured open-lot cap, alert-only loss and informational TPs."""

from __future__ import annotations

import pytest

from nightwatch.config import RiskConfig
from nightwatch.db.repo import LotAnnotation, Repo
from nightwatch.engine.preflight import gate_12_risk_caps
from nightwatch.etoro.client import EtoroClient


_LIVE_TP_ROW: dict[str, object] = {
    "positionID": 2867253750, "openDateTime": "2024-11-27T14:30:41.173Z",
    "openRate": 27.65, "instrumentID": 1084, "isBuy": True,
    "takeProfitRate": 304.15, "stopLossRate": 0.0001, "amount": 200.0,
    "leverage": 1, "units": 7.233273, "isNoTakeProfit": False,
    "isNoStopLoss": True,
}


def test_gate_12_all_limits_set_empty_book(repo: Repo) -> None:
    risk = RiskConfig(max_daily_loss_usd=500.0, max_open_lots=100)
    changes = repo.conn.total_changes
    result = gate_12_risk_caps(risk=risk, repo=repo, positions=[])
    assert result.gate == 12
    assert result.name == "risk_caps"
    assert result.passed is True
    assert result.reason == "risk caps ok"
    assert result.details == {
        "max_notional_per_session_usd": 5000.0,
        "max_daily_loss_usd": 500.0,
        "max_open_lots": 100,
        "open_lots": 0,
        "broker_take_profit_count": 0,
        "evaluated_elsewhere": ["max_notional_per_session_usd", "max_daily_loss_usd"],
    }
    assert repo.conn.total_changes == changes


def test_gate_12_unset_open_lot_cap(repo: Repo) -> None:
    result = gate_12_risk_caps(risk=RiskConfig(), repo=repo, positions=[])
    assert result.passed is False
    assert result.reason == "risk limits unset: max_open_lots"
    assert result.details["max_open_lots"] is None
    assert result.details["open_lots"] == 0
    assert result.details["broker_take_profit_count"] == 0


def test_gate_12_unset_daily_loss_threshold_passes(repo: Repo) -> None:
    result = gate_12_risk_caps(
        risk=RiskConfig(max_open_lots=100), repo=repo, positions=[],
    )
    assert result.passed is True
    assert result.reason == "risk caps ok"
    assert result.details["max_daily_loss_usd"] is None


@pytest.mark.parametrize("open_lots, passed", [(3, False), (2, True)])
def test_gate_12_open_lot_cap(repo: Repo, open_lots: int, passed: bool) -> None:
    for i in range(open_lots + 1):
        repo.insert_lot_annotation(LotAnnotation(
            position_id=f"pos-{i}", symbol="SCHG",
            book="catapult" if i % 2 == 0 else "reentry",
            entry_date="2024-09-03T10:15:00+00:00", entry_amount=10.0,
            origin="adopted", is_open=i < open_lots,
        ))
    annotations = repo.list_lot_annotations()
    changes = repo.conn.total_changes
    result = gate_12_risk_caps(
        risk=RiskConfig(max_daily_loss_usd=500.0, max_open_lots=2),
        repo=repo, positions=[],
    )
    assert result.passed is passed
    assert result.reason == (
        "risk caps ok" if passed else "3 open lots exceed max_open_lots 2"
    )
    assert result.details["open_lots"] == open_lots
    assert result.details["max_open_lots"] == 2
    assert result.details["evaluated_elsewhere"] == [
        "max_notional_per_session_usd", "max_daily_loss_usd",
    ]
    assert repo.list_lot_annotations() == annotations
    assert repo.conn.total_changes == changes


def test_gate_12_broker_take_profit_is_information_only(repo: Repo) -> None:
    position = EtoroClient._parse_position(_LIVE_TP_ROW)
    result = gate_12_risk_caps(
        risk=RiskConfig(max_daily_loss_usd=500.0, max_open_lots=100),
        repo=repo, positions=[position],
    )
    assert result.passed is True
    assert result.reason == "risk caps ok"
    assert result.details["broker_take_profit_count"] == 1
