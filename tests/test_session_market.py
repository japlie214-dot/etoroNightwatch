"""S6b market fetching and session signal paths, with no real HTTP calls."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from nightwatch.config import load_config
from nightwatch.db.repo import LotAnnotation, PriceWindowFingerprint
from nightwatch.engine import session
from nightwatch.engine.preflight import GateResult, GateSpec
from nightwatch.etoro.models import Candle, Eligibility, Rate
from nightwatch.market.source import IexQuote


NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
DAY = date(2026, 10, 2)
IDS = {"SPY": 1, "SCHG": 2, "UPRO": 3}


class FakeTiingo:
    def __init__(self, offline=False):
        self.offline = offline
        self.calls = []
        self.closed = False

    def get_iex_quotes(self, symbols):
        self.calls.append(symbols)
        if self.offline:
            raise RuntimeError("IEX offline")
        return {s: IexQuote(s, 100.0) for s in symbols}

    def close(self):
        self.closed = True


class MarketClient:
    environment = "demo"

    def __init__(self):
        self.rates = {i: Rate(i, 99.9, 100.1, 100.0, NOW.isoformat()) for i in IDS.values()}
        self.candles = {i: [Candle("2026-10-02", 100, 101, 99, 100, timestamp=NOW.isoformat(), interval="OneDay")] for i in IDS.values()}
        self.market_calls = []
        self.portfolio_calls = 0
        self.order_sizes = []

    def resolve_instrument_id(self, symbol):
        return IDS[symbol]

    def get_rates(self, ids):
        self.market_calls.append(("rates", ids))
        return self.rates

    def get_candles(self, instrument_id, *, count):
        assert count == 1
        self.market_calls.append(("candles", instrument_id, count))
        return self.candles[instrument_id]

    def get_balance(self):
        return 1000.0

    def probe_environment(self):
        return self.environment

    def check_eligibility_for_order(self, instrument_id, amount):
        self.order_sizes.append((instrument_id, amount))
        return Eligibility(instrument_id, True, 10.0, 10.0)

    def get_portfolio(self):
        self.portfolio_calls += 1
        return []


def market_client():
    return MarketClient()


def assembled_signal():
    return SimpleNamespace(
        fingerprint=PriceWindowFingerprint("2026-10-02", "SPY", "2010-01-04", "2026-10-01", 4000, "a" * 64, "tiingo", NOW.isoformat()),
        cone_5_z=1.25, spy_context=SimpleNamespace(drawdown=-0.05, tier=1),
    )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refused(*args, **kwargs):
        raise AssertionError("test attempted a real HTTP call")

    monkeypatch.setattr(httpx, "get", refused)
    monkeypatch.setattr(httpx.Client, "send", refused)


@pytest.fixture
def config(config_path, secrets_path, host):
    return load_config(config_path=config_path, secrets_path=secrets_path, env={}, host=host)


def test_fetch_rekeys_and_falls_back(caplog):
    client, tiingo = market_client(), FakeTiingo(offline=True)
    mi = session.fetch_market_inputs(tiingo=tiingo, client=client, symbols=list(IDS))
    assert mi.instrument_ids == IDS
    assert mi.rates == {s: client.rates[i] for s, i in IDS.items()}
    assert all(p.source == "etoro_candle" and p.value == 100 for p in mi.provisional_closes.values())
    assert mi.iex == dict.fromkeys(IDS) and mi.errors == {}
    assert tiingo.calls == [list(IDS)]
    assert client.market_calls == [("rates", [1, 2, 3]), ("candles", 1, 1), ("candles", 2, 1), ("candles", 3, 1)]
    assert "Tiingo IEX unavailable; using candle fallback: IEX offline" in caplog.text


@pytest.mark.parametrize("case", ["fresh", "stale", "missing", "missing_rate"])
def test_price_gate(repo, host, config, case):
    client, tiingo = market_client(), FakeTiingo()
    if case == "stale":
        client.candles[2] = [replace(client.candles[2][0], interval="OneMinute", timestamp=(NOW - timedelta(hours=1)).isoformat())]
    elif case == "missing":
        client.candles[2] = []
        tiingo.offline = True
    elif case == "missing_rate":
        del client.rates[2]
    gates = session.build_preflight_gates(host=host, config=config, repo=repo, client=client, tiingo=tiingo, session_id=1, trading_date=DAY, now=lambda: NOW)
    assert client.market_calls == [] and tiingo.calls == []
    result = gates[9].check()
    assert result.passed is (case == "fresh")
    if case == "stale":
        assert "SCHG: eToro candle is 60.0 minutes old" in result.reason
    elif case == "missing":
        assert result.reason.startswith("no provisional close: SCHG:")
        assert list(result.details["errors"]) == ["SCHG"]
        mi = session.fetch_market_inputs(tiingo=tiingo, client=client, symbols=list(IDS))
        assert mi.candles["SCHG"] is None and "SCHG" not in mi.provisional_closes
    elif case == "missing_rate":
        assert "SCHG" in result.reason


def test_etoro_errors_propagate():
    client = market_client()
    def fail(ids):
        raise RuntimeError("eToro rates offline")
    client.get_rates = fail
    with pytest.raises(RuntimeError, match="eToro rates offline"):
        session.fetch_market_inputs(tiingo=FakeTiingo(), client=client, symbols=list(IDS))


@pytest.mark.parametrize("wait", [True, False])
@pytest.mark.parametrize("path", ["success", "rerun_failed", "rerun_missing", "signal_failed"])
def test_session_market(repo, host, config, monkeypatch, wait, path):
    client, tiingo = market_client(), FakeTiingo()
    clock = [NOW]
    sleeps = []
    monkeypatch.setattr(host, "now", lambda: clock[0])

    def sleep_until(target):
        sleeps.append(target)
        clock[0] = target
        client.rates = {i: replace(r, timestamp=target.isoformat()) for i, r in client.rates.items()}

    build_gates = session.build_preflight_gates
    def gates(**kwargs):
        real = build_gates(**kwargs)[9]
        def after_preflight():
            if path == "rerun_failed":
                client.candles[2] = [replace(client.candles[2][0], close=50)]
            elif path == "rerun_missing":
                client.candles[2] = []
                tiingo.offline = True
            return GateResult(12, "risk_caps", True, "ok")
        before = [GateSpec(n, f"gate_{n}", lambda n=n: GateResult(n, f"gate_{n}", True, "ok")) for n in range(1, 10)]
        return before + [real, GateSpec(11, "reconciliation", lambda: GateResult(11, "reconciliation", True, "ok")), GateSpec(12, "risk_caps", after_preflight)]

    monkeypatch.setattr(session, "build_preflight_gates", gates)
    received = []
    def assemble(t, **kwargs):
        received.append((t, kwargs))
        if path == "signal_failed":
            raise ValueError("bad history")
        return assembled_signal()
    monkeypatch.setattr(session, "assemble_spy_signal", assemble)
    for n, (symbol, book, ath, is_open) in enumerate([
        ("SCHG", "catapult", 120, True), ("UPRO", "reentry", 130, True),
        ("SCHG", "reentry", None, True), ("UPRO", "catapult", 999, False),
    ]):
        repo.insert_lot_annotation(LotAnnotation(str(n), symbol, book, "2026-10-01", 100, "app", entry_spy_ath=ath, is_open=is_open))
    result = session.run_session(host=host, config=config, repo=repo, client=client, tiingo=tiingo, trading_date=DAY, wait=wait, sleep_until=sleep_until)
    assert sleeps == ([session.session_close_datetime(DAY) - timedelta(minutes=30)] if wait else [])
    assert len(tiingo.calls) == 2 and len(client.market_calls) == 8
    row = repo.get_session(result.session_id)
    assert row.finished_at is not None
    checks = repo.get_preflight_checks(row.id)
    assert len(checks) == 12
    first = checks[9]
    assert first.gate == 10 and first.passed
    assert row.preflight_verdict == "pass" and row.preflight_failed_gate is None
    assert row.outcome == result.reason
    if path.startswith("rerun"):
        assert result.status == "rerun_failed" and result.failed_gate == 10
        assert row.state == "aborted" and result.reason.startswith("gate 10 rerun:")
        assert received == []
        assert repo.list_price_window_fingerprints("SPY") == []
    else:
        assert received == [(tiingo, dict(trading_date="2026-10-02", iex_quote=IexQuote("SPY", 100), etoro_daily_candle=client.candles[1][0], cone_horizon=config.strategy.cone_horizon, open_lot_entry_spy_ath=[120, 130]))]
        if path == "signal_failed":
            assert result.status == "signal_failed" and row.state == "aborted"
            assert result.reason == "signal: ValueError: bad history"
            assert repo.list_price_window_fingerprints("SPY") == []
        else:
            assert result.status == "signal_ready" and row.state == "finished"
            assert result.reason == "dry run: cone_5_z=1.25, drawdown=-0.05, tier=1; decide not built (S6c)"
            assert repo.list_price_window_fingerprints("SPY") == [assembled_signal().fingerprint]


def test_sleep_until_uses_host_in_at_most_60_second_chunks(host, monkeypatch):
    clock = [NOW]
    sleeps = []
    monkeypatch.setattr(host, "now", lambda: clock[0])
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += timedelta(seconds=seconds)
    monkeypatch.setattr(session.time, "sleep", sleep)
    session._sleep_until(NOW + timedelta(seconds=125), host=host)
    session._sleep_until(NOW, host=host)
    assert sleeps == [60, 60, 5]
