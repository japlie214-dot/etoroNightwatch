"""Session interval checks, HALT ordering and loud alerts with fakes only."""

from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from nightwatch.engine import session
from nightwatch.engine.halt import read_halt
from nightwatch.engine.interval_pnl import ClosedLot, IntervalResult
from nightwatch.strategy.ladder import LadderIntent
from .test_session_decide import config, prepare_session, no_network
from .test_session_market import DAY, IDS, FakeTiingo


PREV = "2026-10-01T12:00:00Z"


def envelope(*, mark=100, pnl=0, units=10):
    return {"clientPortfolio": {"positions": [{
        "positionID": 10, "instrumentID": IDS["UPRO"], "units": units,
        "openRate": 100, "amount": 1000, "leverage": 1, "isBuy": True,
        "openDateTime": "2026-09-01T12:00:00Z",
        "unrealizedPnL": {"closeRate": mark, "pnL": pnl},
    }], "credit": 1000, "accountCurrencyId": 1}}


def seed(repo):
    row = repo.create_session("2026-10-01", "dry_run", "demo", False)
    snap = session.build_snapshot(session_id=row.id, taken_at=PREV, pnl=envelope(),
                                  available_cash=1000, symbol_by_instrument={IDS["UPRO"]: "UPRO"},
                                  session_prices={}, sleeve={})
    repo.insert_snapshot(snap)
    return snap


class Notifier:
    def __init__(self, host, raises=False):
        self.host = host
        self.raises = raises
        self.calls = []

    def send(self, text, **kwargs):
        self.calls.append((kwargs, text, self.host.halt_flag_path().exists()))
        if self.raises:
            raise RuntimeError("fake send failure")


def run(repo, host, config, monkeypatch, *, current=None, baseline=True, history_error=False,
        notifier=None):
    if baseline:
        seed(repo)
    client = prepare_session(host, monkeypatch)
    client.get_pnl = lambda: envelope() if current is None else current
    def history(**kwargs):
        assert kwargs == {"start_date": "2026-10-01", "end_date": DAY.isoformat()}
        if history_error:
            raise RuntimeError("fake history offline")
        return []
    client.get_trade_history = history
    config = replace(config, risk=replace(config.risk, max_daily_loss_usd=500))
    return session.run_session(host=host, config=config, repo=repo, client=client,
                               tiingo=FakeTiingo(), trading_date=DAY, wait=False, notifier=notifier)


def test_first_snapshot(repo, host, config, monkeypatch):
    notifier = Notifier(host)
    result = run(repo, host, config, monkeypatch, baseline=False, notifier=notifier)
    assert result.interval.state == "no_baseline"
    assert "interval: no baseline (first snapshot)" in result.reason
    assert [c[0]["event"] for c in notifier.calls] == ["session_report"]
    assert not host.halt_flag_path().exists()


@pytest.mark.parametrize("mark,pnl,event,halt,state", [
    (99, -10, None, False, "complete"),
    (99, -70, "alert_c", True, "complete"),
    (40, -600, "alert_a", False, "complete"),
])
def test_market_residual_and_loss(repo, host, config, monkeypatch, mark, pnl, event, halt, state):
    notifier = Notifier(host)
    result = run(repo, host, config, monkeypatch, current=envelope(mark=mark, pnl=pnl), notifier=notifier)
    assert result.interval.state == state and result.interval.pnl_usd == pnl
    assert result.interval.residual_usd == pnl - 10 * (mark - 100)
    assert [c[0]["event"] for c in notifier.calls] == ["session_report"] + ([event] if event else [])
    assert all(c[2] is halt for c in notifier.calls)  # HALT precedes even the normal report.
    assert host.halt_flag_path().exists() is halt
    if halt:
        assert read_halt(host)["source"] == "session"
        assert read_halt(host)["reason"] == "alert C residual $-60.00"
        assert "HALT set" in notifier.calls[-1][1]
    if event == "alert_a":
        assert "Alert only, trading continues." in notifier.calls[-1][1]


@pytest.mark.parametrize("case", ["integrity", "history"])
def test_integrity_or_history_failure(repo, host, config, monkeypatch, case):
    notifier = Notifier(host)
    result = run(repo, host, config, monkeypatch, current=envelope(units=11 if case == "integrity" else 10),
                 history_error=case == "history", notifier=notifier)
    integrity = case == "integrity"
    assert result.interval.state == ("integrity" if integrity else "incomplete")
    assert [c[0]["event"] for c in notifier.calls] == ["session_report", "integrity" if integrity else "interval_incomplete"]
    assert host.halt_flag_path().exists() is integrity
    assert all(c[2] is integrity for c in notifier.calls)
    if integrity:
        assert "units changed" in read_halt(host)["reason"]
    else:
        assert "no HALT" in notifier.calls[-1][1]


@pytest.mark.parametrize("integrity", [False, True])
def test_halt_persistence_failure(repo, host, config, monkeypatch, integrity):
    monkeypatch.setattr(session, "write_halt", Mock(side_effect=OSError("fake disk full")))
    notifier = Notifier(host)
    current = envelope(units=11) if integrity else envelope(mark=99, pnl=-70)
    result = run(repo, host, config, monkeypatch, current=current, notifier=notifier)
    assert "HALT persistence failed: OSError: fake disk full" in result.reason
    assert repo.get_session(result.session_id).error == "fake disk full"
    assert "HALT persistence FAILED: OSError: fake disk full" in notifier.calls[-1][1]
    assert "Next session is NOT protected" in notifier.calls[-1][1]
    assert "stops at gate 1" not in notifier.calls[-1][1]
    assert not host.halt_flag_path().exists()


def test_notifier_failure_does_not_skip_halt(repo, host, config, monkeypatch, caplog):
    notifier = Notifier(host, raises=True)
    result = run(repo, host, config, monkeypatch, current=envelope(mark=99, pnl=-70), notifier=notifier)
    assert result.status == "would_have_done" and result.interval.alert_c
    assert repo.get_session(result.session_id).outcome == result.reason
    assert repo.get_session(result.session_id).error is None
    assert read_halt(host)["source"] == "session"
    assert [c[0]["event"] for c in notifier.calls] == ["session_report", "alert_c"]
    assert all(c[2] for c in notifier.calls)
    assert "Session report failed: fake send failure" in caplog.text
    assert "Session alert_c alert failed: fake send failure" in caplog.text


def test_snapshot_failure(repo, host, config, monkeypatch):
    result = run(repo, host, config, monkeypatch, current={})
    assert result.status == "would_have_done" and result.interval is None
    assert "snapshot failed" in result.reason
    assert not host.halt_flag_path().exists()


@pytest.mark.parametrize("intents,summary", [
    ([LadderIntent("SCHG", "open", amount=666.66)] * 136,
     f"open SCHG 136x $666.66 = ${sum([666.66] * 136):.2f}"),
    ([LadderIntent("SCHG", "open", amount=10), LadderIntent("UPRO", "close", position_ref="1"),
      LadderIntent("SCHG", "open", amount=20), LadderIntent("UPRO", "close", position_ref="2")],
     "open SCHG 2 orders = $30.00; close UPRO 2 lot(s)"),
])
def test_grouped_summary(repo, host, config, monkeypatch, intents, summary):
    original = session.decide
    def decide(*args):
        return replace(original(*args), intents=intents)
    monkeypatch.setattr(session, "decide", decide)
    result = run(repo, host, config, monkeypatch, baseline=False)
    assert result.reason.startswith(f"would have done {len(intents)} intent(s): {summary}; ladder: ")


def test_history_filter_mapping_and_dedup(repo, config, monkeypatch):
    prev = seed(repo)
    row = SimpleNamespace(position_id=20, instrument_id=IDS["UPRO"], units=2, profit=3, fees=-1,
                          open_date="2026-09-01", close_date="2026-10-01T09:00:00-04:00")
    rows = [
        SimpleNamespace(**{**vars(row), "instrument_id": IDS["SPY"]}),
        SimpleNamespace(**{**vars(row), "close_date": "2026-10-01T08:00:00-04:00"}),
        row, SimpleNamespace(**{**vars(row), "profit": 999}),
    ]
    client = SimpleNamespace(get_trade_history=Mock(return_value=rows))
    compute = Mock(return_value="sentinel")
    monkeypatch.setattr(session, "compute_interval", compute)
    assert session._interval_for(prev, prev, client=client, managed_ids=frozenset([IDS["UPRO"]]),
                                 config=config, today=DAY) == "sentinel"
    assert compute.call_args.kwargs["closed"] == [ClosedLot("20", IDS["UPRO"], 2, 3, -1, row.open_date, row.close_date)]
    assert compute.call_args.kwargs["entry_marks"] == compute.call_args.kwargs["exit_marks"] == {}
    client.get_trade_history.assert_called_once_with(start_date="2026-10-01", end_date="2026-10-02")
    client.get_trade_history.return_value = [SimpleNamespace(**{**vars(row), "close_date": None})]
    session._interval_for(prev, prev, client=client, managed_ids=frozenset([IDS["UPRO"]]), config=config, today=DAY)
    assert compute.call_args.kwargs["closed"] is None


def test_disabled_thresholds(repo, config):
    prev = seed(repo)
    cur = session.build_snapshot(session_id=prev.session_id, taken_at="2026-10-02T12:00:00Z",
                                  pnl=envelope(mark=40, pnl=-700), available_cash=1000,
                                  symbol_by_instrument={}, session_prices={}, sleeve={})
    config = replace(config, risk=replace(config.risk, max_daily_loss_usd=None, max_unexplained_loss_usd=None))
    r = session._interval_for(prev, cur, client=SimpleNamespace(get_trade_history=lambda **kw: []),
                              managed_ids=frozenset([IDS["UPRO"]]), config=config, today=DAY)
    assert r.state == "complete" and r.pnl_usd == -700 and r.residual_usd == -100
    assert not r.alert_a and not r.alert_c


def test_interval_format():
    r = IntervalResult("complete", -1234.56, -7.14, 0, -1234.56, True, True,
                       tuple(str(n) for n in range(7)), ())
    assert session.format_interval(r) == ("interval: complete, P&L $-1,234.56, -7.14%, residual $-1,234.56; "
                                           "reasons: 0 | 1 | 2 | 3 | 4 (+2 more)")
    assert session.format_interval(replace(r, state="incomplete", pnl_usd=None, pnl_pct=None,
                                           residual_usd=None, reasons=())) == "interval: incomplete, P&L n/a, n/a, residual n/a"
