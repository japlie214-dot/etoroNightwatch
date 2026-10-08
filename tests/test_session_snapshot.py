"""Post-decision snapshots from one broker envelope, using only fakes."""

from copy import deepcopy
import json
from pathlib import Path

import httpx
import pytest

from nightwatch.engine import session
from nightwatch.notify.commands import _pnl_reply
from .test_session_decide import config, prepare_session
from .test_session_market import DAY, IDS, FakeTiingo


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("test attempted a real HTTP call")
    monkeypatch.setattr(httpx, "get", refused)
    monkeypatch.setattr(httpx.Client, "send", refused)


@pytest.fixture
def pnl():
    return json.loads((Path(__file__).parent / "fixtures/etoro_contracts/pnl_real.json").read_text())


def build(pnl, symbols=None):
    return session.build_snapshot(
        session_id=1, taken_at="2026-10-02T12:00:00Z", pnl=pnl, available_cash=1000.0,
        symbol_by_instrument={} if symbols is None else symbols, session_prices={"UPRO": 1.0},
        sleeve={"cash_reserve_usd": 0.0, "sleeve_cash": 1000.0,
                "sleeve_equity": 1000.0, "schg_position_value": 0.0},
    )


def test_fixture_broker_marks_and_equity(pnl):
    before = deepcopy(pnl)
    snap = build(pnl)
    cp = pnl["clientPortfolio"]
    assert len(snap.positions) == len(cp["positions"])
    for position, p in zip(snap.positions, cp["positions"]):
        assert position.position_id == str(p["positionID"])
        assert position.instrument_id == p["instrumentID"] and position.symbol is None
        assert position.units == p["units"] and position.entry_price == p["openRate"]
        assert position.current_price == p["unrealizedPnL"]["closeRate"]
        assert position.amount == p["amount"] and position.leverage == p["leverage"]
        assert position.unrealized_pnl == p["unrealizedPnL"]["pnL"]
        assert position.open_date == p["openDateTime"]
    assert snap.equity == cp["credit"] + sum(p["amount"] + p["unrealizedPnL"]["pnL"] for p in cp["positions"])
    assert snap.cash == cp["credit"] and snap.available_cash == 1000.0
    assert snap.unrealized_pnl == cp["unrealizedPnL"] and snap.realized_pnl is None
    assert snap.raw["complete"] is True and snap.raw["missing"] == []
    assert snap.raw["pnl"] == pnl == before
    assert snap.raw["version"] == 1 and snap.raw["phase"] == "post_decision"
    assert snap.raw["source"] == "pnl" and snap.raw["account_currency_id"] == 1


@pytest.mark.parametrize("field", ["closeRate", "pnL", "amount"])
def test_missing_mark_never_publishes_partial_equity(pnl, field):
    p = pnl["clientPortfolio"]["positions"][0]
    del (p if field == "amount" else p["unrealizedPnL"])[field]
    snap = build(pnl)
    assert snap.raw["complete"] is False
    assert snap.raw["missing"] == [str(p["positionID"])]
    assert snap.equity is None


def test_non_usd_incomplete(pnl):
    pnl["clientPortfolio"]["accountCurrencyId"] = 2
    snap = build(pnl)
    assert snap.raw["complete"] is False and snap.equity is None


def test_flat_demo():
    snap = build({"clientPortfolio": {"positions": [], "credit": 100001.1, "accountCurrencyId": 1}})
    assert snap.equity == snap.cash == 100001.1
    assert snap.positions == [] and snap.raw["complete"] is True


def test_missing_portfolio():
    with pytest.raises(session.SnapshotError, match="snapshot refused: pnl has no clientPortfolio"):
        build({})


def test_mark_check_first_position_evidence_only(pnl):
    p = pnl["clientPortfolio"]["positions"][0]
    second = deepcopy(p)
    second["positionID"] += 1
    second["unrealizedPnL"]["closeRate"] = 9.0
    pnl["clientPortfolio"]["positions"].append(second)
    snap = build(pnl, {p["instrumentID"]: "UPRO"})
    assert snap.raw["managed_instrument_ids"] == [p["instrumentID"]]
    assert snap.raw["mark_check"] == [{"symbol": "UPRO", "session_price": 1.0,
                                       "broker_close_rate": 1.25, "diff_pct": 25.0}]
    assert snap.raw["complete"] is True


@pytest.mark.parametrize("case", ["broker_missing", "price_missing", "price_none", "price_zero"])
def test_mark_check_missing_or_zero_price_is_evidence_only(pnl, case):
    p = pnl["clientPortfolio"]["positions"][0]
    prices = {"UPRO": 1.0}
    if case == "broker_missing":
        del p["unrealizedPnL"]["closeRate"]
    elif case == "price_missing":
        prices = {}
    else:
        prices["UPRO"] = None if case == "price_none" else 0.0
    snap = session.build_snapshot(
        session_id=1, taken_at="2026-10-02T12:00:00Z", pnl=pnl, available_cash=None,
        symbol_by_instrument={p["instrumentID"]: "UPRO"}, session_prices=prices, sleeve={},
    )
    assert snap.raw["mark_check"] == [{
        "symbol": "UPRO", "session_price": prices.get("UPRO"),
        "broker_close_rate": p["unrealizedPnL"].get("closeRate"), "diff_pct": None,
    }]
    assert snap.raw["complete"] is (case != "broker_missing")
    if case == "broker_missing":
        assert snap.raw["missing"] == [str(p["positionID"])] and snap.equity is None


@pytest.mark.parametrize("failure", [None, "read", "build", "insert"])
def test_session_snapshot_report_not_gate(repo, host, config, monkeypatch, pnl, failure):
    client = prepare_session(host, monkeypatch)
    calls = []
    def get_pnl():
        assert repo.get_decision(1) is not None
        calls.append("pnl")
        if failure == "read":
            raise RuntimeError("fake offline")
        return {} if failure == "build" else pnl
    client.get_pnl = get_pnl
    if failure == "insert":
        def fail_insert(snap):
            raise RuntimeError("fake storage failure")
        monkeypatch.setattr(repo, "insert_snapshot", fail_insert)
    result = session.run_session(host=host, config=config, repo=repo, client=client,
                                 tiingo=FakeTiingo(), trading_date=DAY, wait=False)
    assert calls == ["pnl"] and result.status == "would_have_done"
    row = repo.get_session(result.session_id)
    assert row.state == "finished" and row.outcome == result.reason
    assert repo.conn.execute("SELECT COUNT(*) FROM portfolio_snapshots").fetchone()[0] == (0 if failure else 1)
    if failure:
        expected = {"read": "RuntimeError: fake offline",
                    "build": "SnapshotError: snapshot refused: pnl has no clientPortfolio",
                    "insert": "RuntimeError: fake storage failure"}[failure]
        assert result.reason.endswith("; snapshot failed: " + expected)
        assert repo.conn.execute("SELECT COUNT(*) FROM portfolio_snapshot_positions").fetchone()[0] == 0
    else:
        snap = repo.latest_snapshot()
        assert snap.session_id == result.session_id and snap.taken_at
        assert snap.raw["pnl"] == pnl and snap.raw["source"] == "pnl"
        assert snap.raw["managed_instrument_ids"] == sorted(IDS.values())
        assert snap.available_cash == 1000.0
        assert snap.raw["sleeve"] == {"cash_reserve_usd": config.strategy.cash_reserve_usd,
                                     "sleeve_cash": 1000.0, "sleeve_equity": 1000.0,
                                     "schg_position_value": 0.0}
        assert repo.conn.execute("SELECT COUNT(*) FROM portfolio_snapshot_positions").fetchone()[0] == len(pnl["clientPortfolio"]["positions"])
        assert snap.positions == build(pnl).positions
        assert result.reason.endswith("; snapshot 1: 1 position(s), equity=3.75, complete=True")
        assert _pnl_reply(repo) == (f"equity=3.75 cash=1.25 unrealized_pnl=1.25 realized_pnl=None "
                                   f"(as of {snap.taken_at})")
