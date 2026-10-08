"""Phase A input mapping and dry-run stop, using only fakes."""

from dataclasses import replace
import json

import httpx
import pytest

from nightwatch.config import TradingMode, load_config
from nightwatch.db.repo import Bucket, LotAnnotation
from nightwatch.engine import session
from nightwatch.etoro.models import Balance, Position
from nightwatch.strategy.ladder import LadderIntent, ReentryBucket
from .test_session_market import DAY, NOW, IDS, FakeTiingo, market_client, assembled_signal
from .test_session_skeleton import fake_gates


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("test attempted a real HTTP call")
    monkeypatch.setattr(httpx, "get", refused)
    monkeypatch.setattr(httpx.Client, "send", refused)


@pytest.fixture
def config(config_path, secrets_path, host):
    return load_config(config_path=config_path, secrets_path=secrets_path, env={}, host=host)


def position(ref, symbol="UPRO", units=2, is_buy=True):
    return Position(ref, IDS[symbol], units, 80, 150, 1, is_buy)


def annotation(ref, reference=100):
    return LotAnnotation(ref, "UPRO", "catapult", "2026-10-01", 999, "app",
                         entry_reference_spy=reference, entry_spy_ath=110,
                         entry_spy_close=100, entry_spy_drawdown=-0.1, entry_tier=1)


def build(repo, positions=(), reserve=0, available=1000):
    return session.build_ladder_inputs(
        iso=DAY.isoformat(), year=DAY.year, signal=assembled_signal(),
        prices={"SPY": 100, "UPRO": 90, "SCHG": 50}, positions=positions,
        symbol_by_instrument={v: k for k, v in IDS.items()},
        balance=Balance(2000, None, available), repo=repo, cash_reserve_usd=reserve,
    )


def test_unannotated_upro_excluded_and_all_schg_marked(repo):
    inputs, notes = build(repo, [position("s1", "SCHG", 2), position("s2", "SCHG", 3), position("u")])
    assert inputs.open_lots == []
    assert inputs.schg_position_value == (2 + 3) * 50
    assert inputs.equity == inputs.cash + 250 == 1250
    assert "1 UPRO position(s) not annotated (adoption is S4): excluded from lots and equity" in notes
    assert inputs.prior_year_schg_values == ()
    assert "no prior-year SCHG values" in notes


def test_annotated_lot_and_bucket_mapping(repo, monkeypatch):
    a, p = annotation("u"), position("u")
    repo.insert_lot_annotation(a)
    repo.upsert_bucket(Bucket("b", "2026-09-01", 100, 300, 100, 2, 1, 95))
    years = []
    monkeypatch.setattr(repo, "mean_schg_value", lambda year: years.append(year) or 400.0)
    inputs, notes = build(repo, [p])
    lot, = inputs.open_lots
    assert lot.to_dict() == dict(symbol="UPRO", book=a.book, units=p.units, entry_price=p.entry_price,
                                cost_basis=p.amount, entry_date=a.entry_date, entry_reference_spy=100,
                                entry_spy_ath=110, entry_spy_close=100, entry_spy_drawdown=-0.1,
                                entry_tier=1, bucket_id=None, position_ref="u")
    assert inputs.equity == 1000 + 2 * 90
    assert inputs.buckets == [ReentryBucket("b", "2026-09-01", 100, 300, 100, 2, 1, 95)]
    assert inputs.prior_year_schg_values == (400.0,) and years == [2025]
    assert notes == []


@pytest.mark.parametrize("missing", [True, False])
def test_bad_annotation_refused(repo, missing):
    repo.insert_lot_annotation(annotation("bad", 100 if missing else None))
    with pytest.raises(session.DecideInputError, match="position bad"):
        build(repo, [] if missing else [position("bad")])


def test_bucket_without_reference_refused(repo):
    repo.upsert_bucket(Bucket("bad", "2026-09-01", 100, 300, 100, 2))
    with pytest.raises(session.DecideInputError, match="bucket bad has no last_reference_spy"):
        build(repo)


def test_cash_reserve_and_ignored_positions(repo):
    inputs, notes = build(repo, [position("short", "SCHG", is_buy=False),
                                replace(position("foreign"), instrument_id=99)], reserve=100, available=50)
    assert inputs.cash == inputs.equity == inputs.schg_position_value == 0.0
    assert notes == ["ignored short position short", "1 position(s) in unmanaged instruments ignored",
                     "no prior-year SCHG values"]


def test_arming_refusals_in_order(config):
    assert session.arming_refusals(config) == [
        "mode is dry_run, not live", "NIGHTWATCH_ARM not set",
        "risk limits unset: max_daily_loss_usd, max_open_lots",
    ]
    armed = replace(config, mode=TradingMode.LIVE, arm_flag_present=True,
                    risk=replace(config.risk, max_daily_loss_usd=100, max_open_lots=10))
    assert session.arming_refusals(armed) == []


def test_intent_vocabulary():
    assert session.intent_to_dict(LadderIntent("UPRO", "close", position_ref="lot", reason="exit", book="catapult")) == {
        "symbol": "UPRO", "side": "close", "amount": None, "position_ref": "lot", "reason": "exit", "book": "catapult",
    }


def prepare_session(host, monkeypatch):
    monkeypatch.setattr(host, "now", lambda: NOW)
    monkeypatch.setattr(session, "build_preflight_gates", lambda **kwargs: fake_gates([]))
    monkeypatch.setattr(session, "assemble_spy_signal", lambda *args, **kwargs: assembled_signal())
    return market_client()


@pytest.mark.parametrize("override", [None, 1234.0])
def test_session_decision_persisted_without_advancing_state(repo, host, config, monkeypatch, override):
    client = prepare_session(host, monkeypatch)
    config = replace(config, strategy=replace(config.strategy, budget_override_usd=override))
    repo.get_strategy_state()  # Initialize the singleton before capturing its persisted timestamp.
    before = repo.get_strategy_state()
    result = session.run_session(host=host, config=config, repo=repo, client=client,
                                 tiingo=FakeTiingo(), trading_date=DAY, wait=False)
    assert result.status == "would_have_done"
    assert repo.get_session(result.session_id).state == "finished"
    assert repo.get_strategy_state() == before
    assert repo.conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
    d = repo.get_decision(result.session_id)
    assert d.reason.startswith("phase A, pre-settlement, buy intents provisional; dry run: ")
    assert d.cash == d.equity == 1000
    assert d.spy_close == 100 and d.cone_z == 1.25
    assert "arming refused: " + "; ".join(session.arming_refusals(config)) in result.reason
    assert ("budget_override_usd set but not honoured (ladder has no field)" in result.reason) is (override is not None)
    intents = json.loads(d.intents) if isinstance(d.intents, str) else d.intents
    summary = "; ".join(f"{i['side']} {i['symbol']} ${i['amount']:.2f}" for i in intents) or "none"
    assert result.reason.startswith(f"would have done {len(intents)} intent(s): {summary}; ladder: ")
    assert repo.list_lot_annotations() == [] and repo.active_buckets() == []


@pytest.mark.parametrize("failure", ["balance", "portfolio", "inputs", "decide"])
def test_session_decide_failure_aborts(repo, host, config, monkeypatch, failure):
    client = prepare_session(host, monkeypatch)
    def fail(*args, **kwargs):
        raise RuntimeError("fake offline")
    if failure in ("balance", "portfolio"):
        monkeypatch.setattr(client, "get_" + failure, fail)
    elif failure == "inputs":
        repo.insert_lot_annotation(annotation("missing"))
    else:
        monkeypatch.setattr(session, "decide", fail)
    repo.get_strategy_state()  # Initialize the singleton before capturing its persisted timestamp.
    before = repo.get_strategy_state()
    result = session.run_session(host=host, config=config, repo=repo, client=client,
                                 tiingo=FakeTiingo(), trading_date=DAY, wait=False)
    assert result.status == "decide_failed"
    assert repo.get_session(result.session_id).state == "aborted"
    expected = "DecideInputError: position missing missing at broker" if failure == "inputs" else "RuntimeError: fake offline"
    assert result.reason == "decide inputs: " + expected
    assert repo.get_decision(result.session_id) is None
    assert repo.get_strategy_state() == before
